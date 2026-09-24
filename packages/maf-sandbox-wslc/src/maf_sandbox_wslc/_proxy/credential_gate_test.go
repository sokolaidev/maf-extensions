package proxy

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ironsh/iron-proxy/internal/responseretry"
	"github.com/ironsh/iron-proxy/internal/transform"
	"github.com/ironsh/iron-proxy/internal/transform/allowlist"
	"github.com/stretchr/testify/require"
)

func mafFixture(t *testing.T, lifetime time.Duration) *mafCredentials {
	t.Helper()
	boot := strings.Repeat("a", 48)
	expires := float64(time.Now().Add(lifetime).UnixMilli()) / 1000
	grant := mafGrant{Boot: boot, Peer: "172.22.1.4", ExpiresAt: expires,
		Entries: []mafCredential{{Host: "api.example.com", Port: 8443, Methods: []string{"GET"},
			Paths: []string{"/v1/*"}, Token: "user-alice-secret", ExpiresAt: expires}}}
	b, err := json.Marshal(grant)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "grant.json")
	if err := os.WriteFile(path, b, 0600); err != nil {
		t.Fatal(err)
	}
	return &mafCredentials{enabled: true, boot: boot, path: path, deadline: time.Now().Add(lifetime)}
}

func mafRequest(method, target, peer string) *http.Request {
	r, _ := http.NewRequest(method, target, nil)
	r.Header.Set("Authorization", "Bearer copied-from-another-user")
	return r.WithContext(context.WithValue(r.Context(), mafPeerKey{}, peer))
}

func TestMAFCredentialBoundaries(t *testing.T) {
	for _, tc := range []struct {
		name, method, target, peer string
		allowed                    bool
	}{
		{"own", "GET", "https://api.example.com:8443/v1/items", "172.22.1.4:4000", true},
		{"other-container", "GET", "https://api.example.com:8443/v1/items", "172.22.2.4:4000", false},
		{"forwarded-peer", "GET", "https://api.example.com:8443/v1/items", "172.22.2.4:4000", false},
		{"port", "GET", "https://api.example.com/v1/items", "172.22.1.4:4000", false},
		{"plaintext", "GET", "http://api.example.com:8443/v1/items", "172.22.1.4:4000", false},
		{"method", "POST", "https://api.example.com:8443/v1/items", "172.22.1.4:4000", false},
		{"path", "GET", "https://api.example.com:8443/admin", "172.22.1.4:4000", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			g := mafFixture(t, time.Minute)
			r := mafRequest(tc.method, tc.target, tc.peer)
			r.Header.Set("X-Forwarded-For", "172.22.1.4")
			got, cancel, err := g.authorize(r)
			defer cancel()
			if (err == nil) != tc.allowed {
				t.Fatalf("allowed=%v err=%v", tc.allowed, err)
			}
			if err == nil && got.Header.Get("Authorization") != "Bearer user-alice-secret" {
				t.Fatal("wrong credential")
			}
		})
	}
}

func TestMAFCredentialHeaderIsolation(t *testing.T) {
	cases := []struct {
		name, target, want string
		enabled            bool
	}{
		{"grant", "https://api.example.com:8443/v1/items", "Bearer user-alice-secret", true},
		{"other-tls", "https://other.example.com/", "", true},
		{"other-http", "http://other.example.com/", "", true},
		{"disabled", "https://other.example.com/", "Bearer copied-from-another-user", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			g := mafFixture(t, time.Minute)
			g.enabled = tc.enabled
			r := mafRequest("GET", tc.target, "172.22.1.4:1234")
			r.Header.Set("X-Request-ID", "request-1")
			original := r.Header.Clone()
			got, cancel, err := g.authorize(r)
			defer cancel()
			require.NoError(t, err)
			require.Equal(t, tc.want, got.Header.Get("Authorization"))
			require.Equal(t, "request-1", got.Header.Get("X-Request-ID"))
			require.Equal(t, original, r.Header, "transport credentials must not enter the caller's request")
		})
	}
}

func TestMAFCredentialMixedAllowlist(t *testing.T) {
	seen := make(chan http.Header, 1)
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen <- r.Header.Clone()
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()
	transport := upstream.Client().Transport.(*http.Transport).Clone()
	transport.TLSClientConfig.ServerName = "127.0.0.1"
	transport.DialContext = func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "tcp", upstream.Listener.Addr().String())
	}
	defer transport.CloseIdleConnections()
	rules, err := allowlist.New([]string{"api.example.com", "other.example.com"}, nil)
	require.NoError(t, err)
	p := New(Options{
		Pipeline: transform.NewPipelineHolder(transform.NewPipeline([]transform.Transformer{rules}, transform.BodyLimits{}, testLogger())),
		Logger:   testLogger(),
	})
	p.credentials = mafFixture(t, time.Minute)
	p.transport = transport
	for _, host := range []string{"api.example.com", "other.example.com", "api.example.com"} {
		r := httptest.NewRequest(http.MethodGet, "https://"+host+":8443/v1/items", nil)
		r.RemoteAddr = "172.22.1.4:1234"
		r.TLS = &tls.ConnectionState{ServerName: host}
		r.Header.Add("authorization", "Bearer copied-from-another-user")
		r.Header.Add("Authorization", "Basic guest-value")
		r.Header.Set("X-Request-ID", "request-1")
		w := httptest.NewRecorder()
		p.handleHTTP(w, r, nil)
		require.Equal(t, http.StatusNoContent, w.Code)
		headers := <-seen
		require.Equal(t, "request-1", headers.Get("X-Request-ID"))
		if host == "api.example.com" {
			require.Equal(t, []string{"Bearer user-alice-secret"}, headers.Values("Authorization"))
		} else {
			require.Empty(t, headers.Values("Authorization"))
		}
	}
}

func TestMAFCredentialExpiredConnection(t *testing.T) {
	g := mafFixture(t, time.Minute)
	// A single downstream connection repeatedly passes through the same gate.
	var connections atomic.Int32
	s := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r = mafRequest("GET", "https://api.example.com:8443/v1/items", "172.22.1.4:1234")
		_, cancel, err := g.authorize(r)
		defer cancel()
		if err != nil {
			w.WriteHeader(403)
			return
		}
		w.WriteHeader(200)
	}))
	s.Config.ConnState = func(_ net.Conn, state http.ConnState) {
		if state == http.StateNew {
			connections.Add(1)
		}
	}
	s.Start()
	defer s.Close()
	client := s.Client()
	resp, err := client.Get(s.URL)
	if err != nil {
		t.Fatal(err)
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatal(resp.StatusCode)
	}
	g.mu.Lock()
	g.grant.deadline = time.Now().Add(-time.Second)
	g.mu.Unlock()
	resp, err = client.Get(s.URL)
	if err != nil {
		t.Fatal(err)
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 403 || connections.Load() != 1 {
		t.Fatalf("status=%d connections=%d", resp.StatusCode, connections.Load())
	}
}

func TestMAFCredentialAuthorizationReplay(t *testing.T) {
	for _, bound := range []string{"valid", "token", "generation"} {
		t.Run(bound, func(t *testing.T) {
			var upstreamCalls atomic.Int32
			upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				require.Equal(t, "Bearer user-alice-secret", r.Header.Get("Authorization"))
				if upstreamCalls.Add(1) == 1 {
					w.WriteHeader(http.StatusPaymentRequired)
					return
				}
				require.Equal(t, "retry-token", r.Header.Get("X-Retry-Token"))
				_, err := w.Write([]byte("replayed"))
				require.NoError(t, err)
			}))
			defer upstream.Close()
			g := mafFixture(t, time.Minute)
			grant, err := g.load()
			require.NoError(t, err)
			var decisions atomic.Int32
			authorizer := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/authorize" {
					decisions.Add(1)
					g.mu.Lock()
					if bound == "generation" {
						grant.deadline = time.Now().Add(-time.Second)
					} else if bound == "token" {
						grant.Entries[0].deadline = time.Now().Add(-time.Second)
					}
					g.mu.Unlock()
					_, err := io.WriteString(w, `{"retry":true,"attempt_id":"attempt-1","headers":{"X-Retry-Token":"retry-token"}}`)
					require.NoError(t, err)
				}
			}))
			defer authorizer.Close()
			handler, err := responseretry.New(responseretry.Options{
				AuthorizeEndpoint: authorizer.URL + "/authorize", CompleteEndpoint: authorizer.URL + "/complete",
				Token: "proxy-token", SandboxID: "sandbox-1", Statuses: []int{http.StatusPaymentRequired},
				Client: authorizer.Client(),
			})
			require.NoError(t, err)
			logger := slog.New(slog.NewTextHandler(io.Discard, nil))
			p := New(Options{
				Pipeline: transform.NewPipelineHolder(transform.NewPipeline(nil, transform.BodyLimits{
					MaxRequestBodyBytes: 1 << 20, MaxResponseBodyBytes: 1 << 20,
				}, logger)),
				Logger: logger, ResponseRetryHandler: handler,
			})
			p.credentials = g
			transport := upstream.Client().Transport.(*http.Transport).Clone()
			transport.TLSClientConfig.ServerName = "127.0.0.1"
			transport.DialContext = func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "tcp", upstream.Listener.Addr().String())
			}
			defer transport.CloseIdleConnections()
			p.transport = transport
			r := httptest.NewRequest(http.MethodGet, "https://api.example.com:8443/v1/items", nil)
			r.RemoteAddr = "172.22.1.4:1234"
			r.TLS = &tls.ConnectionState{ServerName: "api.example.com"}
			recorder := httptest.NewRecorder()
			p.handleHTTP(recorder, r, nil)
			require.EqualValues(t, 1, decisions.Load())
			if bound == "valid" {
				require.Equal(t, http.StatusOK, recorder.Code)
				require.Equal(t, "replayed", recorder.Body.String())
				require.EqualValues(t, 2, upstreamCalls.Load())
			} else {
				require.Equal(t, http.StatusForbidden, recorder.Code)
				require.EqualValues(t, 1, upstreamCalls.Load())
			}
			require.NoError(t, r.Context().Err())
		})
	}
}

func TestMAFCredentialActiveUpstreamStreamExpiry(t *testing.T) {
	for _, bound := range []string{"generation", "token"} {
		t.Run(bound, func(t *testing.T) {
			cancelled := make(chan struct{})
			release := make(chan struct{})
			upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/warmup" {
					w.WriteHeader(http.StatusNoContent)
					return
				}
				if r.Header.Get("Authorization") != "Bearer user-alice-secret" {
					t.Error("upstream did not receive the bound credential")
				}
				w.Write([]byte("first chunk\n"))
				w.(http.Flusher).Flush()
				select {
				case <-r.Context().Done():
					close(cancelled)
				case <-release:
				}
			}))
			defer upstream.Close()
			defer close(release)
			transport := upstream.Client().Transport.(*http.Transport).Clone()
			transport.TLSClientConfig.ServerName = "127.0.0.1"
			transport.DialContext = func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "tcp", upstream.Listener.Addr().String())
			}
			defer transport.CloseIdleConnections()
			g := mafFixture(t, time.Minute)
			grant, err := g.load()
			if err != nil {
				t.Fatal(err)
			}
			// Establish TLS before the bounded streaming interval starts.
			warmup, err := http.NewRequest(http.MethodGet, "https://api.example.com:8443/warmup", nil)
			require.NoError(t, err)
			warmResponse, err := transport.RoundTrip(warmup)
			require.NoError(t, err)
			require.NoError(t, warmResponse.Body.Close())
			deadline := time.Now().Add(5 * time.Second)
			if bound == "generation" {
				grant.deadline = deadline
				grant.Entries[0].deadline = deadline
			} else {
				grant.Entries[0].deadline = deadline
			}
			p := &Proxy{credentials: g, transport: transport}
			resp, err := p.doUpstream(mafRequest("GET", "https://api.example.com:8443/v1/items", "172.22.1.4:1234"))
			if err != nil {
				t.Fatal(err)
			}
			defer resp.Body.Close()
			chunk := make([]byte, len("first chunk\n"))
			if _, err := io.ReadFull(resp.Body, chunk); err != nil || string(chunk) != "first chunk\n" {
				t.Fatalf("initial streaming read: %q, %v", chunk, err)
			}
			done := make(chan error, 1)
			go func() {
				_, err := io.Copy(io.Discard, resp.Body)
				done <- err
			}()
			select {
			case err := <-done:
				if !errors.Is(err, context.DeadlineExceeded) {
					t.Fatalf("stream ended without deadline cancellation: %v", err)
				}
			case <-time.After(10 * time.Second):
				t.Fatal("active upstream body outlived credential deadline")
			}
			select {
			case <-cancelled:
			case <-time.After(5 * time.Second):
				t.Fatal("upstream did not observe stream cancellation")
			}
		})
	}
}

func TestMAFCredentialRefusalIsAuditedAsDenial(t *testing.T) {
	for _, reason := range []string{"missing", "expired", "peer", "origin"} {
		t.Run(reason, func(t *testing.T) {
			g := mafFixture(t, time.Minute)
			r := mafRequest("GET", "https://api.example.com:8443/v1/items", "172.22.1.4:1234")
			r.RemoteAddr = "172.22.1.4:1234"
			r.TLS = &tls.ConnectionState{ServerName: "api.example.com"}
			switch reason {
			case "missing":
				os.Remove(g.path)
			case "expired":
				g.deadline = time.Now().Add(-time.Second)
			case "peer":
				r.RemoteAddr = "172.22.2.4:1234"
			case "origin":
				r.URL.Host = "api.example.com:443"
				r.Host = r.URL.Host
			}
			pipeline := transform.NewPipeline(nil, transform.BodyLimits{}, testLogger())
			var result *transform.PipelineResult
			pipeline.SetAuditFunc(func(r *transform.PipelineResult) { result = r })
			p := New(Options{Pipeline: transform.NewPipelineHolder(pipeline), Logger: testLogger()})
			p.credentials = g
			w := httptest.NewRecorder()
			p.handleHTTP(w, r, nil)
			if w.Code != http.StatusForbidden || result == nil || result.Action != transform.ActionReject || result.Err != nil {
				t.Fatalf("credential refusal: status=%d result=%+v", w.Code, result)
			}
		})
	}
}

func TestMAFCredentialMissingMalformedRestartAndOtherHost(t *testing.T) {
	g := mafFixture(t, time.Minute)
	r := mafRequest("GET", "https://api.example.com:8443/v1/items", "172.22.1.4:1234")
	g.boot = strings.Repeat("b", 48)
	if _, cancel, err := g.authorize(r); err == nil {
		cancel()
		t.Fatal("restart restored grant")
	}
	if err := os.WriteFile(g.path, []byte("{"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := g.authorize(r); err == nil {
		t.Fatal("partial grant accepted")
	}
	os.Remove(g.path)
	if _, _, err := g.authorize(r); err == nil {
		t.Fatal("missing grant accepted")
	}
	g = mafFixture(t, time.Minute)
	other := mafRequest("GET", "https://other.example.com/", "172.22.1.4:1234")
	got, cancel, err := g.authorize(other)
	defer cancel()
	require.NoError(t, err)
	require.Empty(t, got.Header.Values("Authorization"))
}
