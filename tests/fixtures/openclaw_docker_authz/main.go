// A fixture-only Docker authorization plugin for one selected removal target.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"
)

type target struct {
	Name  string `json:"name"`
	ID    string `json:"id"`
	Nonce string `json:"nonce"`
}
type request struct {
	Method  string            `json:"RequestMethod"`
	URI     string            `json:"RequestURI"`
	Headers map[string]string `json:"RequestHeaders"`
}
type response struct {
	Allow bool
	Msg   string
	Err   string
}

var version = regexp.MustCompile(`^/v[0-9]+\.[0-9]+/`)
var identity = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$`)

func removal(req request) string {
	if req.Method != "DELETE" {
		return ""
	}
	parsed, err := url.ParseRequestURI(req.URI)
	if err != nil {
		return ""
	}
	path := version.ReplaceAllString(parsed.Path, "/")
	if !strings.HasPrefix(path, "/containers/") {
		return ""
	}
	name := strings.TrimPrefix(path, "/containers/")
	if !identity.MatchString(name) {
		return ""
	}
	return name
}

type plugin struct {
	root     string
	mu       sync.Mutex
	sequence int
}

func (p *plugin) serve(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	if r.Method != "POST" {
		http.Error(w, "method", 405)
		return
	}
	if r.URL.Path == "/Plugin.Activate" {
		json.NewEncoder(w).Encode(map[string]any{"Implements": []string{"authz"}})
		return
	}
	if r.URL.Path != "/AuthZPlugin.AuthZReq" && r.URL.Path != "/AuthZPlugin.AuthZRes" {
		http.NotFound(w, r)
		return
	}
	var req request
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		json.NewEncoder(w).Encode(response{Err: "invalid request"})
		return
	}
	answer := response{Allow: true}
	if r.URL.Path == "/AuthZPlugin.AuthZReq" && removal(req) != "" {
		p.mu.Lock()
		defer p.mu.Unlock()
		var selected target
		content, err := os.ReadFile(p.root + "/deny.json")
		if err == nil {
			if json.Unmarshal(content, &selected) != nil || !identity.MatchString(selected.Name) || !regexp.MustCompile(`^[a-f0-9]{64}$`).MatchString(selected.ID) || !regexp.MustCompile(`^[a-f0-9]{32}$`).MatchString(selected.Nonce) {
				answer = response{Err: "invalid fault target"}
			} else if removal(req) == selected.Name || removal(req) == selected.ID {
				answer = response{Msg: "qualification removal denied"}
			}
		} else if !errors.Is(err, os.ErrNotExist) {
			answer = response{Err: "fault target unavailable"}
		}
		p.sequence++
		client := ""
		for key, value := range req.Headers {
			if strings.EqualFold(key, "X-Qualification-Client") {
				if regexp.MustCompile(`^[a-f0-9]{32}$`).MatchString(value) {
					client = value
				}
			}
		}
		event := map[string]any{"sequence": p.sequence, "client": client, "time_ns": time.Now().UnixNano(), "event": "docker_authz", "target": removal(req), "method": req.Method, "allowed": answer.Allow, "denied": answer.Msg == "qualification removal denied", "nonce": selected.Nonce}
		output, err := os.OpenFile(p.root+"/authz.jsonl", os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0600)
		if err != nil {
			answer = response{Err: "evidence unavailable"}
		} else {
			err = json.NewEncoder(output).Encode(event)
			if err == nil {
				err = output.Sync()
			}
			closeErr := output.Close()
			if err != nil || closeErr != nil {
				answer = response{Err: "evidence unavailable"}
			}
		}
	}
	json.NewEncoder(w).Encode(answer)
}
func main() {
	if len(os.Args) != 2 {
		panic("expected private control directory")
	}
	if err := os.MkdirAll("/run/docker/plugins", 0700); err != nil {
		panic(err)
	}
	listener, err := net.Listen("unix", "/run/docker/plugins/qualification.sock")
	if err != nil {
		panic(err)
	}
	server := http.Server{Handler: http.HandlerFunc((&plugin{root: os.Args[1]}).serve), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second, MaxHeaderBytes: 8192}
	fmt.Fprintln(os.Stderr, "qualification authorization plugin ready")
	if err := server.Serve(listener); err != nil {
		panic(err)
	}
}
