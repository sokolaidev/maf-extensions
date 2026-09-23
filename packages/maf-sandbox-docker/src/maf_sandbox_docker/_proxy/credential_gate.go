package proxy

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/ironsh/iron-proxy/internal/hostmatch"
)

// Each process accepts one immutable grant, installed over the host engine's stdin
// channel. No guest-facing control endpoint or shared credential cache exists.
type mafCredentials struct {
	mu         sync.Mutex
	enabled    bool
	boot, path string
	grant      *mafGrant
	deadline   time.Time
}

type mafGrant struct {
	Boot      string          `json:"boot"`
	Peer      string          `json:"peer"`
	ExpiresAt float64         `json:"expires_at"`
	Entries   []mafCredential `json:"entries"`
	deadline  time.Time
}

type mafCredential struct {
	Host      string   `json:"host"`
	Port      int      `json:"port"`
	Methods   []string `json:"methods"`
	Paths     []string `json:"paths"`
	Token     string   `json:"token"`
	ExpiresAt float64  `json:"expires_at"`
	deadline  time.Time
	rule      hostmatch.Rule
}

type mafPeerKey struct{}

var errMAFCredential = errors.New("credential gateway authorization refused")

func newMAFCredentials() *mafCredentials {
	boot, _ := os.ReadFile("/run/maf-proxy/boot")
	seconds, _ := strconv.Atoi(os.Getenv("MAF_SANDBOX_CREDENTIAL_MAX_SECONDS"))
	if seconds < 1 || seconds > 3600 {
		seconds = 0
	}
	return &mafCredentials{
		enabled: os.Getenv("MAF_SANDBOX_CREDENTIALS") == "1",
		boot:    strings.TrimSpace(string(boot)), path: "/run/maf-proxy/grant.json",
		deadline: time.Now().Add(time.Duration(seconds) * time.Second),
	}
}

func (g *mafCredentials) load() (*mafGrant, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.grant != nil {
		return g.grant, nil
	}
	f, err := os.Open(g.path)
	if err != nil {
		return nil, errMAFCredential
	}
	defer f.Close()
	dec := json.NewDecoder(io.LimitReader(f, 1048577))
	dec.DisallowUnknownFields()
	var grant mafGrant
	if dec.Decode(&grant) != nil || dec.Decode(new(any)) != io.EOF {
		return nil, errMAFCredential
	}
	now := time.Now()
	remaining := time.UnixMilli(int64(grant.ExpiresAt * 1000)).Sub(now)
	if len(g.boot) != 48 || grant.Boot != g.boot || net.ParseIP(grant.Peer) == nil ||
		remaining <= 0 || remaining > time.Hour || len(grant.Entries) == 0 {
		return nil, errMAFCredential
	}
	// Add preserves the monotonic clock: wall-clock rollback cannot extend a live grant.
	grant.deadline = now.Add(remaining)
	if g.deadline.Before(grant.deadline) {
		grant.deadline = g.deadline
	}
	for i := range grant.Entries {
		e := &grant.Entries[i]
		if e.Host == "" || strings.ContainsAny(e.Host, "*?[]/\\\r\n ") || e.Port < 1 || e.Port > 65535 ||
			len(e.Token) == 0 || len(e.Token) > 16384 {
			return nil, errMAFCredential
		}
		for _, c := range e.Token {
			if c < 33 || c > 126 {
				return nil, errMAFCredential
			}
		}
		rules, err := hostmatch.CompileRules([]hostmatch.RuleConfig{{Host: e.Host, Methods: e.Methods, Paths: e.Paths}}, "credential")
		if err != nil {
			return nil, errMAFCredential
		}
		e.rule = rules[0]
		remaining := time.UnixMilli(int64(e.ExpiresAt * 1000)).Sub(now)
		e.deadline = now.Add(remaining)
		if remaining <= 0 {
			return nil, errMAFCredential
		}
		if grant.deadline.Before(e.deadline) {
			e.deadline = grant.deadline
		}
	}
	g.grant = &grant
	return g.grant, nil
}

// authorize is called for every upstream request, including reused TLS and HTTP/2
// connections. Cancellation closes active upstream streams at the same deadline.
func (g *mafCredentials) authorize(req *http.Request) (*http.Request, context.CancelFunc, error) {
	noop := func() {}
	if g == nil || !g.enabled {
		return req, noop, nil
	}
	grant, err := g.load()
	if err != nil {
		return nil, noop, errMAFCredential
	}
	remote, _ := req.Context().Value(mafPeerKey{}).(string)
	peer, _, err := net.SplitHostPort(remote)
	if err != nil || !net.ParseIP(peer).Equal(net.ParseIP(grant.Peer)) || !time.Now().Before(grant.deadline) {
		return nil, noop, errMAFCredential
	}
	deadline := grant.deadline
	host := strings.ToLower(req.URL.Hostname())
	port := req.URL.Port()
	if port == "" {
		port = "443"
	}
	for _, entry := range grant.Entries {
		if !strings.EqualFold(host, entry.Host) {
			continue
		}
		if req.URL.Scheme != "https" || port != strconv.Itoa(entry.Port) ||
			!entry.rule.Matches(host, req.Method, req.URL.Path) || !time.Now().Before(entry.deadline) {
			return nil, noop, errMAFCredential
		}
		deadline = entry.deadline
		// A guest value never chooses a principal, including another guest's placeholder.
		req.Header.Set("Authorization", "Bearer "+entry.Token)
		break
	}
	ctx, cancel := context.WithDeadline(req.Context(), deadline)
	return req.WithContext(ctx), cancel, nil
}

type mafResponseBody struct {
	io.ReadCloser
	cancel context.CancelFunc
}

func (b *mafResponseBody) Close() error {
	defer b.cancel()
	return b.ReadCloser.Close()
}
