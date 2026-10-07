package release

import (
	"bufio"
	"context"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestSocketAgentBoundedFramingAndUnavailableStatus(t *testing.T) {
	for _, response := range []string{`{"ok":true,"status":{"state":"idle","current_sha":"fixture"}}` + "\n", `{"ok":true} {}` + "\n", strings.Repeat("x", 65537) + "\n", "broken\n"} {
		t.Run("response", func(t *testing.T) {
			dir, err := os.MkdirTemp("", "fc-agent-")
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { os.RemoveAll(dir) })
			name := filepath.Join(dir, "control.sock")
			listener, err := net.Listen("unix", name)
			if err != nil {
				t.Fatal(err)
			}
			defer listener.Close()
			done := make(chan struct{})
			go func() {
				defer close(done)
				conn, err := listener.Accept()
				if err != nil {
					return
				}
				defer conn.Close()
				conn.SetDeadline(time.Now().Add(3 * time.Second))
				raw, err := bufio.NewReader(conn).ReadBytes('\n')
				if err != nil {
					return
				}
				var value map[string]any
				if json.Unmarshal(raw, &value) != nil || value["action"] != "status" {
					t.Error("invalid agent framing")
				}
				conn.Write([]byte(response))
			}()
			out := AgentStatus(context.Background(), SocketAgent{Path: name})
			if strings.Contains(response, "fixture") {
				if out["state"] != "idle" {
					t.Fatal(out)
				}
			} else if out["state"] != "unavailable" {
				t.Fatal("malformed response trusted", out)
			}
			<-done
		})
	}
}
