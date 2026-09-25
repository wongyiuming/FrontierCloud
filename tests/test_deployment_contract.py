import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeploymentContractTests(unittest.TestCase):
    def test_native_karaoke_keeps_recording_local_until_authenticated_upload(self):
        web = (ROOT / "static/js/karaoke.js").read_text(encoding="utf-8")
        page = (ROOT / "static/media/karaoke.html").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        self.assertNotIn("  karaoke:", compose)
        for storage in ("localStorage", "sessionStorage", "indexedDB"):
            self.assertNotIn(storage, web)
        self.assertIn("recordedBlob", web)
        self.assertIn("/recordings/ticket", web)
        self.assertIn("elements.upload.disabled = !authenticated", web)
        self.assertNotIn("重录", web)
        self.assertNotIn("只播放歌曲", page)
        self.assertIn('id="pauseResume"', page)
        self.assertIn('id="upload"', page)
        self.assertIn("createMediaStreamDestination", web)
        self.assertIn("lowPass.connect(recordGain)", web)
        self.assertIn("limiter.connect(recordDestination)", web)
        self.assertIn("lowPass.connect(monitorGain)", web)
        self.assertNotIn("mediaSource.connect(recordGain)", web)
        self.assertNotIn("mediaSource.connect(recordDestination)", web)
        self.assertIn("echoCancellation: elements.aec.checked", web)
        self.assertIn("noiseSuppression: false", web)
        self.assertIn("autoGainControl: false", web)
        self.assertIn('id="inputDevice"', page)
        self.assertIn('id="outputDevice"', page)
        self.assertIn('id="voiceGain" type="range" min="0" max="200" value="100"', page)
        self.assertIn('id="monitorGain" type="range" min="0" max="200" value="100"', page)
        self.assertIn('id="monitor" type="checkbox" checked', page)
        self.assertIn('id="aec" type="checkbox"', page)
        self.assertNotIn('id="aec" type="checkbox" checked', page)
        self.assertIn("Math.min(6", web)
        self.assertIn("if (state.graph)", web)
        self.assertIn("context.createMediaElementSource(elements.media)", web)
        self.assertIn("state.graph = graph", web)
        self.assertIn("录音继续，但媒体播放失败", web)
        self.assertIn("state.audioContext.setSinkId", web)
        self.assertFalse((ROOT / "karaoke").exists())
        self.assertFalse((ROOT / "contracts/karaoke-openapi.json").exists())

    def test_hashed_static_assets_are_immutable(self):
        nginx = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
        admin = (ROOT / "static/media/admin.html").read_text(encoding="utf-8")
        self.assertIn('"~^[0-9a-f]{16}$" "public, max-age=31536000, immutable"', nginx)
        for asset in ("admin.js", "admin.css", "player.js", "player.css", "network-observation.js", "karaoke.js", "karaoke.css"):
            line = next(value for value in nginx.splitlines() if f"/{asset}" in value)
            self.assertIn("$versioned_static_cache_control", line)
            self.assertNotIn("no-store", line)
        self.assertIn("{{ADMIN_CSS_URL}}", admin)
        self.assertIn("{{ADMIN_JS_URL}}", admin)
        self.assertIn("{{NODES_JS_URL}}", admin)

    def test_karaoke_and_hidden_home_gestures_match_the_product_contract(self):
        player = (ROOT / "static/js/player.js").read_text(encoding="utf-8")
        home = (ROOT / "static/media/index.html").read_text(encoding="utf-8")
        self.assertIn("event.touches.length !== 3", player)
        self.assertIn("}, 1500)", player)
        self.assertIn("media: media.karaoke_id", player)
        self.assertNotIn("media_path: media.media_path", player[player.index("function karaokeUrl"):player.index("function openKaraoke")])
        self.assertIn("count===5", home)
        self.assertIn("now-started>1800", home)
        self.assertIn('id="refreshHotspot"', home)
        self.assertIn('id="elevateHotspot"', home)
        self.assertNotIn(">提权</button>", home)
        self.assertNotIn("↻ 刷新界面", home)

    def test_runtime_dependencies_are_reproducibly_pinned(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        unpinned = [dependency for dependency in project["dependencies"] if "==" not in dependency]
        self.assertEqual(unpinned, [])

    def test_environment_example_is_the_complete_public_contract(self):
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        config = (ROOT / "app/core/config.py").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        listed = re.findall(r"(?m)^#? ?([A-Z][A-Z0-9_]*)=", env_example)
        active = {
            line.split("=", 1)[0]
            for line in env_example.splitlines()
            if line and not line.startswith("#") and "=" in line
        }
        self.assertEqual(active, set())
        self.assertEqual(len(listed), len(set(listed)))
        lines = env_example.splitlines()
        for index, line in enumerate(lines):
            if re.match(r"^# [A-Z][A-Z0-9_]*=", line):
                self.assertGreater(index, 0)
                self.assertTrue(lines[index - 1].startswith("# "))
                self.assertNotRegex(lines[index - 1], r"^# [A-Z][A-Z0-9_]*=")
        app_names = set(re.findall(r'validation_alias="([A-Z][A-Z0-9_]*)"', config))
        compose_names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose))
        self.assertEqual(set(listed), app_names | compose_names)

    def test_private_deployment_variables_are_absent_from_contract(self):
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        for name in (
            "PROMETHEUS_URL", "GRAFANA_URL", "ELASTICSEARCH_URL", "LOGSTASH_HOST",
            "KIBANA_URL", "RN_HOST", "DMIT_HOST", "WG_ENDPOINT",
            "PRODUCTION_HOST", "STAGING_HOST", "ADMIN_BOOTSTRAP_TOKEN",
            "METRICS_TOKEN",
        ):
            self.assertNotIn(f"{name}=", env_example)

    def test_observability_platform_lifecycle_is_not_bundled(self):
        self.assertFalse(any(
            path.is_file() and path.suffix != ".pyc"
            for path in (ROOT / "monitoring").rglob("*")
        ))
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8").lower()
        workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8").lower()
        for marker in (
            "prometheus", "grafana", "elasticsearch", "logstash", "kibana",
            "node_exporter", "cadvisor", "redis_exporter", "mysql_exporter",
            "nginx_exporter", "monitoring", "weekly_reporter",
        ):
            self.assertNotIn(marker, compose)
            self.assertNotIn(marker, workflow)

    def test_transport_and_upload_timeout_are_direct_technical_settings(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        nginx = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
        self.assertNotIn("ENVIRONMENT", compose)
        self.assertIn("TLS_ENABLED: ${TLS_ENABLED:-false}", compose)
        self.assertIn("UPLOAD_INACTIVITY_TIMEOUT: ${ADMIN_UPLOAD_INACTIVITY_TIMEOUT:-300}", compose)
        self.assertIn("client_body_timeout ${UPLOAD_INACTIVITY_TIMEOUT}s", nginx)

    def test_public_bind_defaults_to_ipv4_and_requires_an_explicit_ipv6_address(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        selector = (ROOT / "nginx/15-select-tls.sh").read_text(encoding="utf-8")
        self.assertGreaterEqual(compose.count("host_ip: ${PUBLIC_BIND_ADDRESS:-0.0.0.0}"), 4)
        self.assertIn("PUBLIC_BIND_ADDRESS: ${PUBLIC_BIND_ADDRESS:-0.0.0.0}", compose)
        self.assertIn('public_bind_address=${PUBLIC_BIND_ADDRESS:-0.0.0.0}', selector)
        self.assertIn('::) ipv6_enabled=true', selector)
        self.assertIn("# IPV6 /\\1/", selector)
        for path in (ROOT / "nginx/transport").rglob("*.conf"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "listen [::]" in line:
                    self.assertTrue(line.lstrip().startswith("# IPV6 "), path)

    def test_nginx_selects_transport_without_deployment_tiers(self):
        selector = (ROOT / "nginx/15-select-tls.sh").read_text(encoding="utf-8")
        self.assertIn('${TLS_ENABLED:-false}', selector)
        self.assertIn("nginx_mode=https", selector)
        self.assertIn("nginx_mode=http", selector)
        self.assertIn('/etc/nginx/transport/$nginx_mode', selector)
        for transport in ("http", "https"):
            for name in ("extra-servers.conf", "public-listen.conf", "public-tls.conf"):
                self.assertTrue((ROOT / "nginx/transport" / transport / name).is_file())
        http = (ROOT / "nginx/transport/http/public-listen.conf").read_text(encoding="utf-8")
        https = (ROOT / "nginx/transport/https/public-listen.conf").read_text(encoding="utf-8")
        self.assertIn("listen 80 default_server", http)
        self.assertIn("listen 443 ssl", https)
        nginx = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
        self.assertIn("/etc/nginx/runtime/extra-servers.conf", nginx)

    def test_runtime_and_readme_do_not_define_deployment_tiers(self):
        paths = [ROOT / "README.md", ROOT / "app/core/config.py", ROOT / "app/api/v1/admin.py"]
        paths.extend(path for path in (ROOT / "nginx").rglob("*") if path.is_file())
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertNotRegex(
                    path.read_text(encoding="utf-8"),
                    r"(?i)\b(?:production|preproduction|staging|development)\b|(?:生产|预发布|测试|开发)环境",
                )

    def test_nginx_limits_large_bodies_to_upload_routes_and_sets_security_headers(self):
        nginx = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
        headers = (ROOT / "nginx/security-headers.conf").read_text(encoding="utf-8")
        dockerfile = (ROOT / "nginx/Dockerfile").read_text(encoding="utf-8")
        self.assertEqual(nginx.count("client_max_body_size 8705M"), 2)
        self.assertIn("location ^~ /internal/v1/storage/", nginx)
        self.assertIn("location /api/v1/media/admin/upload/", nginx)
        self.assertIn("client_max_body_size 64k", nginx)
        self.assertIn("X-Content-Type-Options", headers)
        self.assertIn("X-Frame-Options", headers)
        self.assertIn("Strict-Transport-Security", headers)
        self.assertIn("COPY nginx/security-headers.conf", dockerfile)

    def test_karaoke_recording_collection_does_not_redirect_at_the_proxy_boundary(self):
        nginx = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
        self.assertIn("location ^~ /api/v1/karaoke/account/recordings {", nginx)
        self.assertNotIn("location ^~ /api/v1/karaoke/account/recordings/ {", nginx)

    def test_media_storage_is_initialized_before_the_unprivileged_web_service(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        initializer = (ROOT / "app/services/media_storage_init.py").read_text(encoding="utf-8")
        federation_harness = (ROOT / "tests/federation_stack.py").read_text(encoding="utf-8")
        self.assertIn("media-init:", compose)
        self.assertIn('command: ["python", "-m", "app.services.media_storage_init"]', compose)
        self.assertIn("media-init: {condition: service_completed_successfully}", compose)
        for directory in ("media/music", "media/vido", "media/lyrics"):
            self.assertIn(f'"{directory}"', initializer)
        self.assertIn("APP_UID = 10001", initializer)
        self.assertIn("followlinks=False", initializer)
        self.assertIn('(\"web\", \"secrets-init\", \"media-init\")', federation_harness)

    def test_chinese_product_name_is_consistent(self):
        paths = [ROOT / "app", ROOT / "static"]
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for root in paths
            for path in root.rglob("*")
            if path.is_file() and path.suffix in {".py", ".js", ".html", ".css"}
        )
        self.assertNotIn("前沿" + "视界", source)
        self.assertIn("前沿娱乐", source)

    def test_nginx_emits_structured_logs_without_a_log_directory(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        nginx = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
        self.assertIn("access_log /dev/stdout structured if=$access_loggable", nginx)
        self.assertIn("~^/health(?:/|$) 1", nginx)
        self.assertIn("=/metrics 1", nginx)
        self.assertIn('map "$quiet_success_path:$status" $access_loggable', nginx)
        self.assertIn('"~^1:2[0-9][0-9]$" 0', nginx)
        self.assertIn("error_log /dev/stderr warn", nginx)
        self.assertIn('"request_id"', nginx)
        self.assertIn('"trace_id"', nginx)
        self.assertNotIn("/var/log/nginx", nginx + compose)

    def test_edge_logs_verified_media_identity_and_transfer_results_without_capabilities(self):
        nginx = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
        log_format = nginx.split("log_format structured", 1)[1].split("access_log", 1)[0]
        for field in ("media_resource_id", "media_owner_id", "media_object_id", "parent_request_id",
                      "range", "content_range", "bytes_sent", "status", "request_completion"):
            self.assertIn('"' + field + '"', log_format)
        self.assertIn("map $request_uri $logged_path", nginx)
        self.assertNotIn("$request_uri", log_format)
        self.assertNotIn("$relay_token", log_format)
        self.assertNotIn("$args", log_format)
        self.assertIn("proxy_set_header X-Media-Capability $relay_token", nginx)
        self.assertNotIn("?token=$relay_token", nginx)

    def test_nginx_uses_a_current_patched_stable_image(self):
        dockerfile = (ROOT / "nginx/Dockerfile").read_text(encoding="utf-8")
        self.assertTrue(dockerfile.startswith("FROM nginx:1.30.4-alpine\n"))

    def test_duplicate_uvicorn_access_log_is_disabled(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        logging_config = (ROOT / "app/core/logging_config.py").read_text(encoding="utf-8")
        self.assertIn("--no-access-log", dockerfile)
        self.assertIn('logging.getLogger("uvicorn.access")', logging_config)
        self.assertIn("access_logger.disabled = True", logging_config)

    def test_runtime_secret_initializer_has_no_legacy_environment_inputs(self):
        initializer = (ROOT / "app/services/runtime_secrets.py").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        for obsolete in (
            "ADMIN_BOOTSTRAP_TOKEN", "MYSQL_PASSWORD=", "MYSQL_ROOT_PASSWORD=",
            "MYSQL_URL=", "WEBRTC_STUN_URLS=", "SECURITY_AUTO_BAN_TTL=",
            "METRICS_TOKEN=",
        ):
            self.assertNotIn(obsolete, initializer + compose)

    def test_runtime_secret_initializer_never_logs_secret_values(self):
        initializer = (ROOT / "app/services/runtime_secrets.py").read_text(encoding="utf-8")
        self.assertIn('"secret_names": sorted(names)', initializer)
        self.assertNotIn('"context": {', initializer)
        self.assertNotIn("managed[name].read_text(encoding=\"utf-8\").strip()", initializer)

    def test_env_contract_validator_rejects_unknown_names_without_printing_values(self):
        validator = ROOT / "scripts/validate_env_contract.py"
        with tempfile.TemporaryDirectory() as directory:
            temp_root = Path(directory)
            example = temp_root / ".env.example"
            env_file = temp_root / ".env"
            example.write_text("# Supported switch.\n# TLS_ENABLED=false\n", encoding="utf-8")
            env_file.write_text(
                "TLS_ENABLED=true\nENVIRONMENT=test\nMETRICS_BASIC_PASSWORD=do_not_print_me\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(validator), "--env-file", str(env_file),
                 "--example-file", str(example)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ENVIRONMENT", result.stdout)
        self.assertIn("METRICS_BASIC_PASSWORD", result.stdout)
        self.assertNotIn("do_not_print_me", result.stdout + result.stderr)

    def test_env_contract_validator_accepts_only_formal_variables(self):
        validator = ROOT / "scripts/validate_env_contract.py"
        result = subprocess.run(
            [sys.executable, str(validator), "--env-file", str(ROOT / ".env.example"),
             "--example-file", str(ROOT / ".env.example")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_readme_documents_runtime_secret_recovery_after_web_recreation(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Startup logs list newly created secret names without printing values", readme)
        self.assertIn(
            "docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/admin_key'",
            readme,
        )
        self.assertIn(
            "docker compose exec -T web sh -c 'cat /run/frontiercloud-secrets/metrics_token'",
            readme,
        )
        runtime = (ROOT / "app" / "services" / "runtime_secrets.py").read_text(encoding="utf-8")
        self.assertNotIn('"admin_key": secrets.admin_key', runtime)
        self.assertNotIn('"metrics_token": secrets.metrics_token', runtime)
        self.assertIn("persistent `runtime_secrets` volume", readme)
        self.assertIn("Rapidly click the second half of the home logo five times", readme)

    def test_web_release_control_is_isolated_and_never_uses_compose(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        updater = (ROOT / "updater/server.py").read_text(encoding="utf-8")
        release = (ROOT / "app/services/release_control.py").read_text(encoding="utf-8")
        nodes = (ROOT / "static/js/nodes.js").read_text(encoding="utf-8")
        gate = (ROOT / "nginx/maintenance-gate.conf").read_text(encoding="utf-8")
        for marker in (
            "  prepare-rn:", "  deploy-rn:", "  prepare-evoxt:", "  deploy-evoxt:",
            "self-hosted", "RN_DEPLOY_PATH", "EVOXT_DEPLOY_PATH", "deployments: write",
        ):
            self.assertNotIn(marker, workflow)
        self.assertFalse((ROOT / "scripts/deploy_rn.sh").exists())
        self.assertIn("  updater:", compose)
        self.assertEqual(compose.count("- /var/run/docker.sock:/var/run/docker.sock"), 1)
        self.assertIn("updater_control:/run/frontiercloud-updater", compose)
        self.assertIn("maintenance_state:/run/frontiercloud-maintenance:ro", compose)
        self.assertIn("docker.DockerClient", updater)
        self.assertNotIn("docker compose", updater.lower())
        self.assertNotIn("systemctl", updater.lower())
        self.assertIn("actions/workflows/docker.yml/runs", release)
        self.assertIn("升级并分发", nodes)
        self.assertIn("一键回滚", nodes)
        self.assertIn("/internal/v1/cluster-update", gate)
        self.assertIn("workflow_dispatch:", workflow)


if __name__ == "__main__":
    unittest.main()
