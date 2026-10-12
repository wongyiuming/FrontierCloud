"""Actual offline Compose wrapper, existing DB/vault and production Web UID."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
import uuid

from scripts import operator_backup as backup
from tests import operator_backup_runtime as helpers


@unittest.skipUnless(os.environ.get("FC_OPERATOR_ENDPOINT_RUNTIME") == "1", "explicit isolated Docker-host endpoint acceptance")
class StorageEndpointRuntime(unittest.TestCase):
    def recover(self, engine):
        self.assertEqual(os.geteuid(), 0)
        fixture = helpers.ColdRecoveryRuntime()
        temporary = tempfile.TemporaryDirectory(prefix="endpoint-fixture-", dir="/root/frontiercloud-dev")
        self.addCleanup(temporary.cleanup)
        self.addCleanup(fixture.doCleanups)
        base = Path(temporary.name)
        source = fixture.prepare(base, "fc-ep-" + uuid.uuid4().hex[:12], engine)
        config = json.loads((source / "compose.json").read_text())
        web = config["services"]["web"]
        web["image"] = "frontiercloud-python-archive:endpoint-tools"
        web["user"] = "10001:10001"
        old, new = "https://old.storage.4399.com", "https://new.storage.4399.com"
        web["environment"] |= {"TLS_ENABLED": "true", "SERVER_NAME": "new.storage.4399.com", "DEPLOYMENT_MODE": "only_stroge",
                                "STORAGE_TRANSPORT": "direct", "STORAGE_PORT": "443", "STORAGE_ENDPOINT": new,
                                "PYTHONDONTWRITEBYTECODE": "1"}
        (source / "compose.json").write_text(json.dumps(config))
        fixture.compose(source, "up", "-d")
        docker = backup.Docker(source, [source / "compose.json"])
        plan = backup.Plan.load(docker)
        for key in ("control", "secrets"):
            os.chown(plan.resources[key].root, 10001, 10001)
            os.chmod(plan.resources[key].root, 0o700)
        if engine == "mysql":
            fixture.wait(source, "mysql")
            fixture.mysql(source, "CREATE USER 'media_admin'@'%' IDENTIFIED BY 'fixture-password'; GRANT ALL ON office_automation.* TO 'media_admin'@'%';")
            secret = plan.resources["secrets"].root / "mysql_password"
            secret.write_bytes(b"fixture-password")
            os.chmod(secret, 0o600)
            os.chown(secret, 10001, 10001)
        program = '''import asyncio,json,secrets,uuid
from pathlib import Path
from app.core import db
from app.services.federation.state import State
from app.services.federation import protocol as p
from app.services import media_objects
async def setup():
 await db.init_db()
 state=State(db.engine); await state.initialize(); await state.promote('Follower','https://old.storage.4399.com','fixture')
 package=await state.create_pair('fixture'); relationship=uuid.uuid4().hex
 peer_private=p.new_key()
 peer={'node_id':uuid.uuid4().hex,'role':'Master','endpoint':'https://master.4399.com','public_key':p.public_key(peer_private),'protocol':p.PROTOCOL_VERSION,'app_version':p.APP_VERSION}
 await state.consume(package['payload'],relationship,peer,p.encode(secrets.token_bytes(48))); await state.activate(relationship,'fixture')
 file=Path('/app/data/media/music/album/song.mp3'); file.parent.mkdir(parents=True); file.write_bytes(b'ID3-preserved-media')
 async with db.engine.begin() as conn: await media_objects.ensure_object(conn,'music/album/song.mp3','audio')
 print(json.dumps({'node_id':state.node['node_id'],'relationship':relationship})); await db.engine.dispose()
asyncio.run(setup())
'''
        identity = json.loads(fixture.compose(source, "exec", "-T", "web", "python", "-c", program))
        database = source / "data/frontiercloud.db"
        def facts():
            if engine == "mysql":
                value = fixture.mysql_facts(source)
                value["tables"] = {key.rsplit(".", 1)[-1]: item for key, item in value["tables"].items()}
                return value
            with closing(sqlite3.connect(database)) as connection:
                return backup.sqlite_facts(connection)
        def node():
            if engine == "mysql":
                return tuple(json.loads(fixture.mysql(source, "SELECT JSON_ARRAY(node_id,role,endpoint,private_key) FROM office_automation.node_identity WHERE singleton=1;")))
            with closing(sqlite3.connect(database)) as connection:
                return connection.execute("SELECT node_id,role,endpoint,private_key FROM node_identity").fetchone()
        before = facts()
        private = node()[3]
        vault = (plan.resources["secrets"].root / "node-vault.key").read_bytes()
        executable = Path(__file__).resolve().parents[1] / "scripts/operator_storage_endpoint.py"
        command = ["python3", str(executable), "storage-endpoint", "--project-directory", str(source),
                   "--compose-file", str(source / "compose.json"), "--confirm-node-id", identity["node_id"],
                   "--previous-endpoint", old, "--endpoint", new]
        value = json.loads(fixture.command(command))
        self.assertTrue(value["identity_preserved"])
        after = facts()
        self.assertEqual(node(), (identity["node_id"], "Follower", new, private))
        self.assertEqual({name: facts for name, facts in before["tables"].items() if name not in {"node_identity", "node_audit"}},
                         {name: facts for name, facts in after["tables"].items() if name not in {"node_identity", "node_audit"}})
        self.assertEqual((plan.resources["secrets"].root / "node-vault.key").read_bytes(), vault)
        self.assertEqual((source / "data/media/music/album/song.mp3").read_bytes(), b"ID3-preserved-media")
        lease = source / "data/.frontiercloud-runtime.lock"
        self.assertEqual(lease.stat().st_uid, 10001)
        self.assertEqual(lease.stat().st_mode & 0o777, 0o600)
        self.assertTrue((source / "data/.frontiercloud-native-maintenance").is_file())
        plan.assert_cold(allow_services=frozenset({"mysql"}) if engine == "mysql" else frozenset())
        fixture.command(command)
        self.assertEqual(facts(), after)
        wrong = command.copy()
        wrong[wrong.index("--confirm-node-id") + 1] = "a" * 32
        result = subprocess.run(wrong, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(facts(), after)
        evidence = os.environ.get("FC_OPERATOR_BACKUP_EVIDENCE")
        if evidence:
            backup.private_write(backup.absolute(evidence) / ("operator-endpoint-" + engine + "-proof.json"), backup.json_bytes({
                "engine": engine,
                "real_compose_wrapper": True, "web_uid": 10001, "existing_schema": True,
                "identity_pairing_credentials_preserved": True, "media_preserved": True,
                "maintenance_retained": True, "wrong_id_refused": True, "retry_noop": True,
                "tables": len(after["tables"]), "images": plan.images}))

    def test_sqlite_wrapper_preserves_identity_pairing_media_and_retry_under_web_uid(self):
        self.recover("sqlite")

    def test_mysql_wrapper_preserves_identity_pairing_media_and_retry_under_web_uid(self):
        self.recover("mysql")


if __name__ == "__main__":
    unittest.main()
