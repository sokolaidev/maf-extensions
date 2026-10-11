package main

import (
	"bytes"
	"encoding/json"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

func send(t *testing.T, p *plugin, path string, req request) response {
	t.Helper()
	body, _ := json.Marshal(req)
	recorder := httptest.NewRecorder()
	p.serve(recorder, httptest.NewRequest("POST", path, bytes.NewReader(body)))
	var reply response
	if err := json.Unmarshal(recorder.Body.Bytes(), &reply); err != nil {
		t.Fatal(err)
	}
	return reply
}
func arm(t *testing.T, root string) {
	t.Helper()
	body, _ := json.Marshal(target{Name: "compiler", ID: strings.Repeat("a", 64), Nonce: strings.Repeat("b", 32)})
	if err := os.WriteFile(root+"/deny.json", body, 0600); err != nil {
		t.Fatal(err)
	}
}
func TestOnlySelectedContainerRemovalIsDenied(t *testing.T) {
	for _, tc := range []struct {
		method, uri string
		deny        bool
	}{
		{"DELETE", "/v1.54/containers/compiler?force=1", true},
		{"DELETE", "/containers/" + strings.Repeat("a", 64), true},
		{"DELETE", "/v1.54/containers/%63ompiler", true},
		{"DELETE", "/v1.54/containers/compiler-other?force=1", false},
		{"DELETE", "/v1.54/images/compiler", false},
		{"GET", "/v1.54/containers/compiler/json", false},
		{"POST", "/v1.54/containers/compiler/kill", false},
		{"DELETE", "/v1.54/containers/compiler/archive", false},
	} {
		t.Run(tc.method+tc.uri, func(t *testing.T) {
			root := t.TempDir()
			arm(t, root)
			p := &plugin{root: root}
			reply := send(t, p, "/AuthZPlugin.AuthZReq", request{Method: tc.method, URI: tc.uri})
			if reply.Allow == tc.deny || reply.Err != "" {
				t.Fatalf("unexpected reply %+v", reply)
			}
			if tc.deny && reply.Msg != "qualification removal denied" {
				t.Fatal(reply)
			}
		})
	}
}
func TestDisarmAndResponseAuthorization(t *testing.T) {
	root := t.TempDir()
	arm(t, root)
	p := &plugin{root: root}
	req := request{Method: "DELETE", URI: "/v1.54/containers/compiler?force=1", Headers: map[string]string{"X-Qualification-Client": strings.Repeat("c", 32)}}
	if send(t, p, "/AuthZPlugin.AuthZReq", req).Allow {
		t.Fatal("armed removal allowed")
	}
	if !send(t, p, "/AuthZPlugin.AuthZRes", req).Allow {
		t.Fatal("response denied")
	}
	if err := os.Remove(root + "/deny.json"); err != nil {
		t.Fatal(err)
	}
	if !send(t, p, "/AuthZPlugin.AuthZReq", req).Allow {
		t.Fatal("disarmed removal denied")
	}
	data, err := os.ReadFile(root + "/authz.jsonl")
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) != 2 {
		t.Fatalf("got %d records", len(lines))
	}
	for i, line := range lines {
		var row map[string]any
		if err := json.Unmarshal([]byte(line), &row); err != nil {
			t.Fatal(err)
		}
		if row["sequence"] != float64(i+1) || row["client"] != strings.Repeat("c", 32) || row["denied"] != (i == 0) || row["allowed"] != (i == 1) {
			t.Fatal(row)
		}
		if i == 0 && row["nonce"] != strings.Repeat("b", 32) {
			t.Fatal(row)
		}
		if i == 1 && row["nonce"] != "" {
			t.Fatal(row)
		}
	}
}
func TestMissingEvidenceAndInvalidPolicyFailClosed(t *testing.T) {
	for _, mode := range []string{"invalid", "unwritable", "invalid-name", "invalid-id", "invalid-nonce"} {
		t.Run(mode, func(t *testing.T) {
			root := t.TempDir()
			arm(t, root)
			if mode == "unwritable" {
				if err := os.Mkdir(root+"/authz.jsonl", 0700); err != nil {
					t.Fatal(err)
				}
			} else if mode == "invalid" {
				os.WriteFile(root+"/deny.json", []byte("{"), 0600)
			} else {
				selected := target{Name: "compiler", ID: strings.Repeat("a", 64), Nonce: strings.Repeat("b", 32)}
				if mode == "invalid-name" {
					selected.Name = "../compiler"
				}
				if mode == "invalid-id" {
					selected.ID = "a"
				}
				if mode == "invalid-nonce" {
					selected.Nonce = ""
				}
				body, _ := json.Marshal(selected)
				os.WriteFile(root+"/deny.json", body, 0600)
			}
			reply := send(t, &plugin{root: root}, "/AuthZPlugin.AuthZReq", request{Method: "DELETE", URI: "/containers/compiler"})
			if reply.Allow || reply.Err == "" || reply.Msg != "" {
				t.Fatal(reply)
			}
		})
	}
}
