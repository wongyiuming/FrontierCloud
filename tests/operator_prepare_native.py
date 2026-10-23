"""Real Compose image preparation, not a business upgrade or activation gate."""
import copy
import hashlib
import json
import os
from pathlib import Path
import unittest
import subprocess
import sqlite3
import shutil
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_prepare as prepare
from scripts import operator_source as source_io
from scripts import operator_upgrade as transactions
from tests import operator_deployment_native as fixtures
from tests import operator_backup_runtime as recovery_fixtures


@unittest.skipUnless(os.environ.get("FC_OPERATOR_PREPARE_RUNTIME") == "1", "explicit isolated Docker image preparation acceptance")
class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.NativeDiscoveryTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        evidence = backup.absolute(os.environ["FC_OPERATOR_PREPARE_EVIDENCE"])
        self.evidence = evidence / self._testMethodName
        self.evidence.mkdir(mode=0o700)
        self.results = []

    def candidate(self, deployment, *, invalid_dockerfile=False, addition=True, helper_source=False, application_source=False):
        stage = self.evidence / "stage"
        source = stage / "source"
        source.mkdir(parents=True)
        (source / "nginx").mkdir()
        base = os.environ.get("FC_OPERATOR_DISCOVERY_IMAGE", "frontiercloud-python-archive:db-test-tools")
        dockerfile = "FROM " + base + "\nCOPY source-proof-marker /source-proof-marker\n"
        if helper_source:
            dockerfile += 'USER 0\nRUN rm -rf /app/app /app/static /app/main.py\n'
            shutil.copytree(Path(__file__).resolve().parents[1] / 'app', source / 'app', ignore=shutil.ignore_patterns('__pycache__'))
            dockerfile += 'COPY app /app/app\nWORKDIR /app\nRUN chmod -R a+rX /app/app && mkdir -p /app/data\nUSER 10001:10001\n'
        if application_source:
            shutil.copy2(Path(__file__).resolve().parents[1]/'main.py', source/'main.py')
            shutil.copytree(Path(__file__).resolve().parents[1]/'static', source/'static')
            dockerfile += 'COPY main.py /app/main.py\nCOPY static /app/static\n'
        (source / "Dockerfile").write_text("INVALID_DIRECTIVE\n" if invalid_dockerfile else dockerfile)
        (source / "nginx/Dockerfile").write_text(dockerfile)
        (source / "source-proof-marker").write_bytes(b"exact candidate fixture bytes\x00\xff")
        (source / ".dockerignore").write_text(".env\n")
        config = copy.deepcopy(deployment.plan.config)
        config["services"]["web"]["environment"]["ADMIN_KEY"] = "${OPERATOR_FIXTURE_KEY:?missing fixture input}"
        # The source context carries public placeholders, never the original
        # private .env. Existing data paths are restored by mapping admission.
        for row in config["services"].values():
            for volume in row.get("volumes", []):
                if volume["target"] == "/app/data":
                    volume["source"] = "${DATA_DIRECTORY:-./data}"
        if addition:
            config["services"]["certificate-init"] = copy.deepcopy(config["services"]["certificate-agent"])
            config["services"]["certificate-init"]["restart"] = "no"
            config["volumes"]["traffic_runtime"] = {"name": config["name"] + "_traffic_runtime"}
            for name in ("web", "nginx"):
                config["services"][name]["volumes"].append({"type": "volume", "source": "traffic_runtime",
                    "target": "/run/frontiercloud-traffic", "read_only": name == "web"})
        # This is an input Compose file: placeholders must remain unescaped.
        (source / "docker-compose.yaml").write_text(json.dumps(config))
        inventory = {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in source.rglob("*") if path.is_file()}
        receipt = {"stage_sha256": mapping.checksum(inventory), "provenance": {"revision": "f" * 40, "public_provenance_verified": False},
                   "fixture_kind": "unpublished-isolated-native-source"}
        def verify(path):
            self.assertEqual(path, stage)
            current = {item.relative_to(source).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest() for item in source.rglob("*") if item.is_file()}
            if current != inventory:
                raise mapping.DeploymentError("fixture_source_changed", "native source changed")
            return copy.deepcopy(receipt)
        return stage, verify

    def record(self, value):
        backup.private_write(self.evidence / "result.json", backup.json_bytes(value))

    def stop_application(self, identifier):
        self.fixture.run_command(['docker', 'stop', '--time', '15', identifier])
        # AutoRemove finishes after stop's response. Retain referenced volumes
        # until the exact owned container has actually disappeared.
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            if not self.fixture.run_command(['docker', 'ps', '-aq', '--filter', 'id='+identifier]):
                return
            time.sleep(.1)
        self.fail('Owned application fixture has not finished removal; volumes are preserved')

    def cleanup_added_volumes(self, old, prepared):
        for name in prepared.receipt['new_ephemeral_volumes']:
            value = old.docker.volume(name)
            if value is None:
                continue
            logical = next(key for key, row in prepared.config['volumes'].items() if row.get('name') == name)
            self.assertEqual(value.get('Labels', {}).get(mapping.LABEL+'project'), old.plan.name)
            self.assertEqual(value.get('Labels', {}).get(mapping.LABEL+'volume'), logical)
            self.assertFalse(self.fixture.run_command(['docker', 'ps', '-aq', '--filter', 'volume='+name]))
            self.fixture.run_command(['docker', 'volume', 'rm', name])

    def test_actual_build_new_service_literal_inputs_and_moved_old_tag(self):
        directory, project, _, _, marker = self.fixture.prepare(literal_input=True, owned_application_image=True)
        old = mapping.Deployment.discover(directory)
        original = backup.digest(marker)
        stage, verify = self.candidate(old)
        alias = old.plan.config["services"]["web"]["image"]
        def execute(docker, arguments, output, step):
            prepare._command(docker, arguments, output, step)
            build = docker.config()
            self.fixture.run_command(["docker", "tag", build["services"]["web"]["image"], alias])
        result = prepare.prepare(old, stage, self.evidence / "prepared", verify=verify, acceptance=True, execute=execute)
        self.assertEqual(result.receipt["phase"], "images_prepared")
        self.assertTrue(result.receipt["acceptance_only"])
        self.assertEqual(result.receipt["added_services"], ["certificate-init"])
        self.assertEqual(result.config["services"]["web"]["environment"]["ADMIN_KEY"], "literal$DOLLAR${BRACES}$$tail")
        self.assertEqual(result.config["services"]["redis"]["image"], old.observed["redis"]["image_id"])
        self.assertEqual(old.observed, mapping.Deployment.discover(directory).observed)
        self.assertEqual(original, backup.digest(marker))
        self.assertFalse((marker.parent / ".frontiercloud-native-maintenance").exists())
        self.assertIsNone(old.docker.volume(project + "_traffic_runtime"))
        rollback = mapping.ComposeDocker(directory, [result.directory / "old-compose.json"], project, []).config()
        self.assertEqual(rollback["services"]["web"]["image"], old.observed["web"]["image_id"])
        moved = json.loads(self.fixture.run_command(["docker", "image", "inspect", alias]))[0]["Id"]
        self.assertNotEqual(moved, old.observed["web"]["image_id"])
        self.assertTrue(all(row["image"].startswith("sha256:") and "build" not in row for row in result.config["services"].values()))
        self.record({"passed": True, "actual_image_build": True, "added_service_not_started": True,
            "new_ephemeral_volume_not_created": True, "old_tag_moved_but_running_and_rollback_ids_preserved": True,
            "literal_environment_and_data_paths_preserved": True, "data_marker_sha256": original[1],
            "old_container_ids": {name: row["container_id"] for name, row in old.observed.items()},
            "prepared_image_ids": {name: row["id"] for name, row in result.receipt["images"].items()},
            "source_stage_sha256": result.receipt["source_stage_sha256"], "not_an_application_upgrade": True})

    def test_real_build_failure_preserves_services_data_and_diagnostics(self):
        directory, _, _, _, marker = self.fixture.prepare()
        old = mapping.Deployment.discover(directory)
        original = backup.digest(marker)
        stage, verify = self.candidate(old, invalid_dockerfile=True)
        output = self.evidence / "failed-build"
        with self.assertRaises(mapping.DeploymentError) as caught:
            prepare.prepare(old, stage, output, verify=verify, acceptance=True)
        self.assertEqual(caught.exception.code, "image_build_failed")
        self.assertFalse((output / "prepared.json").exists())
        attempt = json.loads((output / "image-build-result.json").read_text())
        self.assertNotEqual(attempt["exit_code"], 0)
        self.assertTrue((output / "image-build.log").stat().st_size)
        self.assertEqual(old.observed, mapping.Deployment.discover(directory).observed)
        self.assertEqual(original, backup.digest(marker))
        self.assertFalse((marker.parent / ".frontiercloud-native-maintenance").exists())
        self.record({"passed": True, "actual_build_failed": True, "old_services_and_data_preserved": True,
                     "no_completion_receipt": True, "private_diagnostics_retained": True, "error": caught.exception.code})

    def test_unsupported_persistent_addition_fails_before_build_or_any_new_service(self):
        directory, _, _, _, marker = self.fixture.prepare()
        old = mapping.Deployment.discover(directory)
        stage, first_verify = self.candidate(old)
        file = stage / "source/docker-compose.yaml"
        config = json.loads(file.read_text())
        config["services"]["certificate-init"]["volumes"].append({"type": "bind", "source": str(marker.parent), "target": "/unknown-state"})
        file.write_text(json.dumps(config))
        def verify(path):
            return {"stage_sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                    "provenance": {"revision": "f" * 40, "public_provenance_verified": False}}
        attempted = []
        with self.assertRaises(mapping.DeploymentError) as caught:
            prepare.prepare(old, stage, self.evidence / "must-not-exist", verify=verify, acceptance=True,
                            execute=lambda *args: attempted.append(args))
        self.assertEqual(caught.exception.code, "unsupported_mount")
        self.assertEqual(attempted, [])
        self.assertFalse((self.evidence / "must-not-exist").exists())
        self.assertEqual(old.observed, mapping.Deployment.discover(directory).observed)
        self.record({"passed": True, "unknown_persistent_mount_refused": True, "image_build_attempted": False,
                     "old_services_unchanged": True, "new_service_started": False})

    def legacy_source(self, docker):
        revision = "5da7a21c208e5fa758ff926443d5169dd466406d"
        expected_tree = "768b5d2e90e29d30444b3a52be7e66d9ad9b9e17"
        git = Path(__file__).resolve().parents[1]
        exists = subprocess.run(["git", "-C", str(git), "cat-file", "-e", revision + "^{commit}"], capture_output=True, check=False)
        if exists.returncode:
            git = self.evidence / "legacy-git"
            prepare._command(docker, ["git", "init", "--bare", "--quiet", str(git)], self.evidence, "legacy-git-init")
            prepare._command(docker, ["git", "-C", str(git), "fetch", "--depth=1", "--no-tags", "https://github.com/wongyiuming/FrontierCloud.git", revision], self.evidence, "legacy-git-fetch")
            self.assertEqual(self.fixture.run_command(["git", "-C", str(git), "for-each-ref"]), "")
        tree = self.fixture.run_command(["git", "-C", str(git), "rev-parse", revision + "^{tree}"])
        self.assertEqual(tree, expected_tree)
        listing = subprocess.check_output(["git", "-C", str(git), "ls-tree", "-r", "-t", "--long", "-z", revision])
        entries = []
        for record in listing.split(b"\0"):
            if not record:
                continue
            fields, name = record.split(b"\t", 1)
            mode, kind, digest, size = fields.split()
            row = {"path": name.decode(), "mode": mode.decode(), "type": kind.decode(), "sha": digest.decode()}
            if kind == b"blob":
                row["size"] = int(size)
            entries.append(row)
        api = SimpleNamespace(get=lambda *args, **kwargs: {"sha": tree, "truncated": False, "tree": entries})
        files, directories, _ = source_io.inventory(api, tree)
        archive = self.evidence / "legacy-source.tar.gz"
        prepare._command(docker, ["git", "-C", str(git), "archive", "--format=tar.gz", "--prefix=wongyiuming-FrontierCloud-" + revision[:7] + "/", "--output=" + str(archive), revision], self.evidence, "legacy-git-archive")
        destination = self.evidence / "legacy-source"
        destination.mkdir(mode=0o755)
        # The UID 10001 bootstrap must traverse /app even under umask 077.
        destination.chmod(0o755)
        source_io.extract(archive, destination, files, directories, revision)
        return destination, revision, tree

    def wait_mysql(self, docker, identifier):
        docker.audit_file = self.evidence / ("mysql-ready-" + identifier[:12] + ".jsonl")
        for _ in range(60):
            try:
                docker.run(["docker", "exec", identifier, "mysqladmin", "ping", "-h127.0.0.1", "--protocol=TCP", "-uroot", "--silent"])
                return
            except mapping.DeploymentError as error:
                if error.code != "docker_command_failed":
                    raise
            time.sleep(.5)
        self.fail("isolated MySQL did not become ready")

    def mysql_facts(self, docker, identifier):
        def mysql(_directory, sql):
            return self.fixture.run_command(["docker", "exec", identifier, "mysql", "-uroot", "--batch", "--raw", "--skip-column-names", "-e", sql])
        return recovery_fixtures.ColdRecoveryRuntime.mysql_facts(SimpleNamespace(mysql=mysql), docker.directory)

    def restored_mysql_facts(self, old, snapshot):
        directory = self.evidence / "restored-empty-target"
        directory.mkdir()
        data = directory / "data"
        data.mkdir()
        config = copy.deepcopy(old.plan.config)
        project = config["name"] + "-recovery"
        config["name"] = project
        for key, definition in config.get("volumes", {}).items():
            definition["name"] = project + "_" + key
        for key, definition in config.get("networks", {}).items():
            definition["name"] = project + "_" + key
            definition['ipam'] = {'config':[{'subnet':self.fixture.isolated_subnet()}]}
        for name, definition in config["services"].items():
            definition["image"] = old.observed[name]["image_id"]
            definition.pop("build", None)
            definition.pop("profiles", None)
            definition["pull_policy"] = "never"
            for row in definition.get("volumes", []):
                if row["target"] == "/app/data":
                    row["source"] = str(data)
        file = directory / "docker-compose.yaml"
        backup.private_write(file, backup.json_bytes(backup.compose_values(config, encode=True)))
        docker = mapping.ComposeDocker(directory, [file], project, [])
        self.addCleanup(self.fixture.run_command, docker.base + ["down", "--volumes", "--timeout", "10"])
        manifest = backup.verify(snapshot)
        plan = backup.Plan.load(docker, restoring=True, snapshot_resources=set(manifest["resources"]))
        backup.restore(plan, snapshot)
        self.fixture.run_command(docker.base + ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "mysql", "redis"])
        identifier = self.fixture.run_command(docker.base + ["ps", "-q", "mysql"])
        self.wait_mysql(docker, identifier)
        self.assertFalse((data / "media").exists())
        self.assertFalse((data / "recordings").exists())
        return self.mysql_facts(docker, identifier)

    def cold_generation4(self, engine, *, database_prepare=False, database_controller=False, invalid_configuration=False, fenced_application=False):
        directory, project, _, _, marker = self.fixture.prepare(mysql=engine == "mysql", business_mode='all' if invalid_configuration else '', application_tls=fenced_application)
        initial = mapping.Deployment.discover(directory)
        legacy, revision, tree = self.legacy_source(initial.docker)
        # runc needs the ignored empty mountpoint before overlaying /app/data
        # on the read-only exact tracked source. No tracked source file changes.
        (legacy / "data").mkdir(mode=0o755)
        data = initial.plan.resources["control"].root
        secrets = initial.plan.resources["secrets"]
        for path in (data, secrets.root):
            os.chown(path, 10001, 10001)
            os.chmod(path, 0o700)
        if engine == "mysql":
            self.wait_mysql(initial.docker, initial.observed["mysql"]["container_id"])
            self.fixture.run_command(["docker", "exec", initial.observed["mysql"]["container_id"], "mysql", "-uroot", "-e",
                "CREATE DATABASE office_automation CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"])
        program = """import asyncio,json
from app.services.runtime_secrets import initialize_runtime_secrets
initialize_runtime_secrets()
from app.core import db
if db.settings.DB_TYPE=='mysql':
    from sqlalchemy.ext.asyncio import create_async_engine
    db.engine=create_async_engine('mysql+asyncmy://root@mysql:3306/office_automation')
from app.services.federation.state import state
from app.services import media_crypto
async def run():
    try:
        await db.init_db()
        await state.initialize()
        await media_crypto.initialize()
        assert db.SCHEMA_GENERATION==4
        print(json.dumps({'generation':db.SCHEMA_GENERATION,'node_id':state.node['node_id'],'role':state.node['role']}))
    finally:
        await db.engine.dispose()
asyncio.run(run())
"""
        tools = os.environ.get("FC_OPERATOR_DISCOVERY_IMAGE", "frontiercloud-python-archive:db-test-tools")
        network = initial.plan.config["networks"]["default"]["name"] if engine == "mysql" else "none"
        arguments = ["docker", "run", "--rm", "--name", project + "-legacy-bootstrap", "--network", network, "--cpus", "1", "--memory", "512m",
            "--user", "10001:10001", "--workdir", "/app", "--mount", "type=bind,source=" + str(legacy) + ",target=/app,readonly",
            "--mount", "type=bind,source=" + str(data) + ",target=/app/data", "--mount", "type=volume,source=" + secrets.source + ",target=/run/frontiercloud-secrets",
            "-e", "DB_TYPE=" + engine, "-e", "SQLITE_PATH=/app/data/frontiercloud.db", "-e", "PYTHONDONTWRITEBYTECODE=1", tools, "python", "-c", program]
        prepare._command(initial.docker, arguments, self.evidence, "legacy-bootstrap")
        identity = json.loads((self.evidence / "legacy-bootstrap.log").read_text().strip().splitlines()[-1])
        self.assertEqual(identity["generation"], 4)
        if engine == 'mysql':
            program = """import asyncio,pathlib,asyncmy
async def run():
    password=pathlib.Path('/run/frontiercloud-secrets/mysql_password').read_text().strip()
    connection=await asyncmy.connect(host='mysql',user='root',autocommit=True)
    try:
        async with connection.cursor() as cursor:
            await cursor.execute("CREATE USER 'media_admin'@'%%' IDENTIFIED BY %s",(password,))
            await cursor.execute("GRANT ALL ON office_automation.* TO 'media_admin'@'%'")
    finally:
        connection.close()
asyncio.run(run())
"""
            arguments = ['docker', 'run', '--rm', '--network', network, '--cpus', '1', '--memory', '512m',
                '--user', '10001:10001', '--mount', 'type=volume,source=' + secrets.source + ',target=/run/frontiercloud-secrets,readonly',
                tools, 'python', '-c', program]
            prepare._command(initial.docker, arguments, self.evidence, 'fixture-database-credential')
        if engine == "sqlite":
            with closing(sqlite3.connect((data / "frontiercloud.db").as_uri() + "?mode=ro", uri=True)) as connection:
                facts = backup.sqlite_facts(connection)
        else:
            facts = self.mysql_facts(initial.docker, initial.observed["mysql"]["container_id"])
        key_hashes = {name: backup.digest(path)[1] for name, path in {
            "node_vault": secrets.root / "node-vault.key", "media_premaster": data / "media-keys/media-premaster.key"}.items()}
        for name in ("media", "recordings"):
            (data / name).mkdir(exist_ok=True)
            (data / name / "existing.bin").write_bytes(b"preserved payload bytes\x00\xff" * 4096)
            # Seed payloads with the old application's private non-root owner.
            for path in (data / name, data / name / "existing.bin"):
                os.chown(path, 10001, 10001)
        payload_hashes = {name: backup.digest(data / name / "existing.bin")[1] for name in ("media", "recordings")}
        old = mapping.Deployment.discover(directory)
        stage, verify = self.candidate(old, helper_source=True, application_source=fenced_application)
        prepared = prepare.prepare(old, stage, self.evidence / "prepared", verify=verify, acceptance=True)
        self.addCleanup(self.cleanup_added_volumes, old, prepared)
        if invalid_configuration:
            with self.assertRaises(mapping.DeploymentError) as caught:
                transactions.Upgrade.begin(old, prepared, verify=verify, acceptance=True)
            self.assertEqual(caught.exception.code, 'configuration_invalid')
            self.assertFalse((data/transactions.HARD).exists())
            self.assertFalse((prepared.directory/'upgrade-state.json').exists())
            self.assertEqual(mapping.Deployment.discover(directory).summary(), old.summary())
            self.record({'passed': True, 'candidate_configuration_refused_before_fence': True,
                'old_services_unchanged': True, 'database_generation': 4, 'error': caught.exception.code,
                'business_opened': False, 'upgrade_complete': False})
            return
        if database_controller:
            partial = data/'media/.upload-native-fixture.part'
            partial.write_bytes(b'fixture-owned partial upload')
            with self.assertRaises(mapping.DeploymentError) as caught:
                transactions.Upgrade.begin(old, prepared, verify=verify, acceptance=True)
            self.assertEqual(caught.exception.code, 'unfinished_transactions')
            self.assertEqual(partial.read_bytes(), b'fixture-owned partial upload')
            self.assertFalse((data/transactions.HARD).exists())
            self.assertFalse((prepared.directory/'upgrade-state.json').exists())
            self.assertEqual(mapping.Deployment.discover(directory).summary(), old.summary())
            # This test created the fault material; product preflight never
            # removes it or treats its age as permission to bypass recovery.
            partial.unlink()
        operation = transactions.Upgrade.begin(old, prepared, verify=verify, acceptance=True)
        self.assertEqual(operation.state['preflight']['phase'], 'preflight_passed')
        self.assertFalse((data / transactions.HARD).exists())
        for name in prepared.receipt['new_ephemeral_volumes']:
            self.assertIsNone(old.docker.volume(name))
        def incomplete(plan, output):
            output.mkdir(mode=0o700)
            backup.private_write(output / "partial", b"retained incomplete snapshot")
            raise OSError("native interruption while building state snapshot")
        with patch.object(backup, "backup", incomplete), self.assertRaises(OSError):
            operation.capture()
        previous = operation.state["snapshot"]
        self.assertTrue((data / transactions.HARD).exists())
        current = transactions.Upgrade.resume(old, prepared)
        result = current.capture()
        manifest = backup.verify(Path(result["snapshot"]))
        if engine == "sqlite":
            self.assertEqual(manifest["sqlite"]["facts"], facts)
        else:
            self.assertEqual(self.restored_mysql_facts(old, Path(result["snapshot"])), facts)
        self.assertEqual((prepared.directory / previous / "partial").read_bytes(), b"retained incomplete snapshot")
        self.assertIn(previous, current.state["retained_snapshots"])
        self.assertFalse(any(row.get("State", {}).get("Running") for row in old.docker.containers()
            if (row.get("Config", {}).get("Labels") or {}).get(mapping.LABEL + "project") == project))
        for name, path in {"node_vault": secrets.root / "node-vault.key", "media_premaster": data / "media-keys/media-premaster.key"}.items():
            self.assertEqual(backup.digest(path)[1], key_hashes[name])
        self.assertEqual({name: backup.digest(data / name / "existing.bin")[1] for name in payload_hashes}, payload_hashes)
        self.assertFalse(any(name.startswith(("media/", "recordings/")) or name in {"media", "recordings"}
                             for name in manifest["resources"]["control"]["inventory"]))
        with patch.object(backup, "backup", side_effect=AssertionError("duplicate copied state")):
            self.assertEqual(transactions.Upgrade.resume(old, prepared).capture(), result)
        database_result = self.prepare_captured_database(old, prepared, current, identity) if database_prepare else None
        if database_controller:
            def lost_reply(docker, arguments, output, step, **options):
                prepare._command(docker, arguments, output, step, **options)
                if step == 'database-prepare':
                    raise mapping.DeploymentError('injected_lost_database_reply', 'lost host reply after actual successful database preparation')
            with self.assertRaises(mapping.DeploymentError) as caught:
                current.prepare_database(verify=verify, execute=lost_reply)
            self.assertEqual(caught.exception.code, 'injected_lost_database_reply')
            self.assertEqual(current.state['phase'], 'database_preparing')
            baseline_hash = current.state['database_baseline_sha256']
            current = transactions.Upgrade.resume(old, prepared)
            database_result = current.prepare_database(verify=verify)
            self.assertEqual(current.state['database_baseline_sha256'], baseline_hash)
            self.assertEqual((database_result['from_generation'], database_result['generation']), (4, 9))
            def refuse(*_args, **_options):
                raise AssertionError('terminal replay executed a database helper')
            self.assertEqual(transactions.Upgrade.resume(old, prepared).prepare_database(verify=verify, execute=refuse), database_result)
            self.assertEqual(current.state['phase'], 'database_prepared')
        startup = self.fenced_application(old, prepared, current, payload_hashes) if fenced_application else None
        self.assertEqual({name: backup.digest(data / name / "existing.bin")[1] for name in payload_hashes}, payload_hashes)
        self.record({"passed": True, "published_old_source": revision, "published_old_tree": tree,
            "database": engine, "old_schema_generation": 4, "old_code_database_bootstrap": True, "old_asgi_application_started": False,
            "real_docker_stop_and_state_only_snapshot": True, "typed_database_tables": len(facts["tables"]),
            "typed_database_facts_sha256": mapping.checksum(facts), "node_id": identity["node_id"],
            "key_hashes": key_hashes, "payload_hashes": payload_hashes, "media_copied": False,
            "incomplete_snapshot_retained": True, "restart_resume": True, "duplicate_did_not_recopy": True,
            "actual_mysql_empty_target_restore": engine == "mysql", "database_preparation": database_result,
            "database_reply_loss_reconciled": database_controller,
            "partial_upload_refused_before_fence_and_preserved": database_controller,
            "live_preflight": operation.state['preflight'],
            "fenced_application": startup,
            "new_code_activated": fenced_application, "full_deployment_activated": False, "upgrade_complete": False})

    def fenced_application(self, old, prepared, operation, payload_hashes):
        data, project = old.plan.resources['control'].root, old.plan.name
        tools = os.environ.get('FC_OPERATOR_DISCOVERY_IMAGE', 'frontiercloud-python-archive:db-test-tools')
        program = """from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
from datetime import datetime,timedelta,timezone
import pathlib,hashlib,json
root=pathlib.Path('/app/data');now=datetime.now(timezone.utc)
ca_key=ec.generate_private_key(ec.SECP256R1());key=ec.generate_private_key(ec.SECP256R1())
ca_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Operator Native Fixture CA')])
usage=lambda authority:x509.KeyUsage(True,False,False,False,False,authority,authority,None,None)
authority=x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key())
ca=x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).add_extension(usage(True),critical=True).add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),critical=False).add_extension(authority,critical=False).sign(ca_key,hashes.SHA256())
leaf=x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'operator.4399.com')])).issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=1)).add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True).add_extension(usage(False),critical=True).add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),critical=False).add_extension(authority,critical=False).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False).add_extension(x509.SubjectAlternativeName([x509.DNSName('operator.4399.com')]),critical=False).sign(ca_key,hashes.SHA256())
for name,value in [('operator-test-ca.pem',ca.public_bytes(serialization.Encoding.PEM)),('operator-test-leaf.pem',leaf.public_bytes(serialization.Encoding.PEM)),('operator-test-key.pem',key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))]:
    path=root/name;path.write_bytes(value);path.chmod(0o600)
print(json.dumps({'leaf_sha256':hashlib.sha256(leaf.public_bytes(serialization.Encoding.DER)).hexdigest()}))
"""
        prepare._command(old.docker, ['docker', 'run', '--rm', '--network', 'none', '--cpus', '1', '--memory', '128m',
            '--user', '10001:10001', '--mount', 'type=bind,source='+str(data)+',target=/app/data', tools, 'python', '-c', program],
            self.evidence, 'fenced-test-certificate')
        leaf = json.loads((self.evidence/'fenced-test-certificate.log').read_text().strip().splitlines()[-1])['leaf_sha256']
        rollback = mapping.ComposeDocker(old.docker.directory, [prepared.directory/'old-compose.json'], project, [])
        prepare._command(rollback, rollback.base+['up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'redis'], self.evidence, 'fenced-redis-start')
        name = project+'-fenced-application'
        baseline = operation.state['database_baseline']
        config = copy.deepcopy(prepared.config)
        config['services']['web']['extra_hosts'] = {'operator.4399.com': '127.0.0.1'}
        config['services']['web']['volumes'].append({'type': 'bind', 'source': baseline, 'target': '/run/upgrade-baseline.json', 'read_only': True})
        compose = self.evidence/'fenced-application-compose.json'
        backup.private_write(compose, backup.json_bytes(backup.compose_values(config, encode=True)))
        application_docker = mapping.ComposeDocker(old.docker.directory, [compose], project, [])
        arguments = application_docker.base + ['run', '--rm', '-d', '--no-deps', '--pull', 'never', '--name', name,
            '--entrypoint', 'python', 'web', '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', '8000', '--workers', '1',
            '--ssl-certfile', '/app/data/operator-test-leaf.pem', '--ssl-keyfile', '/app/data/operator-test-key.pem']
        prepare._command(application_docker, arguments, self.evidence, 'fenced-application-start')
        value = json.loads(self.fixture.run_command(['docker', 'inspect', name]))[0]
        identifier = value['Id']
        self.addCleanup(self.stop_application, identifier)
        self.assertEqual(value['Image'], prepared.receipt['images']['web']['id'])
        self.assertEqual((value['HostConfig']['NanoCpus'], value['HostConfig']['Memory']), (1000000000, 512*1024*1024))
        program = """import asyncio,hashlib,json,pathlib,socket,ssl,time,urllib.request,urllib.error
from sqlalchemy.ext.asyncio import create_async_engine
from app.core import db
from app.core.config import SECRET_DIR
from app.services.offline_upgrade import inspect_database
from app.services.federation import protocol
context=ssl.create_default_context(cafile='/app/data/operator-test-ca.pem')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
deadline=time.monotonic()+60
while True:
    try:
        with opener.open('https://operator.4399.com:8000/health/ready',timeout=3) as response:
            assert response.status==200;break
    except urllib.error.URLError as error:
        if isinstance(error.reason,ssl.SSLCertVerificationError) or time.monotonic()>=deadline:raise
        time.sleep(.3)
try:
    opener.open('https://operator.4399.com:8000/api/v1/media/',timeout=3)
    raise AssertionError('business was opened under the hard fence')
except urllib.error.HTTPError as error:
    assert error.code==503
with opener.open('https://operator.4399.com:8000/internal/v1/identity?challenge='+'b'*32,timeout=3) as response:
    envelope=json.load(response)
identity=protocol.verify(envelope['payload']['public_key'],envelope)
baseline=json.loads(pathlib.Path('/run/upgrade-baseline.json').read_text())
assert identity['node_id']==baseline['node_id']
assert hashlib.sha256(identity['public_key'].encode()).hexdigest()==baseline['public_key_sha256']
with socket.create_connection(('operator.4399.com',8000),timeout=3) as sock:
    with context.wrap_socket(sock,server_hostname='operator.4399.com') as secured:
        fingerprint=hashlib.sha256(secured.getpeercert(binary_form=True)).hexdigest()
async def inspect():
    reader=create_async_engine(db.engine.url.set(database=pathlib.Path('/app/data/frontiercloud.db').as_uri(),query={'mode':'ro','uri':'true'})) if db.settings.DB_TYPE=='sqlite' else db.engine
    try:
        value=await inspect_database(reader,SECRET_DIR,pathlib.Path('/app/data/media-keys'),baseline=baseline)
        assert value['generation']==db.SCHEMA_GENERATION
        return len(value['tables'])
    finally:
        await reader.dispose()
tables=asyncio.run(inspect())
assert pathlib.Path('/app/data/.frontiercloud-native-maintenance').is_file()
assert not pathlib.Path('/app/data/media/lyrics/default.lrc').exists()
print(json.dumps({'certificate_verified':True,'leaf_sha256':fingerprint,'ready_status':200,'business_status':503,
    'identity_status':200,'node_id':identity['node_id'],'generation':db.SCHEMA_GENERATION,'original_tables_preserved':tables,
    'single_worker_command':pathlib.Path('/proc/1/cmdline').read_bytes().replace(b'\\x00',b' ').decode()}))
"""
        try:
            prepare._command(prepared.docker, ['docker', 'exec', identifier, 'python', '-c', program], self.evidence, 'fenced-application-probe', timeout=90)
        except Exception:
            self.fixture.run_command(['docker', 'logs', '--tail', '80', identifier])
            raise
        result = json.loads((self.evidence/'fenced-application-probe.log').read_text().strip().splitlines()[-1])
        self.assertEqual(result['leaf_sha256'], leaf)
        cpu = self.fixture.run_command(['python3', 'scripts/check_cpu_quiescence.py', identifier, '--timeout', '30'])
        self.assertIn('for 5 samples', cpu)
        self.assertEqual({key: backup.digest(data/key/'existing.bin')[1] for key in payload_hashes}, payload_hashes)
        operation._owned_fence()
        return {**result, 'cpu': cpu, 'database': old.plan.engine, 'fixture_https_only': True,
            'product_acme_not_exercised': True, 'application_opened': False}

    def prepare_captured_database(self, old, prepared, operation, identity):
        data = old.plan.resources['control'].root
        secrets = old.plan.resources['secrets']
        project = old.plan.name
        if old.plan.engine == 'mysql':
            old.docker.run(old.docker.base + ['up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'mysql'])
            self.wait_mysql(old.docker, old.observed['mysql']['container_id'])
        tools = os.environ.get('FC_OPERATOR_DISCOVERY_IMAGE', 'frontiercloud-python-archive:db-test-tools')
        program = """import sys
from app.core import db
if db.settings.DB_TYPE=='mysql':
    from sqlalchemy.ext.asyncio import create_async_engine
    db.engine=create_async_engine('mysql+asyncmy://root@mysql:3306/office_automation')
from app.services.offline_upgrade import main
raise SystemExit(main(sys.argv[1:]))
"""
        # This is explicitly a candidate-source helper component acceptance,
        # not activation of the prepared dummy application image above.
        base = ['docker', 'run', '--rm', '--name', project + '-database-prepare', '--network',
            old.plan.config['networks']['default']['name'] if old.plan.engine == 'mysql' else 'none',
            '--cpus', '1', '--memory', '512m', '--read-only', '--user', '10001:10001', '--workdir', '/app',
            '--mount', 'type=bind,source=' + str(Path(__file__).resolve().parents[1]) + ',target=/app,readonly',
            '--mount', 'type=bind,source=' + str(data) + ',target=/app/data',
            '--mount', 'type=volume,source=' + secrets.source + ',target=/run/frontiercloud-secrets',
            '-e', 'DB_TYPE=' + old.plan.engine, '-e', 'SQLITE_PATH=/app/data/frontiercloud.db', '-e', 'PYTHONDONTWRITEBYTECODE=1']
        fence = backup.digest(data / transactions.HARD)[1]
        prepare._command(old.docker, base + [tools, 'python', '-c', program, 'inspect', '--fence-sha256', fence,
            '--node-id', identity['node_id']], self.evidence, 'database-inspect')
        baseline = json.loads((self.evidence/'database-inspect.log').read_text().strip().splitlines()[-1])
        self.assertEqual(baseline['generation'], 4)
        path = self.evidence/'database-baseline.json'
        backup.private_write(path, backup.json_bytes(baseline))
        os.chown(path, 10001, 10001)
        arguments = base + ['--mount', 'type=bind,source=' + str(path) + ',target=/run/upgrade-baseline.json,readonly',
            tools, 'python', '-c', program, 'prepare', '--fence-sha256', fence, '--node-id', identity['node_id'],
            '--baseline', '/run/upgrade-baseline.json', '--baseline-sha256', backup.digest(path)[1]]
        prepare._command(old.docker, arguments, self.evidence, 'database-prepare')
        result = json.loads((self.evidence/'database-prepare.log').read_text().strip().splitlines()[-1])
        self.assertEqual((result['from_generation'], result['generation']), (4, 9))
        self.assertTrue(result['original_state_preserved'])
        prepare._command(old.docker, arguments, self.evidence, 'database-prepare-repeat')
        repeated = json.loads((self.evidence/'database-prepare-repeat.log').read_text().strip().splitlines()[-1])
        self.assertEqual(repeated, result)
        operation._owned_fence()
        self.assertTrue(all(row.get('Config', {}).get('Labels', {}).get(mapping.LABEL + 'service') == 'mysql'
            for row in old.docker.containers() if row.get('Config', {}).get('Labels', {}).get(mapping.LABEL + 'project') == project
            and row.get('State', {}).get('Running')))
        return {**result, 'repeat_identical': True, 'candidate_helper_only': True, 'new_application_started': False}

    def test_real_cold_capture_of_published_generation4_database_and_resume(self):
        self.cold_generation4("sqlite")

    def test_real_mysql_generation4_cold_capture_empty_target_restore_and_resume(self):
        self.cold_generation4("mysql")

    def test_real_published_sqlite_generation4_to_current_database_prepare(self):
        self.cold_generation4('sqlite', database_prepare=True)

    def test_real_published_mysql_generation4_to_current_database_prepare(self):
        self.cold_generation4('mysql', database_prepare=True)

    def test_real_host_transaction_prepares_published_sqlite_database(self):
        self.cold_generation4('sqlite', database_controller=True)

    def test_real_host_transaction_prepares_published_mysql_database(self):
        self.cold_generation4('mysql', database_controller=True)

    def test_real_candidate_configuration_failure_preserves_running_old_deployment(self):
        self.cold_generation4('sqlite', invalid_configuration=True)

    def test_real_fenced_sqlite_application_after_database_preparation(self):
        self.cold_generation4('sqlite', database_controller=True, fenced_application=True)

    def test_real_fenced_mysql_application_after_database_preparation(self):
        self.cold_generation4('mysql', database_controller=True, fenced_application=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
