"""Frozen source CI, real native acceptance, and federation safety contracts."""
import json
import io
import subprocess
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.check_ci_budget import check_workflows, inspect_source, inspect_workflow

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
        source_job=workflow.split('  source:',1)[1].split('  test-compose:',1)[0]
        self.assertIn('    timeout-minutes: 3\n',source_job)
        self.assertNotIn('needs:',source_job)
        self.assertIn('timeout --signal=TERM --kill-after=5s 150s bash scripts/test-ci-source.sh',source_job)
        self.assertIn('ref: ${{ github.sha }}',source_job)
        self.assertIn('cancel-in-progress: true',source_job)
        self.assertNotIn('concurrency:',workflow.split('  promote-main:',1)[1])
        self.assertNotIn('--allow-dirty',workflow)
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

    def test_light_ci_keeps_complete_units_and_native_acceptance_on_the_development_host(self):
        workflow = (ROOT / '.github/workflows/docker.yml').read_text()
        lightweight = (ROOT / 'scripts/test-ci-source.sh').read_text()
        for marker in ('source-unit:', 'test_python_units.py', 'pip install', 'test-python-final.sh'):
            self.assertNotIn(marker,workflow)
        self.assertNotIn('unittest discover',lightweight)
        for module in ('test_repository_policy','test_deployment_contract','test_federation_contract',
                       'test_operator_release','test_operator_source','test_native_acceptance_cleanup'):
            self.assertIn('tests.'+module,lightweight)
        for check in ('check_ci_budget.py','check_cpu_boundary.py', 'validate_env_contract.py',
                      'check_release_policy.py', 'check_english_comments.py'):
            self.assertIn(check, lightweight)
        runner=(ROOT/'scripts/test_python_units.py').read_text()
        self.assertIn("loader.discover(str(root / 'tests'), pattern='test_*.py')",runner)
        self.assertIn('if loader.errors:',runner)
        suite = (ROOT / 'scripts/test-python-final.sh').read_text()
        for check in ('--cpus 1', 'check_cpu_quiescence.py', 'tests.federation_stack',
                      'certificate-nginx', 'certificate-storage', 'unit.log',
                      'test_python_units.py run --count 4', 'test_python_units.py verify --count 4'):
            self.assertIn(check, suite)
        self.assertIn('complete native acceptance', (ROOT / 'CONTRIBUTING.md').read_text())

    def test_required_compose_job_only_renders_configuration_on_exact_source(self):
        workflow = (ROOT / '.github/workflows/docker.yml').read_text()
        compose = workflow.split('  test-compose:', 1)[1].split('  topology:', 1)[0]
        for marker in ('runs-on: ubuntu-latest', '    timeout-minutes: 3\n',
                       'persist-credentials: false', 'ref: ${{ github.sha }}',
                       '150s python -m tests.compose_layout_native'):
            self.assertIn(marker, compose)
        for marker in ('needs:', 'pip install', 'docker ', 'playwright', 'secrets.', 'continue-on-error'):
            self.assertNotIn(marker, compose)
        self.assertNotIn('  pull_request:',workflow)
        self.assertIn('  merge_group:',workflow)
        self.assertIn("for (const required of ['source', 'test-compose'])", workflow)
        self.assertIn('listJobsForWorkflowRunAttempt', workflow)
        self.assertIn("job.conclusion !== 'success'", workflow)
        layout=(ROOT/'tests/compose_layout_native.py').read_text()
        self.assertIn("command.extend(['config','--format','json'])",layout)
        self.assertNotIn("'up'",layout)
        runner=(ROOT / 'scripts/test-python-final.sh').read_text()
        self.assertIn('python -m tests.resource_reservations_native',runner)
        self.assertIn('python -m tests.resource_admission_native',runner)
        self.assertIn('python -m tests.resource_transfer_native',runner)
        for flag in ('FRONTIERCLOUD_CANCEL_SOURCE_RELEASE_NATIVE','FRONTIERCLOUD_CANCEL_TARGET_RELEASE_NATIVE',
                     'FRONTIERCLOUD_CANCEL_MASTER_RELEASE_NATIVE'):
            self.assertIn('-e '+flag+'=1',runner)
            self.assertIn("'"+flag+"':'1'",(ROOT/'tests/resource_cancel_census_https_mysql_native.py').read_text())
        self.assertIn('FC_S3_EVIDENCE_DIR="$evidence/s3" bash scripts/test-python-s3.sh',runner)

    def test_hosted_budget_accepts_current_parallel_foreground_checks(self):
        self.assertEqual(check_workflows(ROOT/'.github/workflows'),[])
        self.assertEqual(inspect_source((ROOT/'scripts/test-ci-source.sh').read_text()),[])

    def test_compose_layout_main_only_invokes_daemon_free_configuration_rendering(self):
        from tests import compose_layout_native as layout
        files=[('docker-compose.yaml',),('docker-compose.yaml','docker-compose.mysql.yaml'),
               ('docker-compose.storage.yaml',)]
        configurations=[
            {'web':{'cpus':1,'environment':{'DB_TYPE':'sqlite'}}},
            {'web':{'cpus':1,'environment':{'DB_TYPE':'mysql'},'depends_on':{'mysql':{'condition':'service_healthy'}},
                    'volumes':[{'target':'/run/frontiercloud-secrets','read_only':True}]},
             'mysql':{'volumes':[{'target':'/var/lib/mysql'},{'target':'/run/frontiercloud-secrets','read_only':True}]}},
            {'web':{'cpus':1}},
        ]
        calls=[]
        def run(command,**options):
            index=len(calls)
            self.assertLess(index,len(files))
            expected=['docker','compose']
            for name in files[index]:expected.extend(['-f',str(ROOT/name)])
            expected.extend(['config','--format','json'])
            self.assertEqual(command,expected)
            self.assertEqual(options['cwd'],ROOT)
            self.assertEqual(options['timeout'],30)
            self.assertTrue(options['capture_output'] and options['text'])
            calls.append(command)
            return SimpleNamespace(returncode=0,stdout=json.dumps({'services':configurations[index]}))
        output=io.StringIO()
        with patch.object(layout.subprocess,'run',side_effect=run),redirect_stdout(output):
            layout.main()
        self.assertEqual(len(calls),3)
        receipt=json.loads(output.getvalue())
        self.assertTrue(receipt['passed'])
        self.assertEqual(receipt['count'],5)

    def test_hosted_budget_rejects_extended_timeouts_serial_jobs_and_timeout_bypass(self):
        workflow=(ROOT/'.github/workflows/docker.yml').read_text()
        for bad in (workflow.replace('timeout-minutes: 3','timeout-minutes: 4',1),
                    workflow.replace('  test-compose:\n','  test-compose:\n    needs: source\n',1),
                    workflow.replace('150s bash scripts/test-ci-source.sh','151s bash scripts/test-ci-source.sh',1),
                    workflow.replace('150s bash scripts/test-ci-source.sh','150s bash scripts/test-ci-source.sh || true',1),
                    workflow.replace('  source:\n','  source:\n    continue-on-error: true\n',1),
                    workflow+'\n  extra:\n    timeout-minutes: 3\n    steps:\n      - run: echo bypass\n'):
            with self.subTest(change=bad):
                self.assertTrue(inspect_workflow(bad,'docker.yml'))

    def test_hosted_budget_rejects_native_database_browser_and_background_work(self):
        source=(ROOT/'scripts/test-ci-source.sh').read_text()
        for command in ('bash scripts/test-python-final.sh','python scripts/test_python_units.py run --count 4',
                        'python -m unittest discover','docker compose up -d', 'docker --context remote build .',
                        'python -m tests.federation_stack', 'python -m tests.s3_uploads_native',
                        'python -m pip install -r requirements.txt', 'playwright test', 'sleep 1 &', 'echo ok || true'):
            with self.subTest(command=command):
                self.assertTrue(inspect_source(source+'\n'+command+'\n'))
        self.assertTrue(inspect_source(source.replace('tests.test_operator_source','tests.test_resource_scheduler')))

    def test_main_promotion_executes_exact_attempt_and_newest_run_guards(self):
        workflow=(ROOT/'.github/workflows/docker.yml').read_text()
        script=textwrap.dedent(workflow.split('  promote-main:',1)[1].split('          script: |\n',1)[1])
        harness=r'''
const fs = require('node:fs');
const assert = require('node:assert/strict');
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const promote = new AsyncFunction('context', 'github', 'core', JSON.parse(fs.readFileSync(0, 'utf8')));
(async () => {
  for (const mode of ['good', 'newest-failed', 'wrong-head', 'wrong-run', 'duplicate', 'skipped']) {
    const sha='1'.repeat(40), tree='2'.repeat(40), source='3'.repeat(40);
    const run={id:12,run_number:12,run_attempt:2,head_sha:source,head_branch:'dev',event:'push',status:'completed',conclusion:'success'};
    let attempt=null; const failures=[];
    const jobs=['source','test-compose'].map(name=>({name,status:'completed',conclusion:'success',head_sha:source,run_id:12}));
    if (mode==='wrong-head') jobs[0].head_sha=sha;
    if (mode==='wrong-run') jobs[0].run_id=11;
    if (mode==='duplicate') jobs.push({...jobs[0]});
    if (mode==='skipped') jobs[0].conclusion='skipped';
    const github={rest:{repos:{
      getCommit:async()=>({data:{commit:{tree:{sha:tree}}}}),
      listPullRequestsAssociatedWithCommit:async()=>({data:[{merged_at:'2026-10-23',base:{ref:'main'},head:{ref:'dev',sha:source,repo:{full_name:'owner/repo'}}}]})
    },actions:{
      listWorkflowRuns:async()=>({data:{workflow_runs:[{...run,id:11,run_number:11}, {...run,conclusion:mode==='newest-failed'?'failure':'success'}]}}),
      listJobsForWorkflowRunAttempt:async args=>{attempt=args;return {data:{jobs}};}
    }}};
    await promote({sha,repo:{owner:'owner',repo:'repo'}},github,{setFailed:value=>failures.push(value),notice:()=>{},setOutput:()=>{}});
    assert.equal(failures.length===0,mode==='good',mode);
    if(mode!=='newest-failed') assert.deepEqual(attempt,{owner:'owner',repo:'repo',run_id:12,attempt_number:2,per_page:100});
    else assert.equal(attempt,null);
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        subprocess.run(['node','-e',harness],input=json.dumps(script),text=True,check=True,timeout=10)

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
