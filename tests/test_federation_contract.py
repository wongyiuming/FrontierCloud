"""Frozen source CI, real native acceptance, and federation safety contracts."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FederationContractTests(unittest.TestCase):

    def test_business_configuration_has_no_test_transport_or_role_switch(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        for marker in ("cloudflared", "trycloudflare", "NODE_ROLE=", "MASTER_URL=", "SLAVE_URL=", "TLS_VERIFY=false"):
            self.assertNotIn(marker, compose + example)

    def test_ci_cluster_is_exactly_one_master_one_direct_and_one_relay(self):
        harness = (ROOT / "tests/federation_stack.py").read_text(encoding="utf-8")
        self.assertIn('(\"master-a\", \"direct-b\", \"relay-c\")', harness)
        self.assertIn('service[\"cpus\"] = 1.0', harness)
        self.assertFalse((ROOT / "tests/federation_seven_node.py").exists())

    def test_integrity_client_javascript_parses(self):
        for relative in (
            "static/js/admin-upload-integrity.js",
            "static/js/karaoke-audio-quality.js",
        ):
            subprocess.run(["node", "--check", str(ROOT / relative)], check=True)

    def test_relay_has_verified_tls_and_no_disk_buffering(self):
        nginx = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
        relay = nginx.split("location ~ \"^/_relay_media/", 1)[1].split("location = /_relay_failure", 1)[0]
        for marker in ("internal;", "proxy_ssl_verify on;", "proxy_ssl_server_name on;", "proxy_buffering off;", "proxy_max_temp_file_size 0;",
                       "proxy_next_upstream off;", "proxy_ignore_headers X-Accel-Redirect;", 'proxy_set_header Cookie "";'):
            self.assertIn(marker, relay)
        self.assertIn("~^/_relay_media/ /api/v1/media/stream", nginx)

    def test_actions_has_no_deployment_authority_or_insecure_test_transport(self):
        workflow = (ROOT / '.github/workflows/docker.yml').read_text()
        for marker in ('contents: write', 'packages: write', 'id-token: write',
                       'self-hosted', 'ssh-action', 'scp-action', 'curl -k'):
            self.assertNotIn(marker, workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertIn('contents: read', workflow)
        self.assertIn('actions: read', workflow)
        source_job=workflow.split('  source:',1)[1].split('  topology:',1)[0]
        self.assertIn('    timeout-minutes: 15\n',source_job)
        self.assertIn('needs: [source-policy, source-unit]',source_job)
        self.assertIn('if: always() &&',source_job)
        self.assertIn('test "$POLICY_RESULT" = success && test "$UNIT_RESULT" = success',source_job)
        self.assertIn('python scripts/test_python_units.py verify --count 4',source_job)
        self.assertNotIn('--allow-dirty',workflow)
        self.assertIn('SQLITE_PATH: ${{ github.workspace }}/data/frontiercloud-source.db', workflow)
        self.assertNotIn('ignore_https_errors', (ROOT / 'tests/federation_stack.py').read_text())

    def test_exact_dev_promotion_and_reviewed_tree_are_required(self):
        workflow = (ROOT / '.github/workflows/docker.yml').read_text()
        promotion = workflow.split('  promote-main:', 1)[1]
        self.assertIn('      pull-requests: read', promotion)
        self.assertNotIn('pull-requests:', workflow.split('  promote-main:', 1)[0])
        for marker in ('listPullRequestsAssociatedWithCommit', "pr?.base?.ref === 'main'",
                       "pr?.head?.ref === 'dev'", 'sourceShas.length !== 1',
                       'sourceTree !== mainTree', 'head_sha: sourceSha',
                       "item?.head_branch === 'dev'", "run.conclusion !== 'success'"):
            self.assertIn(marker, workflow)
        self.assertIn('Only same-repository dev -> main promotion is allowed', workflow)
        self.assertNotIn('parents[1]', workflow)

    def test_ci_runs_complete_unit_suite_and_independent_native_acceptance_remains_required(self):
        workflow = (ROOT / '.github/workflows/docker.yml').read_text()
        units=workflow.split('  source-unit:',1)[1].split('  source:',1)[0]
        self.assertIn('shard: [0, 1, 2, 3]',units)
        self.assertIn('fail-fast: false',units)
        self.assertIn('    timeout-minutes: 60\n',units)
        self.assertIn('python scripts/test_python_units.py run --count 4 --index',units)
        self.assertIn('if-no-files-found: error',units)
        runner=(ROOT/'scripts/test_python_units.py').read_text()
        self.assertIn("loader.discover(str(root / 'tests'), pattern='test_*.py')",runner)
        self.assertIn('if loader.errors:',runner)
        for check in ('check_cpu_boundary.py', 'validate_env_contract.py',
                      'check_release_policy.py', 'check_english_comments.py'):
            self.assertIn(check, workflow)
        suite = (ROOT / 'scripts/test-python-final.sh').read_text()
        for check in ('--cpus 1', 'check_cpu_quiescence.py', 'tests.federation_stack',
                      'certificate-nginx', 'certificate-storage', 'unit.log',
                      'test_python_units.py run --count 4', 'test_python_units.py verify --count 4'):
            self.assertIn(check, suite)
        self.assertIn('complete native acceptance', (ROOT / 'CONTRIBUTING.md').read_text())

    def test_required_compose_check_executes_real_native_suite_on_exact_source(self):
        workflow = (ROOT / '.github/workflows/docker.yml').read_text()
        native = workflow.split('  test-compose:', 1)[1].split('  promote-main:', 1)[0]
        for marker in ('needs: source', 'runs-on: ubuntu-latest', '    timeout-minutes: 360\n',
                       'persist-credentials: false', 'github.event.pull_request.head.sha || github.sha',
                       "github.event_name != 'push' || github.ref != 'refs/heads/main'",
                       'bash scripts/test-python-final.sh', 'FC_BROWSER_PYTHON',
                       'playwright==1.62.0', 'httpx==0.28.1', 'cryptography==50.0.1',
                       'libnss3-tools', 'test -x "$PLAYWRIGHT_CHROMIUM_EXECUTABLE"',
                       'safe.directory "$GITHUB_WORKSPACE"', '/root/frontiercloud-dev',
                       'source-commit.txt', 'operator-endpoint-sqlite-proof.json',
                       'operator-endpoint-mysql-proof.json', 'native-reservations.json', 'native-reservations.log',
                       'native-admission.json','native-admission.log',
                       'native-transfer.json','native-transfer.log',
                       'provider-initial.json', 'provider-restart.json', 'transactions.json', 'browser.json',
                       'operator-application.log','operator-application-inputs.jsonl','operator-install.log',
                       'test_business_sqlite_package','test_business_mysql_package','test_direct_storage_package','test_relay_storage_package',
                       'test_business_sqlite_owned_resources','test_business_mysql_owned_resources',
                       'test_direct_storage_owned_resources','test_relay_storage_owned_resources',
                       'test_controlled_sqlite_restore_preserves_failed_state_and_payload_inodes',
                       'test_controlled_mysql_restore_preserves_failed_state_and_payload_inodes',
                       'test_early_sqlite_abort_preserves_incomplete_snapshot_and_original_resources',
                       'test_early_mysql_abort_preserves_incomplete_snapshot_and_original_resources',
                       'test_staged_host_entry_upgrades_old_sqlite_application',
                       'test_staged_host_entry_upgrades_old_mysql_application_and_adds_init',
                       'test_staged_recover_preserves_sqlite_original_resources',
                       'test_staged_recover_preserves_mysql_original_resources'):
            self.assertIn(marker, native)
        self.assertNotIn('continue-on-error', native)
        self.assertNotIn('secrets.', native)
        self.assertNotIn('playwright install', native)
        self.assertIn("for (const required of ['source', 'test-compose'])", workflow)
        self.assertIn('listJobsForWorkflowRun', workflow)
        self.assertIn("job.conclusion !== 'success'", workflow)
        artifact = native.split('      - uses: actions/upload-artifact@v4', 1)[1]
        self.assertIn('frontiercloud-native-artifact/*', artifact)
        self.assertNotIn('frontiercloud-native-evidence', artifact)
        self.assertNotIn('path: .', artifact)
        runner=(ROOT / 'scripts/test-python-final.sh').read_text()
        self.assertIn('python -m tests.resource_reservations_native',runner)
        self.assertIn('python -m tests.resource_admission_native',runner)
        self.assertIn('python -m tests.resource_transfer_native',runner)
        for flag in ('FRONTIERCLOUD_CANCEL_SOURCE_RELEASE_NATIVE','FRONTIERCLOUD_CANCEL_TARGET_RELEASE_NATIVE',
                     'FRONTIERCLOUD_CANCEL_MASTER_RELEASE_NATIVE'):
            self.assertIn('-e '+flag+'=1',runner)
            self.assertIn("'"+flag+"':'1'",(ROOT/'tests/resource_cancel_census_https_mysql_native.py').read_text())
        self.assertIn('FC_S3_EVIDENCE_DIR="$evidence/s3" bash scripts/test-python-s3.sh',runner)

    def test_no_product_component_mounts_docker_socket_or_can_replace_software(self):
        for name in ('docker-compose.yaml', 'docker-compose.storage.yaml', 'docker-compose.mysql.yaml'):
            source = (ROOT / name).read_text()
            self.assertNotIn('/var/run/docker.sock', source)
            self.assertNotIn('  updater:', source)
            self.assertNotIn('privileged: true', source)
        for name in ('app/services/release_control.py', 'app/services/cluster_update_coordinator.py',
                     'app/api/internal_cluster_update.py', 'updater/server.py'):
            self.assertFalse((ROOT / name).exists())

    def test_operator_maintenance_has_no_force_open_escape(self):
        gate = (ROOT / 'nginx/maintenance-gate.conf').read_text()
        self.assertIn('.frontiercloud-native-maintenance', gate)
        self.assertIn('return 503', gate)
        self.assertNotIn('.frontiercloud-force-open', gate)
        self.assertNotIn('internal/v1/cluster-update', gate)
        configuration = (ROOT/'nginx/nginx.conf').read_text()
        self.assertIn('map "$request_method:$request_uri" $frontiercloud_identity_probe',configuration)
        self.assertIn('~^GET:/internal/v1/identity[?]challenge=[a-f0-9]{32}$',configuration)
        self.assertIn('if ($frontiercloud_identity_probe = 1)',gate)
