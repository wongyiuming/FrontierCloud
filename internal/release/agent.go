// Package release implements runtime-independent release control. Docker mutation
// belongs to the isolated updater, never to an HTTP business handler.
package release

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"time"
)

const ControlSocket = "/run/frontiercloud-updater/control.sock"

type Agent interface {
	Request(context.Context, map[string]any) (map[string]any, error)
}
type SocketAgent struct{ Path string }

func (a SocketAgent) Request(parent context.Context, value map[string]any) (map[string]any, error) {
	ctx, cancel := context.WithTimeout(parent, 3*time.Second)
	defer cancel()
	name := a.Path
	if name == "" {
		name = ControlSocket
	}
	raw, err := json.Marshal(value)
	if err != nil || len(raw) > 8190 {
		return nil, errors.New("invalid updater request")
	}
	conn, err := (&net.Dialer{}).DialContext(ctx, "unix", name)
	if err != nil {
		return nil, errors.New("updater unavailable")
	}
	defer conn.Close()
	deadline, _ := ctx.Deadline()
	if err = conn.SetDeadline(deadline); err != nil {
		return nil, err
	}
	stop := context.AfterFunc(ctx, func() { conn.Close() })
	defer stop()
	if _, err = io.Copy(conn, bytes.NewReader(append(raw, '\n'))); err != nil {
		return nil, errors.New("updater request failed")
	}
	line, err := bufio.NewReaderSize(conn, 65536).ReadSlice('\n')
	if err != nil || len(line) > 65536 {
		return nil, errors.New("invalid updater response")
	}
	var result map[string]any
	decoder := json.NewDecoder(bytes.NewReader(line))
	decoder.UseNumber()
	if decoder.Decode(&result) != nil || result == nil {
		return nil, errors.New("invalid updater response")
	}
	var tail any
	if decoder.Decode(&tail) != io.EOF {
		return nil, errors.New("invalid updater response")
	}
	return result, nil
}

func AgentStatus(ctx context.Context, agent Agent) map[string]any {
	if agent != nil {
		value, err := agent.Request(ctx, map[string]any{"action": "status"})
		if err == nil && value["ok"] == true {
			if status, ok := value["status"].(map[string]any); ok && status != nil {
				return status
			}
		}
	}
	return map[string]any{"state": "unavailable", "phase": "unavailable", "detail": "updater unavailable"}
}
