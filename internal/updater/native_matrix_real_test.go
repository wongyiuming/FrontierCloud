package updater

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

const fleetNodeCount = 5

type fleetSite struct {
	root, origin, runtime, database string
	project                         string
	client                          *http.Client
	environment, binds              []string
	web                             string
}

func fleetRange(t *testing.T, ctx context.Context, sites []*fleetSite, target, resource string, payload []byte) {
	t.Helper()
	master := sites[0]
	if strings.HasPrefix(target, "/") {
		target = master.origin + target
	}
	for redirects := 0; redirects < 2; redirects++ {
		u, err := url.Parse(target)
		if err != nil {
			t.Fatal("invalid private media URL")
		}
		var site *fleetSite
		for _, candidate := range sites {
			if u.Scheme+"://"+u.Host == candidate.origin {
				site = candidate
				break
			}
		}
		if site == nil {
			t.Fatal("media delivery left private fleet")
		}
		r, err := http.NewRequestWithContext(ctx, "GET", target, nil)
		if err != nil {
			t.Fatal("invalid media request")
		}
		r.Header.Set("Origin", master.origin)
		r.Header.Set("Range", "bytes=3-12")
		response, err := site.client.Do(r)
		if err != nil {
			t.Fatal("verified private media transport failed")
		}
		if response.StatusCode == 307 && redirects == 0 {
			target = response.Header.Get("Location")
			response.Body.Close()
			continue
		}
		raw, err := io.ReadAll(io.LimitReader(response.Body, 1024))
		response.Body.Close()
		if err != nil || response.StatusCode != 206 || !bytes.Equal(raw, payload[3:13]) || response.Header.Get("Content-Range") != "bytes 3-12/"+big.NewInt(int64(len(payload))).String() {
			t.Fatal("real native matrix byte-range delivery mismatch", site.runtime, site.database, response.StatusCode, "bytes", len(raw), "content-range", response.Header.Get("Content-Range"), err)
		}
		// Resource identity is checked in the catalog. The public edge deliberately
		// hides internal audit metadata; those headers must not leak to clients.
		if resource != "" && response.Header.Get("X-Media-Resource-ID") != "" {
			t.Fatal("internal media identity leaked through public edge")
		}
		return
	}
	t.Fatal("unexpected media redirect chain")
}

func (s *fleetSite) request(ctx context.Context, method, path string, value any, headers ...map[string]string) (int, map[string]any, error) {
	var body []byte
	contentType := "application/json"
	if form, ok := value.(url.Values); ok {
		body = []byte(form.Encode())
		contentType = "application/x-www-form-urlencoded"
	} else if raw, ok := value.([]byte); ok {
		body = raw
		contentType = "application/octet-stream"
	} else if value != nil {
		body, _ = json.Marshal(value)
	}
	r, err := http.NewRequestWithContext(ctx, method, s.origin+path, bytes.NewReader(body))
	if err != nil {
		return 0, nil, err
	}
	r.Header.Set("Content-Type", contentType)
	origin, _ := url.Parse(s.origin)
	csrfCookie := "__Host-admin-csrf"
	csrfHeader := "X-CSRF-Token"
	if strings.HasPrefix(path, "/api/v1/karaoke/account/") {
		csrfCookie = "__Host-karaoke_csrf"
		csrfHeader = "X-Karaoke-CSRF"
	}
	for _, cookie := range s.client.Jar.Cookies(origin) {
		if cookie.Name == csrfCookie {
			r.Header.Set(csrfHeader, cookie.Value)
		}
	}
	for _, values := range headers {
		for key, value := range values {
			r.Header.Set(key, value)
		}
	}
	response, err := s.client.Do(r)
	if err != nil {
		return 0, nil, err
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, 2*1024*1024+1))
	if err != nil || len(raw) > 2*1024*1024 {
		return response.StatusCode, nil, ErrState
	}
	var out map[string]any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if decoder.Decode(&out) != nil {
		return response.StatusCode, nil, ErrState
	}
	return response.StatusCode, out, nil
}

func fleetCertificates(t *testing.T, directory string, extraDNS ...string) *x509.CertPool {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	ca := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "Disposable FrontierCloud matrix CA"}, NotBefore: now.Add(-time.Minute), NotAfter: now.Add(24 * time.Hour), IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature}
	caDER, err := x509.CreateCertificate(rand.Reader, ca, ca, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	// Parse the actual issuer: CreateCertificate synthesizes its SubjectKeyId
	// in DER, not in the input template. Passing the template would omit the
	// leaf AuthorityKeyId and fail Python/OpenSSL's strict verification.
	ca, err = x509.ParseCertificate(caDER)
	if err != nil {
		t.Fatal(err)
	}
	leafKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	leaf := &x509.Certificate{SerialNumber: big.NewInt(2), Subject: pkix.Name{CommonName: "*.fleet.invalid"}, DNSNames: []string{"*.fleet.invalid"}, NotBefore: now.Add(-time.Minute), NotAfter: now.Add(24 * time.Hour), KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	leaf.DNSNames = append(leaf.DNSNames, extraDNS...)
	leafDER, err := x509.CreateCertificate(rand.Reader, leaf, ca, &leafKey.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	private, err := x509.MarshalPKCS8PrivateKey(leafKey)
	if err != nil {
		t.Fatal(err)
	}
	caPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER})
	for name, raw := range map[string][]byte{"ca.pem": caPEM, "fullchain.pem": append(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: leafDER}), caPEM...), "privkey.pem": pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: private})} {
		mode := os.FileMode(0644)
		if name == "privkey.pem" {
			mode = 0600
		}
		if err := os.WriteFile(filepath.Join(directory, name), raw, mode); err != nil {
			t.Fatal(err)
		}
	}
	roots := x509.NewCertPool()
	roots.AppendCertsFromPEM(caPEM)
	return roots
}

// This fleet exercises real private-CA processes, control, media, recordings and
// logical backup transfer, outage recovery and durable role/database/cache
// restart. Whole release execution and physical restore remain separate gates;
// no existing deployment is reused.
func TestRealNativeMatrixFleetControl(t *testing.T) {
	base, prefix := os.Getenv("FRONTIERCLOUD_TEST_NATIVE_MATRIX_WORKSPACE"), os.Getenv("FRONTIERCLOUD_TEST_NATIVE_MATRIX_PREFIX")
	if base == "" {
		t.Skip("isolated native matrix fleet not selected")
	}
	info, err := os.Lstat(base)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || !filepath.IsAbs(base) || !strings.HasPrefix(filepath.Base(base), "fc-native-matrix-") || !dockerName.MatchString(prefix) || !strings.HasPrefix(prefix, "fc-matrix-") {
		t.Fatal("explicit newly created native matrix workspace required")
	}
	whole := os.Getenv("FRONTIERCLOUD_TEST_NATIVE_MATRIX_RELEASE") == "1"
	duration := 45 * time.Minute
	kinds := []string{"go-sqlite", "go-mysql"}
	if whole {
		duration = 90 * time.Minute
		kinds = []string{"go-sqlite", "go-mysql"}
		if selected := os.Getenv("FRONTIERCLOUD_TEST_NATIVE_MATRIX_MASTER"); selected != "" && selected != "go-sqlite" && selected != "go-mysql" {
			t.Fatal("whole release selects native SQLite/MySQL Master directions")
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), duration)
	defer cancel()
	e, err := NewEngine(ctx, os.Getenv("FRONTIERCLOUD_TEST_DOCKER_SOCKET"))
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	driver, err := e.Inspect(ctx, prefix+"-driver")
	if err != nil || driver.label("frontiercloud.acceptance") != prefix {
		t.Fatal("private driver ownership required", err)
	}
	selected := os.Getenv("FRONTIERCLOUD_TEST_NATIVE_MATRIX_MASTER")
	if selected != "" && selected != "go-sqlite" && selected != "go-mysql" {
		t.Fatal("unknown native matrix Master selection")
	}
	for _, kind := range kinds {
		if selected != "" && selected != kind {
			continue
		}
		t.Run(kind, func(t *testing.T) { testFleetControl(t, ctx, e, base, prefix, driver.ID, kind, whole) })
	}
}

func testFleetControl(t *testing.T, ctx context.Context, e *Engine, base, prefix, driver, kind string, whole bool) {
	t.Helper()
	project := prefix + "-" + kind
	root := filepath.Join(base, kind)
	if err := os.Mkdir(root, 0755); err != nil {
		t.Fatal(err)
	}
	certs := filepath.Join(root, "certs")
	if err := os.Mkdir(certs, 0755); err != nil {
		t.Fatal(err)
	}
	var extraDNS []string
	if whole {
		extraDNS = []string{"api.github.com"}
	}
	roots := fleetCertificates(t, certs, extraDNS...)
	owned := map[string]bool{project: true}
	for i := 0; i < fleetNodeCount && whole; i++ {
		owned[project+"-site"+big.NewInt(int64(i)).String()] = true
	}
	var containers, networks []string
	defer func() {
		cleanup, stop := context.WithTimeout(context.Background(), 5*time.Minute)
		defer stop()
		if whole {
			// Releases replace IDs and create helpers. Retire only the exact
			// newly owned projects, stopping agents before their service/helpers.
			retireFleetProjects(t, cleanup, e, owned)
		}
		for i := len(containers) - 1; i >= 0; i-- {
			if c, err := e.Inspect(cleanup, containers[i]); err == nil && c.label("com.docker.compose.project") == project {
				e.Stop(cleanup, c.ID)
				e.Remove(cleanup, c.ID)
			}
		}
		for i := len(networks) - 1; i >= 0; i-- {
			e.call(cleanup, "POST", "/networks/"+networks[i]+"/disconnect", map[string]any{"Container": driver, "Force": false}, nil)
			e.call(cleanup, "DELETE", "/networks/"+networks[i], nil, nil)
		}
	}()
	networkCIDRs := map[string][]string{}
	newNetwork := func(name string) string {
		var reply struct {
			ID string `json:"Id"`
		}
		if err := e.call(ctx, "POST", "/networks/create", map[string]any{"Name": name, "Labels": map[string]string{"frontiercloud.acceptance": project}}, &reply); err != nil {
			t.Fatal(err)
		}
		networks = append(networks, reply.ID)
		var inspected struct {
			IPAM struct {
				Config []struct {
					Subnet string `json:"Subnet"`
				} `json:"Config"`
			} `json:"IPAM"`
		}
		if err := e.call(ctx, "GET", "/networks/"+reply.ID, nil, &inspected); err != nil {
			t.Fatal("private network CIDR inventory", err)
		}
		for _, row := range inspected.IPAM.Config {
			if _, _, err := net.ParseCIDR(row.Subnet); err != nil {
				t.Fatal("private network has invalid subnet")
			}
			networkCIDRs[name] = append(networkCIDRs[name], row.Subnet)
		}
		if len(networkCIDRs[name]) == 0 {
			t.Fatal("private network has no subnet")
		}
		return reply.ID
	}
	sharedName := project + "-plane"
	shared := newNetwork(sharedName)
	var joint *fleetJointRelease
	if whole {
		joint = prepareFleetJointRelease(t, ctx, e, base, root, project, shared, certs)
		defer joint.publication.close()
	} else {
		if err := e.call(ctx, "POST", "/networks/"+shared+"/connect", map[string]any{"Container": driver}, nil); err != nil {
			t.Fatal(err)
		}
	}
	create := func(service, image string, cmd, env, binds []string, user string, endpoints map[string]any, health []string) Container {
		name := project + "-" + service
		owner, canonical := project, service
		if whole {
			owner, canonical = fleetReleaseService(project, service)
		}
		host := map[string]any{"Binds": binds, "NetworkMode": sharedName, "Tmpfs": map[string]string{"/tmp": "size=64m,mode=1777"}, "RestartPolicy": map[string]string{"Name": "no"}}
		if strings.HasSuffix(service, "-web") {
			host["NanoCpus"] = int64(1_000_000_000)
		}
		if whole && canonical == "updater" {
			host["RestartPolicy"] = map[string]string{"Name": "always"}
		}
		body := map[string]any{"Image": image, "Cmd": cmd, "User": user, "Env": env, "HostConfig": host, "Labels": map[string]string{"com.docker.compose.project": owner, "com.docker.compose.service": canonical}, "NetworkingConfig": map[string]any{"EndpointsConfig": endpoints}}
		if health != nil {
			body["Healthcheck"] = map[string]any{"Test": health, "Interval": int64(time.Second), "Timeout": int64(5 * time.Second), "Retries": 90, "StartPeriod": int64(30 * time.Second)}
		}
		id, err := e.Create(ctx, name, body)
		if err != nil {
			t.Fatal(service, err)
		}
		containers = append(containers, id)
		if err = e.Start(ctx, id); err != nil {
			t.Fatal(service, err)
		}
		c, err := e.Inspect(ctx, id)
		if err != nil {
			t.Fatal(err)
		}
		return c
	}
	sharedEndpoint := func(alias string) map[string]any {
		return map[string]any{sharedName: map[string]any{"Aliases": []string{alias}}}
	}
	native := os.Getenv("FRONTIERCLOUD_TEST_NATIVE_MATRIX_NATIVE_IMAGE")
	agent := os.Getenv("FRONTIERCLOUD_TEST_NATIVE_MATRIX_AGENT_IMAGE")
	edge := os.Getenv("FRONTIERCLOUD_TEST_NATIVE_MATRIX_EDGE_IMAGE")
	if whole {
		native, agent, edge = joint.native, joint.agent, joint.edge
	}
	commonSecrets := filepath.Join(root, "mysql-secrets")
	if err := os.Mkdir(commonSecrets, 0755); err != nil {
		t.Fatal(err)
	}
	initializer := create("common-init", native, []string{"init-secrets"}, nil, []string{commonSecrets + ":/run/frontiercloud-secrets"}, "0:0", sharedEndpoint("common-init"), nil)
	if err := e.Wait(ctx, initializer.ID); err != nil {
		t.Fatal(err)
	}
	mysql := create("mysql", "mysql:8.4.11", nil, []string{"MYSQL_DATABASE=fc_fleet_0", "MYSQL_USER=media_admin", "MYSQL_PASSWORD_FILE=/run/frontiercloud-secrets/mysql_password", "MYSQL_ROOT_PASSWORD_FILE=/run/frontiercloud-secrets/mysql_root_password"}, []string{commonSecrets + ":/run/frontiercloud-secrets:ro"}, "", sharedEndpoint("mysql"), []string{"CMD-SHELL", "MYSQL_PWD=$(cat /run/frontiercloud-secrets/mysql_password) mysql -h 127.0.0.1 -u media_admin -e 'SELECT 1' fc_fleet_0"})
	if err := e.Healthy(ctx, mysql.ID); err != nil {
		t.Fatal("private MySQL initialization", err)
	}
	var ddl strings.Builder
	for i := 1; i < fleetNodeCount; i++ {
		ddl.WriteString("CREATE DATABASE fc_fleet_" + big.NewInt(int64(i)).String() + "; GRANT ALL ON fc_fleet_" + big.NewInt(int64(i)).String() + ".* TO 'media_admin'@'%';")
	}
	if err := e.Exec(ctx, mysql.ID, []string{"sh", "-c", "MYSQL_PWD=$(cat /run/frontiercloud-secrets/mysql_root_password) mysql -h 127.0.0.1 -uroot -e \"" + ddl.String() + "\""}); err != nil {
		t.Fatal("private schemas", err)
	}
	redisContainer := create("redis", "redis:7.4.11-alpine", []string{"redis-server", "--appendonly", "yes"}, nil, nil, "", sharedEndpoint("redis"), nil)
	password, err := os.ReadFile(filepath.Join(commonSecrets, "mysql_password"))
	if err != nil {
		t.Fatal(err)
	}
	sites := make([]*fleetSite, fleetNodeCount)
	for i := range sites {
		runtime, database := "go", "sqlite"
		if i == 0 {
			runtime, database, _ = strings.Cut(kind, "-")
		} else {
			choices := []string{"go-mysql", "go-sqlite"}
			runtime, database, _ = strings.Cut(choices[(i-1)%len(choices)], "-")
		}
		label := "site" + big.NewInt(int64(i)).String()
		folder := filepath.Join(root, label)
		if err := os.Mkdir(folder, 0755); err != nil {
			t.Fatal(err)
		}
		for _, child := range []string{"data", "secrets", "maintenance", "updater-control"} {
			if err := os.Mkdir(filepath.Join(folder, child), 0755); err != nil {
				t.Fatal(err)
			}
		}
		domain := label + ".fleet.invalid"
		privateName := project + "-" + label
		newNetwork(privateName)
		env := []string{"DB_TYPE=" + database, "DATA_ROOT=/app/data", "SQLITE_PATH=/app/data/frontiercloud.db", "MYSQL_HOST=mysql", "MYSQL_DATABASE=fc_fleet_" + big.NewInt(int64(i)).String(), "MYSQL_USER=media_admin", "REDIS_URL=redis://redis:6379/" + big.NewInt(int64(i)).String(), "TLS_ENABLED=true", "SERVER_NAME=" + domain, "SSL_CERT_FILE=/certs/ca.pem"}
		// Docker may allocate 192.168/10.x after its 172.16/12 pool fills.
		// Trust only this node's actual private Nginx/Web network, not every
		// private network or client-supplied forwarding header.
		env = append(env, "TRUSTED_PROXY_NETWORKS="+strings.Join(networkCIDRs[privateName], ","))
		binds := []string{filepath.Join(folder, "data") + ":/app/data", filepath.Join(folder, "secrets") + ":/run/frontiercloud-secrets", certs + ":/certs:ro"}
		if whole {
			branch, source := "gin_main", "gin_dev"
			env = append(env, "RELEASE_BRANCH="+branch, "RELEASE_SOURCE_BRANCH="+source, "RELEASE_MANIFEST_PATH=/release/current.json")
			binds = append(binds, joint.metadata+":/release:ro")
			checkout := filepath.Join(folder, "source")
			joint.source.clone(t, checkout, branch)
			control := filepath.Join(folder, "updater-control")
			updaterImage, command := agent, []string{"serve"}
			create(label+"-updater", updaterImage, command, []string{"UPDATER_PROJECT=" + privateName, "RELEASE_BRANCH=" + branch, "UPDATER_DATA_DIRECTORY=/data", "SSL_CERT_FILE=/certs/ca.pem"}, []string{checkout + ":/workspace:rw", joint.source.origin + ":" + joint.source.origin + ":ro", "/var/run/docker.sock:/var/run/docker.sock", certs + ":/certs:ro", control + ":/run/frontiercloud-updater", filepath.Join(folder, "maintenance") + ":/run/frontiercloud-maintenance", filepath.Join(folder, "data") + ":/data"}, "0:0", sharedEndpoint(label+"-updater"), nil)
			binds = append(binds, control+":/run/frontiercloud-updater:ro")
		} else {
			control := filepath.Join(folder, "updater-control")
			create(label+"-updater", agent, []string{"serve"}, []string{"UPDATER_PROJECT=" + project, "RELEASE_BRANCH=gin_main", "UPDATER_DATA_DIRECTORY=/data"}, []string{control + ":/run/frontiercloud-updater", filepath.Join(folder, "maintenance") + ":/run/frontiercloud-maintenance", filepath.Join(folder, "data") + ":/data"}, "0:0", sharedEndpoint(label+"-updater"), nil)
			binds = append(binds, control+":/run/frontiercloud-updater:ro")
		}
		for _, command := range []string{"init-secrets", "init-media"} {
			initImage, initCommand := native, []string{command}
			init := create(label+"-"+command, initImage, initCommand, env, binds, "0:0", sharedEndpoint(label+"-"+command), nil)
			if err := e.Wait(ctx, init.ID); err != nil {
				t.Fatal(label, command, err)
			}
		}
		if err := os.WriteFile(filepath.Join(folder, "secrets", "mysql_password"), password, 0600); err != nil {
			t.Fatal(err)
		}
		image, command, health := native, []string{"serve"}, []string{"CMD", "/app/frontiercloud", "healthcheck"}
		webEndpoints := sharedEndpoint(label + "-web")
		webEndpoints[privateName] = map[string]any{"Aliases": []string{"web"}}
		web := create(label+"-web", image, command, env, binds, "10001:10001", webEndpoints, health)
		if err := e.Healthy(ctx, web.ID); err != nil {
			response, logErr := e.request(ctx, "GET", "/containers/"+web.ID+"/logs?stdout=true&stderr=true&tail=35", nil, "")
			if logErr == nil {
				raw, _ := io.ReadAll(io.LimitReader(response.Body, 8192))
				response.Body.Close()
				for _, line := range strings.Split(string(raw), "\n") {
					lower := strings.ToLower(line)
					if strings.Contains(lower, "password") || strings.Contains(lower, "credential") || strings.Contains(lower, "private_key") || strings.Contains(lower, "token") {
						continue
					}
					t.Logf("private Web diagnostic %q", line)
				}
			}
			t.Fatal(label, runtime, database, "Web startup", err)
		}
		edgeEndpoints := sharedEndpoint(domain)
		edgeImage := edge
		edgeEndpoints[privateName] = map[string]any{"Aliases": []string{"edge"}}
		create(label+"-edge", edgeImage, nil, []string{"TLS_ENABLED=true", "SERVER_NAME=" + domain, "INSTANCE_NAME=native matrix-fixture", "UPLOAD_INACTIVITY_TIMEOUT=300", "NGINX_ENVSUBST_OUTPUT_DIR=/etc/nginx"}, []string{filepath.Join(folder, "data") + ":/app/data:ro", filepath.Join(folder, "maintenance") + ":/run/frontiercloud-maintenance:ro", certs + ":/etc/nginx/certs:ro", filepath.Join(certs, "ca.pem") + ":/etc/ssl/certs/ca-certificates.crt:ro"}, "", edgeEndpoints, nil)
		jar, _ := cookiejar.New(nil)
		client := &http.Client{Jar: jar, Timeout: 15 * time.Second, Transport: &http.Transport{Proxy: nil, TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: roots}}, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
		sites[i] = &fleetSite{root: folder, project: privateName, origin: "https://" + domain, runtime: runtime, database: database, client: client, environment: env, binds: binds, web: web.ID}
		t.Cleanup(func() { client.CloseIdleConnections() })
		key, err := os.ReadFile(filepath.Join(folder, "secrets", "admin_key"))
		if err != nil {
			t.Fatal(err)
		}
		deadline := time.Now().Add(30 * time.Second)
		for {
			code, _, err := sites[i].request(ctx, "POST", "/api/v1/media/admin/elevate", url.Values{"token": {strings.TrimSpace(string(key))}})
			if err == nil && code == 200 {
				break
			}
			if time.Now().After(deadline) {
				t.Fatal(label, "TLS/Admin startup", code, err)
			}
			time.Sleep(time.Second)
		}
	}
	perform := func(site *fleetSite, method, path string, value any) map[string]any {
		t.Helper()
		code, out, err := site.request(ctx, method, "/api/v1/media/admin"+path, value)
		if err != nil || code != 200 {
			// Only the bounded public error detail, never pair packages/status rows.
			detail, _ := out["detail"].(string)
			if len(detail) > 256 || strings.ContainsAny(detail, "\r\n") || strings.Contains(strings.ToLower(detail), "token") || strings.Contains(strings.ToLower(detail), "credential") || strings.Contains(strings.ToLower(detail), "password") {
				detail = "redacted"
			}
			t.Fatal(site.runtime, site.database, method, path, code, err, detail)
		}
		return out
	}
	master := sites[0]
	perform(master, "POST", "/nodes/promote", map[string]any{"role": "Master", "endpoint": master.origin, "local_capacity_gib": 1})
	relations := make([]string, fleetNodeCount-1)
	for i, follower := range sites[1:] {
		t.Log("pairing private follower", i+1, follower.runtime, follower.database)
		perform(follower, "POST", "/nodes/promote", map[string]any{"role": "Follower", "endpoint": follower.origin})
		pkg := perform(follower, "POST", "/nodes/pair-package", map[string]any{})
		relation := perform(master, "POST", "/nodes/pair", map[string]any{"package": pkg})["relationship_id"].(string)
		relations[i] = relation
		mode := "Relay"
		if i < 2 {
			mode = "Direct"
		}
		perform(master, "POST", "/nodes/"+relation+"/mode", map[string]any{"mode": mode})
		perform(master, "POST", "/nodes/"+relation+"/resources", map[string]any{"storage_enabled": true, "storage_capacity_gib": 1, "backup_enabled": false})
	}
	deadline := time.Now().Add(90 * time.Second)
	for {
		status := perform(master, "GET", "/nodes", nil)
		rows, ok := status["relationships"].([]any)
		ready := ok && len(rows) == fleetNodeCount-1
		for _, raw := range rows {
			row := raw.(map[string]any)
			ready = ready && row["state"] == "active" && row["status"] == "online"
		}
		if ready {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("four active native matrix followers did not establish outbound heartbeat")
		}
		time.Sleep(time.Second)
	}
	t.Log("real private-CA native matrix fleet pairing/confirmation/configuration/heartbeat passed", kind, "2 Direct / 2 Relay")
	// Exercise both native database profiles, including private key/credential
	// rotation and durable revocation receipt before an independent new pair.
	for _, index := range []int{1, 2} {
		follower, old := sites[index], relations[index-1]
		perform(master, "POST", "/nodes/"+old+"/revoke", nil)
		deadline := time.Now().Add(60 * time.Second)
		for {
			status := perform(follower, "GET", "/nodes", nil)
			rows, _ := status["relationships"].([]any)
			// Public Admin status omits revoked tombstones. Absence, or an
			// explicitly revoked row, is followed by the new-pair admission
			// check that requires the durable old revocation receipt.
			revoked := true
			for _, raw := range rows {
				row := raw.(map[string]any)
				if row["relationship_id"] == old && row["state"] != "revoked" {
					revoked = false
				}
			}
			if revoked {
				break
			}
			if time.Now().After(deadline) {
				t.Fatal("native matrix peer did not acknowledge revocation", follower.runtime, follower.database)
			}
			time.Sleep(time.Second)
		}
		pkg := perform(follower, "POST", "/nodes/pair-package", map[string]any{})
		fresh := perform(master, "POST", "/nodes/pair", map[string]any{"package": pkg})["relationship_id"].(string)
		if fresh == old {
			t.Fatal("re-pair reused revoked relationship")
		}
		relations[index-1] = fresh
		perform(master, "POST", "/nodes/"+fresh+"/mode", map[string]any{"mode": "Direct"})
		perform(master, "POST", "/nodes/"+fresh+"/resources", map[string]any{"storage_enabled": true, "storage_capacity_gib": 1, "backup_enabled": false})
	}
	t.Log("real native matrix SQLite/MySQL revocation and re-pair passed", kind)
	// This is business/control acceptance, not fleet release execution. Native
	// sites use actual compiled idle agents on their private control sockets.
	// The normal audited action must not defeat unknown agents or offline fences.
	for _, site := range sites {
		perform(site, "POST", "/site/maintenance", map[string]any{"enabled": false})
	}
	deadline = time.Now().Add(60 * time.Second)
	for {
		rows := perform(master, "GET", "/nodes", nil)["relationships"].([]any)
		active := 0
		for _, raw := range rows {
			row := raw.(map[string]any)
			if row["state"] == "active" && row["status"] == "online" {
				active++
			}
		}
		if active == fleetNodeCount-1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("re-paired fleet did not recover")
		}
		time.Sleep(time.Second)
	}
	placements := map[string]bool{}
	var mediaPaths []string
	for i, siteType := range []string{"primary", "direct", "direct", "relay", "relay"} {
		t.Log("checking real native matrix media transport", i, siteType)
		category := "music/混合验收-" + big.NewInt(int64(i)).String()
		mediaPaths = append(mediaPaths, category+"/曲目.mp3")
		payload := []byte("ID3disposable-native matrix-media-payload")
		ticket := perform(master, "POST", "/upload/session", map[string]any{"site_type": siteType, "target_dir": category, "filename": "曲目.mp3", "size_bytes": len(payload)})
		member, _ := ticket["member_id"].(string)
		placements[member] = true
		path, _ := ticket["upload_url"].(string)
		target := master
		if strings.HasPrefix(path, "https://") {
			u, err := url.Parse(path)
			if err != nil {
				t.Fatal("invalid private upload URL")
			}
			target = nil
			for _, site := range sites {
				if u.Scheme+"://"+u.Host == site.origin {
					target = site
					break
				}
			}
			if target == nil {
				t.Fatal("upload left private fleet")
			}
			path = u.RequestURI()
		}
		code, _, err := target.request(ctx, "PUT", path, payload)
		if err != nil || code != 200 {
			t.Fatal("real native matrix upload", siteType, target.runtime, target.database, code, err)
		}
		id, _ := ticket["upload_id"].(string)
		if siteType == "direct" {
			perform(master, "POST", "/upload/session/"+id+"/finalize", nil)
		}
		code, catalog, err := master.request(ctx, "GET", "/api/v1/media/catalog/media?"+url.Values{"media_type": {"music"}, "path": {category}, "playback_session_id": {"private-fleet-session"}}.Encode(), nil)
		entries, ok := catalog["entries"].([]any)
		if err != nil || code != 200 || !ok || len(entries) != 1 || entries[0].(map[string]any)["resource_id"] != ticket["media_id"] {
			t.Fatal("real native matrix catalog lost placement", siteType, code, err)
		}
		track := entries[0].(map[string]any)
		stream, _ := track["url"].(string)
		resource, _ := ticket["media_id"].(string)
		fleetRange(t, ctx, sites, stream, resource, payload)
	}
	if len(placements) != fleetNodeCount {
		t.Fatal("fair native matrix placement did not cover all five stores", len(placements))
	}
	t.Log("real native matrix Primary/Direct/Relay uploads, global catalog and byte ranges passed", kind, "all five stores")
	account := func(method, path string, value any) map[string]any {
		t.Helper()
		code, out, err := master.request(ctx, method, "/api/v1/karaoke/account"+path, value)
		if err != nil || code != 200 {
			t.Fatal("real native matrix account/recording action", method, path, code, err)
		}
		return out
	}
	challenge, _ := account("GET", "/captcha", nil)["challenge"].(string)
	// Read only this disposable challenge, never log answer/session/password.
	cache := redis.NewClient(&redis.Options{Addr: "redis:6379", DB: 0})
	defer cache.Close()
	answer, err := cache.Get(ctx, "karaoke:captcha:"+challenge+":image").Result()
	if err != nil {
		t.Fatal("private fixture challenge missing")
	}
	account("POST", "/register", map[string]any{"username": "混合验收账号", "password": "Disposable-Fleet@123", "challenge": challenge, "captcha": answer})
	if code, _, err := master.request(ctx, "POST", "/api/v1/karaoke/account/recordings/ticket", map[string]any{"size_bytes": 45, "content_type": "audio/webm"}, map[string]string{"X-Karaoke-CSRF": ""}); err != nil || code != 403 {
		t.Fatal("real recording mutation accepted missing CSRF", code, err)
	}
	var recordings []string
	for i := 0; i < fleetNodeCount; i++ {
		t.Log("checking real native matrix recording transport", i)
		payload := []byte("disposable native matrix recording payload")
		ticket := account("POST", "/recordings/ticket", map[string]any{"size_bytes": len(payload), "content_type": "audio/webm", "title": "混合录音"})
		id, _ := ticket["recording_id"].(string)
		path, _ := ticket["upload_url"].(string)
		target := master
		if strings.HasPrefix(path, "https://") {
			u, err := url.Parse(path)
			if err != nil {
				t.Fatal("invalid private recording URL")
			}
			target = nil
			for _, site := range sites {
				if u.Scheme+"://"+u.Host == site.origin {
					target = site
					break
				}
			}
			if target == nil {
				t.Fatal("recording left private fleet")
			}
			path = u.RequestURI()
		}
		headers := map[string]string{"Origin": master.origin, "Content-Type": "audio/webm"}
		if token, ok := ticket["capability"].(string); ok {
			headers["X-Recording-Capability"] = token
		}
		code, _, err := target.request(ctx, "PUT", path, payload, headers)
		if err != nil || code != 200 {
			t.Fatal("real native matrix recording upload", target.runtime, target.database, code, err)
		}
		account("POST", "/recordings/"+id+"/finalize", nil)
		fleetRange(t, ctx, sites, "/api/v1/karaoke/account/recordings/"+id+"/stream", "", payload)
		recordings = append(recordings, id)
	}
	items, _ := account("GET", "/recordings", nil)["items"].([]any)
	if len(items) != fleetNodeCount {
		t.Fatal("real native matrix recording publication lost items", len(items))
	}
	unauthenticated := *master.client
	unauthenticated.Jar = nil
	r, err := http.NewRequestWithContext(ctx, "GET", master.origin+"/api/v1/karaoke/account/recordings/"+recordings[0]+"/stream", nil)
	if err != nil {
		t.Fatal(err)
	}
	anonymous, err := unauthenticated.Do(r)
	if err != nil {
		t.Fatal("anonymous private recording transport failed")
	}
	anonymous.Body.Close()
	if anonymous.StatusCode != 401 {
		t.Fatal("private recording bytes exposed without an account", anonymous.StatusCode)
	}
	masterID, _ := perform(master, "GET", "/nodes", nil)["node_id"].(string)
	for _, index := range []int{1, 2} {
		perform(master, "POST", "/nodes/"+relations[index-1]+"/resources", map[string]any{"storage_enabled": true, "storage_capacity_gib": 1, "backup_enabled": true})
	}
	deadline = time.Now().Add(90 * time.Second)
	generations := map[string]string{}
	for {
		pool := perform(master, "GET", "/storage-pool", nil)
		members, _ := pool["members"].([]any)
		for _, raw := range members {
			member := raw.(map[string]any)
			backup, _ := member["backup"].(map[string]any)
			generation, ok := backup["generation"].(json.Number)
			checksum, _ := backup["checksum"].(string)
			if ok && generation.String() != "0" && backup["state"] == "ready" && len(checksum) == 64 {
				id, _ := member["member_id"].(string)
				generations[id] = generation.String()
			}
		}
		if len(generations) == 2 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("real native matrix cold backup did not converge", len(generations))
		}
		time.Sleep(time.Second)
	}
	for _, index := range []int{1, 2} {
		follower := sites[index]
		id, _ := perform(follower, "GET", "/nodes", nil)["node_id"].(string)
		generation, ok := generations[id]
		if !ok {
			t.Fatal("native matrix backup landed on wrong member")
		}
		binds := append([]string{}, follower.binds...)
		for i := range binds {
			if strings.HasSuffix(binds[i], ":/app/data") {
				binds[i] += ":ro"
			}
		}
		check := create("backup-proof-site"+big.NewInt(int64(index)).String(), native, []string{"verify-backup", "--master-id", masterID, "--generation", generation, "--scratch-dir", "/tmp/backup-proof"}, follower.environment, binds, "10001:10001", sharedEndpoint("backup-proof-site"+big.NewInt(int64(index)).String()), nil)
		if err := e.Wait(ctx, check.ID); err != nil {
			if logs, logErr := e.request(ctx, "GET", "/containers/"+check.ID+"/logs?stdout=true&stderr=true&tail=10", nil, ""); logErr == nil {
				raw, _ := io.ReadAll(io.LimitReader(logs.Body, 4096))
				logs.Body.Close()
				// Extract fixed public classifications only, never row contents,
				// credentials, database connection strings or private IDs.
				classification := regexp.MustCompile(`invalid or incomplete business backup(?:: (?:record [0-9]+|[a-z-]+(?: check failed)?))?|operation not permitted|permission denied|read-only database|no such table: [a-z_]+`).Find(raw)
				if len(classification) > 0 {
					t.Log("private native backup proof classification", string(classification))
				}
			}
			t.Fatal("real native matrix backup failed independent read-only logical preflight", follower.runtime, follower.database, err)
		}
	}
	t.Log("real native matrix cold backups passed transfer and independent logical preflight", kind, "Go/MySQL and Go/SQLite followers")
	if whole {
		joint.execute(t, ctx, e, sites, perform)
		// Prove actual business bytes/session survived both entire releases.
		if retained, _ := account("GET", "/recordings", nil)["items"].([]any); len(retained) != fleetNodeCount {
			t.Fatal("whole release lost recording/session state")
		}
		fleetRange(t, ctx, sites, "/api/v1/karaoke/account/recordings/"+recordings[0]+"/stream", "", []byte("disposable native matrix recording payload"))
	}
	// A follower outage must become visible while the Master remains live; an
	// all-services restart alone would not prove offline admission/recovery.
	followerID, _ := perform(sites[2], "GET", "/nodes", nil)["node_id"].(string)
	if err := e.Stop(ctx, sites[2].web); err != nil {
		t.Fatal("private outage stop failed", err)
	}
	deadline = time.Now().Add(180 * time.Second)
	for {
		rows, _ := perform(master, "GET", "/nodes", nil)["relationships"].([]any)
		offline := false
		for _, raw := range rows {
			row := raw.(map[string]any)
			if row["relationship_id"] == relations[1] {
				offline = row["status"] == "offline"
			}
		}
		if offline {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("real follower outage was not observed by Master")
		}
		time.Sleep(time.Second)
	}
	rows, _ := perform(master, "GET", "/storage-pool", nil)["members"].([]any)
	ineligible := false
	for _, raw := range rows {
		row := raw.(map[string]any)
		if row["member_id"] == followerID {
			ineligible = row["health"] == "offline" && (row["writable"] == false || row["writable"] == json.Number("0"))
		}
	}
	if !ineligible {
		t.Fatal("offline follower remained eligible for new storage")
	}
	// Loss of TLS must not downgrade or reset an established native Follower.
	// The existing service is stopped: this helper cannot race its SQL writer.
	before, err := os.ReadFile(filepath.Join(sites[2].root, "data", ".native-runtime"))
	if err != nil {
		t.Fatal("native startup provenance missing before TLS-loss check")
	}
	withoutTLS := append([]string(nil), sites[2].environment...)
	for i, setting := range withoutTLS {
		if strings.HasPrefix(setting, "TLS_ENABLED=") {
			withoutTLS[i] = "TLS_ENABLED=false"
		}
	}
	rejected := create("tls-loss-check", native, []string{"serve"}, withoutTLS, sites[2].binds, "10001:10001", sharedEndpoint("tls-loss-check"), nil)
	probe, stop := context.WithTimeout(ctx, 20*time.Second)
	if err := e.Wait(probe, rejected.ID); err == nil {
		stop()
		t.Fatal("native fixed-role TLS loss accepted")
	}
	stop()
	failed, err := e.Inspect(ctx, rejected.ID)
	if err != nil || failed.State.Status != "exited" || failed.State.ExitCode == 0 {
		t.Fatal("TLS loss did not fail closed before serving")
	}
	response, err := e.request(ctx, "GET", "/containers/"+rejected.ID+"/logs?stdout=true&stderr=true&tail=5", nil, "")
	if err != nil {
		t.Fatal("TLS-loss classification unavailable")
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, 4096))
	response.Body.Close()
	if err != nil || !bytes.Contains(raw, []byte("fixed Master/Follower identity requires HTTPS")) {
		t.Fatal("native TLS-loss rejection did not match fixed-role contract")
	}
	after, err := os.ReadFile(filepath.Join(sites[2].root, "data", ".native-runtime"))
	if err != nil || !bytes.Equal(before, after) {
		t.Fatal("TLS loss changed established native provenance")
	}
	if err := e.Start(ctx, sites[2].web); err != nil {
		t.Fatal("private outage restart failed", err)
	}
	if err := e.Healthy(ctx, sites[2].web); err != nil {
		t.Fatal("native Follower could not recover its durable role", err)
	}
	if recovered, _ := perform(sites[2], "GET", "/nodes", nil)["node_id"].(string); recovered != followerID {
		t.Fatal("TLS-loss recovery reset native identity")
	}
	deadline = time.Now().Add(90 * time.Second)
	for {
		rows, _ := perform(master, "GET", "/nodes", nil)["relationships"].([]any)
		online := false
		for _, raw := range rows {
			row := raw.(map[string]any)
			if row["relationship_id"] == relations[1] {
				online = row["state"] == "active" && row["status"] == "online"
			}
		}
		if online {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("native follower heartbeat did not recover after real outage")
		}
		time.Sleep(time.Second)
	}
	t.Log("real native matrix Master observed follower outage, excluded offline storage and recovered native role/heartbeat", kind)
	identities := make([]string, len(sites))
	for i, site := range sites {
		identities[i], _ = perform(site, "GET", "/nodes", nil)["node_id"].(string)
		if err := e.Stop(ctx, site.web); err != nil {
			t.Fatal("private Web stop failed", i, err)
		}
	}
	for _, service := range []Container{redisContainer, mysql} {
		if err := e.Stop(ctx, service.ID); err != nil {
			t.Fatal("private state-service stop failed", err)
		}
		if err := e.Start(ctx, service.ID); err != nil {
			t.Fatal("private state-service restart failed", err)
		}
	}
	if err := e.Healthy(ctx, mysql.ID); err != nil {
		t.Fatal("private MySQL restart not healthy", err)
	}
	for i, site := range sites {
		if err := e.Start(ctx, site.web); err != nil {
			t.Fatal("private Web restart failed", i, err)
		}
		if err := e.Healthy(ctx, site.web); err != nil {
			t.Fatal("private durable-role Web restart not healthy", i, site.runtime, site.database, err)
		}
		if id, _ := perform(site, "GET", "/nodes", nil)["node_id"].(string); id == "" || id != identities[i] {
			t.Fatal("durable identity changed after real restart", i)
		}
	}
	deadline = time.Now().Add(90 * time.Second)
	for {
		rows, _ := perform(master, "GET", "/nodes", nil)["relationships"].([]any)
		online := len(rows) == fleetNodeCount-1
		for _, raw := range rows {
			row := raw.(map[string]any)
			online = online && row["state"] == "active" && row["status"] == "online"
		}
		if online {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("native matrix heartbeat did not recover after Web/Redis/MySQL restart")
		}
		time.Sleep(time.Second)
	}
	items, _ = account("GET", "/recordings", nil)["items"].([]any)
	if len(items) != fleetNodeCount {
		t.Fatal("recordings/session lost after real restart")
	}
	fleetRange(t, ctx, sites, "/api/v1/karaoke/account/recordings/"+recordings[0]+"/stream", "", []byte("disposable native matrix recording payload"))
	t.Log("real native matrix durable-role Web, Redis and MySQL restart preserved identity, relationships, sessions and recordings", kind)
	for _, id := range recordings {
		account("DELETE", "/recordings/"+id, nil)
	}
	items, _ = account("GET", "/recordings", nil)["items"].([]any)
	if len(items) != 0 {
		t.Fatal("real native matrix recording deletion did not converge")
	}
	t.Log("real native matrix registration, recording upload/finalize/range/deletion passed", kind)
	deleted := perform(master, "POST", "/delete", map[string]any{"paths": mediaPaths})
	count, ok := deleted["deleted"].(json.Number)
	pending, _ := deleted["pending_delete"].([]any)
	if !ok || count.String() != big.NewInt(fleetNodeCount).String() || len(pending) != 0 {
		t.Fatal("real native matrix media deletion did not converge")
	}
	for _, name := range mediaPaths {
		category := name[:strings.LastIndex(name, "/")]
		code, catalog, err := master.request(ctx, "GET", "/api/v1/media/catalog/media?"+url.Values{"media_type": {"music"}, "path": {category}, "playback_session_id": {"private-fleet-session"}}.Encode(), nil)
		entries, ok := catalog["entries"].([]any)
		if err == nil && code == 404 && catalog["detail"] == "Media category not found" {
			// A virtual global category disappears with its last placement.
			continue
		}
		if err != nil || code != 200 || !ok || len(entries) != 0 {
			t.Fatal("deleted media remained in native matrix global catalog", code, err)
		}
	}
	t.Log("real native matrix all-owner media deletion and catalog invalidation passed", kind)
}
