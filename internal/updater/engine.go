package updater

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/release"
)

var ErrNotFound = errors.New("Docker object not found")
var dockerID = regexp.MustCompile(`^[a-f0-9]{64}$`)
var dockerName = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$`)

// Engine only talks to the configured local Unix socket; no proxy, redirect,
// remote host selection, shell interpolation or Docker CLI is involved.
type Engine struct {
	client  *http.Client
	version string
	Project string
}

func NewEngine(ctx context.Context, socket string) (*Engine, error) {
	if socket == "" {
		return nil, ErrState
	}
	transport := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{Timeout: 5 * time.Second}).DialContext(ctx, "unix", socket)
	}, MaxConnsPerHost: 8}
	e := &Engine{client: &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("Docker redirects forbidden") }}}
	probe, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	var version struct {
		API     string `json:"ApiVersion"`
		Minimum string `json:"MinAPIVersion"`
	}
	// Engine version responses contain other documented fields.
	if err := e.call(probe, "GET", "/version", nil, &version); err != nil {
		transport.CloseIdleConnections()
		return nil, err
	}
	parse := func(raw string) (int, error) {
		values := strings.Split(raw, ".")
		if len(values) != 2 || values[0] != "1" {
			return 0, ErrState
		}
		return strconv.Atoi(values[1])
	}
	maximum, err := parse(version.API)
	if err != nil {
		return nil, ErrState
	}
	minimum, err := parse(version.Minimum)
	if err != nil {
		return nil, ErrState
	}
	if maximum > 52 {
		maximum = 52
	}
	if maximum < 44 || minimum > maximum {
		return nil, errors.New("unsupported Docker API version")
	}
	e.version = fmt.Sprintf("/v1.%d", maximum)
	return e, nil
}
func (e *Engine) Close() {
	if t, ok := e.client.Transport.(*http.Transport); ok {
		t.CloseIdleConnections()
	}
}
func (e *Engine) request(ctx context.Context, method, path string, body io.Reader, contentType string) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, method, "http://docker"+e.version+path, body)
	if err != nil {
		return nil, err
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	r, err := e.client.Do(req)
	if err != nil {
		return nil, errors.New("Docker request failed")
	}
	allowedAlready := r.StatusCode == http.StatusNotModified && method == "POST" && (strings.HasSuffix(path, "/start") || strings.Contains(path, "/stop?t="))
	if (r.StatusCode < 200 || r.StatusCode >= 300) && !allowedAlready {
		r.Body.Close()
		if r.StatusCode == 404 {
			return nil, ErrNotFound
		}
		return nil, errors.New("Docker operation rejected")
	}
	return r, nil
}
func (e *Engine) call(ctx context.Context, method, path string, value, result any) error {
	// Docker stop sends headers only after its graceful timeout. A global
	// header timeout equal to that timeout incorrectly interrupts replacement.
	wait := 30 * time.Second
	if strings.Contains(path, "/stop?t=") {
		wait = 45 * time.Second
	}
	if strings.Contains(path, "/wait?condition=") {
		wait = 6 * time.Minute
	}
	ctx, cancel := context.WithTimeout(ctx, wait)
	defer cancel()
	var body io.Reader
	if value != nil {
		raw, err := json.Marshal(value)
		if err != nil || len(raw) > 16<<20 {
			return ErrState
		}
		body = bytes.NewReader(raw)
	}
	r, err := e.request(ctx, method, path, body, "application/json")
	if err != nil {
		return err
	}
	defer r.Body.Close()
	if result == nil {
		_, err = io.Copy(io.Discard, io.LimitReader(r.Body, 1<<20))
		return err
	}
	raw, err := io.ReadAll(io.LimitReader(r.Body, (16<<20)+1))
	if err != nil {
		return err
	}
	if _, err = protocol.ParseStrictJSON(raw, 16<<20); err != nil {
		return errors.New("invalid Docker response")
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	if d.Decode(result) != nil {
		return errors.New("invalid Docker response")
	}
	return nil
}

type Container struct {
	ID              string         `json:"Id"`
	Name            string         `json:"Name"`
	Image           string         `json:"Image"`
	Config          map[string]any `json:"Config"`
	HostConfig      map[string]any `json:"HostConfig"`
	NetworkSettings struct {
		Networks map[string]map[string]any `json:"Networks"`
	} `json:"NetworkSettings"`
	State struct {
		Status   string `json:"Status"`
		ExitCode int    `json:"ExitCode"`
		Health   struct {
			Status string `json:"Status"`
		} `json:"Health"`
	} `json:"State"`
}

func (c Container) valid() bool {
	return dockerID.MatchString(c.ID) && dockerID.MatchString(strings.TrimPrefix(c.Image, "sha256:")) && dockerName.MatchString(strings.TrimPrefix(c.Name, "/")) && c.Config != nil && c.HostConfig != nil
}
func (c Container) label(key string) string {
	labels, _ := c.Config["Labels"].(map[string]any)
	value, _ := labels[key].(string)
	return value
}
func (e *Engine) Inspect(ctx context.Context, name string) (Container, error) {
	var c Container
	if !dockerID.MatchString(name) && !dockerName.MatchString(name) {
		return c, ErrState
	}
	err := e.call(ctx, "GET", "/containers/"+name+"/json", nil, &c)
	if err == nil && !c.valid() {
		return c, ErrState
	}
	return c, err
}
func (e *Engine) Service(ctx context.Context, project, service string) (Container, error) {
	if !dockerName.MatchString(project) || !dockerName.MatchString(service) {
		return Container{}, ErrState
	}
	filters, _ := json.Marshal(map[string][]string{"label": {"com.docker.compose.project=" + project, "com.docker.compose.service=" + service}})
	var rows []struct {
		ID string `json:"Id"`
	}
	err := e.call(ctx, "GET", "/containers/json?all=true&filters="+url.QueryEscape(string(filters)), nil, &rows)
	if err != nil {
		return Container{}, err
	}
	if len(rows) != 1 {
		return Container{}, errors.New("expected exactly one project service container")
	}
	c, err := e.Inspect(ctx, rows[0].ID)
	if err != nil {
		return c, err
	}
	if c.label("com.docker.compose.project") != project || c.label("com.docker.compose.service") != service {
		return c, ErrState
	}
	return c, nil
}
func cloneObject(value map[string]any) map[string]any {
	raw, _ := json.Marshal(value)
	var result map[string]any
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	d.Decode(&result)
	return result
}
func (c Container) createBody(image string) (map[string]any, error) {
	if !c.valid() || image == "" || len(image) > 256 {
		return nil, ErrState
	}
	body := cloneObject(c.Config)
	body["Image"] = image
	host := cloneObject(c.HostConfig)
	host["AutoRemove"] = false
	body["HostConfig"] = host
	endpoints := map[string]any{}
	for name, network := range c.NetworkSettings.Networks {
		endpoint := map[string]any{}
		for _, key := range []string{"IPAMConfig", "Links", "DriverOpts"} {
			if v, ok := network[key]; ok && v != nil {
				endpoint[key] = v
			}
		}
		var aliases []string
		if raw, ok := network["Aliases"].([]any); ok {
			for _, v := range raw {
				if a, ok := v.(string); ok && a != "" && a != c.ID && a != c.ID[:12] {
					aliases = append(aliases, a)
				}
			}
		}
		endpoint["Aliases"] = aliases
		endpoints[name] = endpoint
	}
	body["NetworkingConfig"] = map[string]any{"EndpointsConfig": endpoints}
	return body, nil
}
func (e *Engine) Create(ctx context.Context, name string, body map[string]any) (string, error) {
	if !dockerName.MatchString(name) {
		return "", ErrState
	}
	var result struct {
		ID string `json:"Id"`
	}
	err := e.call(ctx, "POST", "/containers/create?name="+url.QueryEscape(name), body, &result)
	if err != nil {
		return "", err
	}
	if !dockerID.MatchString(result.ID) {
		return "", ErrState
	}
	return result.ID, nil
}
func (e *Engine) Start(ctx context.Context, id string) error {
	if !dockerID.MatchString(id) {
		return ErrState
	}
	return e.call(ctx, "POST", "/containers/"+id+"/start", nil, nil)
}
func (e *Engine) Stop(ctx context.Context, id string) error {
	if !dockerID.MatchString(id) {
		return ErrState
	}
	return e.call(ctx, "POST", "/containers/"+id+"/stop?t=30", nil, nil)
}
func (e *Engine) Remove(ctx context.Context, id string) error {
	if !dockerID.MatchString(id) {
		return ErrState
	}
	return e.call(ctx, "DELETE", "/containers/"+id+"?force=false&v=false", nil, nil)
}
func (e *Engine) Wait(ctx context.Context, id string) error {
	if !dockerID.MatchString(id) {
		return ErrState
	}
	var result struct {
		Code  int `json:"StatusCode"`
		Error *struct {
			Message string `json:"Message"`
		} `json:"Error"`
	}
	if err := e.call(ctx, "POST", "/containers/"+id+"/wait?condition=not-running", nil, &result); err != nil {
		return err
	}
	if result.Code != 0 || result.Error != nil {
		return errors.New("native helper failed")
	}
	return nil
}
func (e *Engine) Healthy(ctx context.Context, id string) error {
	for {
		c, err := e.Inspect(ctx, id)
		if err != nil {
			return err
		}
		if c.State.Status == "running" && c.State.Health.Status == "healthy" {
			return nil
		}
		if c.State.Status == "dead" || c.State.Status == "exited" || c.State.Health.Status == "unhealthy" {
			return errors.New("release service failed health check")
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Second):
		}
	}
}
func (e *Engine) Exec(ctx context.Context, id string, command []string) error {
	if !dockerID.MatchString(id) || len(command) == 0 {
		return ErrState
	}
	var created struct {
		ID string `json:"Id"`
	}
	if err := e.call(ctx, "POST", "/containers/"+id+"/exec", map[string]any{"Cmd": command, "AttachStdout": false, "AttachStderr": false, "Tty": false}, &created); err != nil {
		return err
	}
	if !dockerID.MatchString(created.ID) {
		return ErrState
	}
	if err := e.call(ctx, "POST", "/exec/"+created.ID+"/start", map[string]any{"Detach": true, "Tty": false}, nil); err != nil {
		return err
	}
	for {
		var state struct {
			Running  bool `json:"Running"`
			ExitCode int  `json:"ExitCode"`
		}
		if err := e.call(ctx, "GET", "/exec/"+created.ID+"/json", nil, &state); err != nil {
			return err
		}
		if !state.Running {
			if state.ExitCode != 0 {
				return errors.New("native release command failed")
			}
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(250 * time.Millisecond):
		}
	}
}

func (e *Engine) NginxReady(parent context.Context, id string) error {
	ctx, cancel := context.WithTimeout(parent, 60*time.Second)
	defer cancel()
	for {
		c, err := e.Inspect(ctx, id)
		if err != nil {
			return err
		}
		if c.State.Status == "exited" || c.State.Status == "dead" {
			return errors.New("Nginx release startup failed")
		}
		// Docker start acknowledges before the entrypoint renders templates.
		// A bare nginx -t can fail too early, or pass the old empty default
		// config. Require rendered transport plus the running Nginx PID too.
		if c.State.Status == "running" && e.Exec(ctx, id, []string{"/bin/sh", "-c", `test -f /etc/nginx/runtime/public-listen.conf && nginx -t && pid=$(cat /var/run/nginx.pid) && kill -0 "$pid"`}) == nil {
			return nil
		}
		select {
		case <-ctx.Done():
			return errors.New("Nginx release readiness deadline reached")
		case <-time.After(time.Second):
		}
	}
}
func (e *Engine) Build(ctx context.Context, source Source, target, component, dockerfile string) (string, error) {
	if !release.ValidSHA(target) || !dockerName.MatchString(e.Project) || (component != "web" && component != "nginx" && component != "updater") {
		return "", ErrState
	}
	archive, err := source.Archive(ctx, target)
	if err != nil {
		return "", err
	}
	defer archive.Close()
	tag := "frontiercloud-" + component + ":" + target
	labels, _ := json.Marshal(map[string]string{"frontiercloud.revision": target, "frontiercloud.component": component, "frontiercloud.runtime": "go", "frontiercloud.schema-generation": "2", "frontiercloud.project": e.Project})
	args, _ := json.Marshal(map[string]string{"REVISION": target})
	query := url.Values{"dockerfile": {dockerfile}, "t": {tag}, "rm": {"true"}, "forcerm": {"true"}, "labels": {string(labels)}, "buildargs": {string(args)}, "version": {"1"}}
	r, err := e.request(ctx, "POST", "/build?"+query.Encode(), archive, "application/x-tar")
	if err != nil {
		return "", err
	}
	defer r.Body.Close()
	reader := bufio.NewReaderSize(r.Body, 1<<20)
	for {
		line, err := reader.ReadSlice('\n')
		if err == io.EOF && len(line) == 0 {
			break
		}
		if err != nil && err != io.EOF {
			return "", errors.New("invalid Docker build stream")
		}
		if len(bytes.TrimSpace(line)) != 0 {
			parsed, e := protocol.ParseStrictJSON(line, 1<<20)
			if e != nil {
				return "", ErrState
			}
			row, ok := parsed.(map[string]any)
			if !ok {
				return "", ErrState
			}
			if row["error"] != nil || row["errorDetail"] != nil {
				return "", errors.New("native release image build failed")
			}
		}
		if err == io.EOF {
			break
		}
	}
	if err = archive.Close(); err != nil {
		return "", err
	}
	if _, err = e.Image(ctx, tag, target, component); err != nil {
		return "", err
	}
	return tag, nil
}

type Image struct {
	ID       string   `json:"Id"`
	RepoTags []string `json:"RepoTags"`
	Config   struct {
		Labels map[string]string `json:"Labels"`
	} `json:"Config"`
}

func (e *Engine) Image(ctx context.Context, ref, target, component string) (Image, error) {
	var image Image
	if len(ref) > 256 || strings.ContainsAny(ref, "?#\r\n") {
		return image, ErrState
	}
	err := e.call(ctx, "GET", "/images/"+url.PathEscape(ref)+"/json", nil, &image)
	if err != nil {
		return image, err
	}
	labels := image.Config.Labels
	if !dockerID.MatchString(strings.TrimPrefix(image.ID, "sha256:")) || labels["frontiercloud.revision"] != target || labels["frontiercloud.component"] != component || labels["frontiercloud.runtime"] != "go" || labels["frontiercloud.schema-generation"] != "2" {
		return image, errors.New("release image provenance or schema mismatch")
	}
	return image, nil
}

func (e *Engine) Replace(ctx context.Context, snapshot Container, image, operation string) (Container, error) {
	if !snapshot.valid() || !release.ValidSHA(operation) {
		return Container{}, ErrState
	}
	name := strings.TrimPrefix(snapshot.Name, "/")
	current, err := e.Inspect(ctx, name)
	if err == nil {
		// A matching name is not authority to remove someone else's container.
		if current.ID != snapshot.ID && (current.label("frontiercloud.release-operation") != operation || current.label("com.docker.compose.project") != snapshot.label("com.docker.compose.project") || current.label("com.docker.compose.service") != snapshot.label("com.docker.compose.service")) {
			return Container{}, ErrState
		}
		if err = e.Stop(ctx, current.ID); err != nil {
			return Container{}, err
		}
		if err = e.Remove(ctx, current.ID); err != nil {
			return Container{}, err
		}
	} else if !errors.Is(err, ErrNotFound) {
		return Container{}, err
	}
	body, err := snapshot.createBody(image)
	if err != nil {
		return Container{}, err
	}
	labels, _ := body["Labels"].(map[string]any)
	if labels == nil {
		labels = map[string]any{}
	}
	labels["frontiercloud.release-operation"] = operation
	body["Labels"] = labels
	id, err := e.Create(ctx, name, body)
	if err != nil {
		return Container{}, err
	}
	if err = e.Start(ctx, id); err != nil {
		return Container{}, err
	}
	return e.Inspect(ctx, id)
}

// Helpers retain configured mounts, environment and resource/security limits,
// but never inherit published ports, service aliases or Compose service labels.
func (e *Engine) Helper(ctx context.Context, snapshot Container, image, name string, command []string, detached bool) (string, error) {
	body, err := snapshot.createBody(image)
	if err != nil {
		return "", err
	}
	body["Cmd"], body["User"], body["ExposedPorts"], body["Healthcheck"], body["Hostname"] = command, "0:0", map[string]any{}, map[string]any{"Test": []string{"NONE"}}, ""
	body["Labels"] = map[string]any{"frontiercloud.helper": name, "frontiercloud.project": snapshot.label("com.docker.compose.project")}
	host := body["HostConfig"].(map[string]any)
	host["CapAdd"] = []string{"CHOWN", "DAC_OVERRIDE", "FOWNER"}
	host["PortBindings"], host["PublishAllPorts"] = map[string]any{}, false
	host["RestartPolicy"] = map[string]any{"Name": "no"}
	if detached {
		host["RestartPolicy"] = map[string]any{"Name": "on-failure", "MaximumRetryCount": 3}
	}
	endpoints := map[string]any{}
	for network := range snapshot.NetworkSettings.Networks {
		endpoints[network] = map[string]any{}
	}
	body["NetworkingConfig"] = map[string]any{"EndpointsConfig": endpoints}
	id, err := e.Create(ctx, name, body)
	if err != nil {
		return "", err
	}
	if err = e.Start(ctx, id); err != nil {
		return id, err
	}
	if detached {
		return id, nil
	}
	err = e.Wait(ctx, id)
	// On cancellation an unresolved helper must not be removed forcefully or
	// mistaken for a completed mutation. Recovery inspects its recorded name.
	if err != nil {
		return id, err
	}
	return id, e.Remove(ctx, id)
}

var releaseTag = regexp.MustCompile(`^frontiercloud-(web|nginx|updater):([a-f0-9]{40})$`)

func (e *Engine) Cleanup(ctx context.Context, current, previous string) error {
	if !release.ValidSHA(current) || !dockerName.MatchString(e.Project) || (previous != "" && !release.ValidSHA(previous)) {
		return ErrState
	}
	var images []Image
	if err := e.call(ctx, "GET", "/images/json", nil, &images); err != nil {
		return err
	}
	for _, image := range images {
		for _, tag := range image.RepoTags {
			match := releaseTag.FindStringSubmatch(tag)
			if match == nil || match[2] == current || match[2] == previous {
				continue
			}
			// Remove only our exact release tag, never an ID/shared tag/parent, and
			// never force deletion of an image currently used by another container.
			if owned, err := e.Image(ctx, tag, match[2], match[1]); err != nil || owned.Config.Labels["frontiercloud.project"] != e.Project {
				continue
			}
			e.call(ctx, "DELETE", "/images/"+url.PathEscape(tag)+"?force=false&noprune=true", nil, nil)
		}
	}
	return ctx.Err()
}
