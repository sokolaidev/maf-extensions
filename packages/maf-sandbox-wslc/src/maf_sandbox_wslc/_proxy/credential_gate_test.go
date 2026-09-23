package proxy

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
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

func TestMAFCredentialExpiredConnectionAndStream(t *testing.T) {
	g := mafFixture(t, 180*time.Millisecond)
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
	r, cancel, err := g.authorize(mafRequest("GET", "https://api.example.com:8443/v1/items", "172.22.1.4:1234"))
	if err != nil {
		t.Fatal(err)
	}
	defer cancel()
	resp, err := client.Get(s.URL)
	if err != nil {
		t.Fatal(err)
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatal(resp.StatusCode)
	}
	select {
	case <-r.Context().Done():
	case <-time.After(time.Second):
		t.Fatal("active stream outlived grant")
	}
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
	if err != nil || got.Header.Get("Authorization") == "Bearer user-alice-secret" {
		t.Fatal("credential leaked to another origin")
	}
}
