"""Private offline snapshots: full SQLite facts, safe paths, coherent control state."""
from contextlib import closing,ExitStack
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch

from scripts import operator_backup as backup


class FakeDocker:
    def __init__(self, base, name, *, engine="sqlite", initialized=True):
        self.directory = base / name
        self.directory.mkdir()
        self.root = base / "docker"
        self.root.mkdir(exist_ok=True)
        self.data = self.directory / "data"
        self.data.mkdir()
        self.stopped = False
        self.containers_value = []
        self.volumes = {}
        self.images = {service: {"id": "sha256:" + service, "architecture": "amd64", "os": "linux"}
                       for service in ("web", "redis", "mysql", "certificate-agent")}
        targets = {"runtime_secrets": "/run/frontiercloud-secrets", "redis_data": "/data",
                   "certificate_state": "/var/lib/frontiercloud-certificate", "certificate_published": "/run/frontiercloud-tls"}
        definitions = {key: {"name": name + "_" + key} for key in targets}
        definitions["mysql_data"] = {"name": name + "_mysql_data"}
        definitions["certificate_runtime"] = {"name": name + "_certificate_runtime"}
        services = {
            "web": {"image": "web", "environment": {"DB_TYPE": engine, "SQLITE_PATH": "/app/data/frontiercloud.db"},
                    "volumes": [{"type": "bind", "source": str(self.data), "target": "/app/data"},
                                {"type": "volume", "source": "runtime_secrets", "target": "/run/frontiercloud-secrets"}]},
            "redis": {"image": "redis", "volumes": [{"type": "volume", "source": "redis_data", "target": "/data"}]},
            "mysql": {"image": "mysql", "volumes": [{"type": "volume", "source": "mysql_data", "target": "/var/lib/mysql"}]},
            "certificate-agent": {"image": "certificate-agent", "volumes": [
                {"type": "volume", "source": "certificate_state", "target": "/var/lib/frontiercloud-certificate"},
                {"type": "volume", "source": "certificate_published", "target": "/run/frontiercloud-tls"},
                {"type": "volume", "source": "certificate_runtime", "target": "/run/frontiercloud-certificate"}]},
        }
        self.config_value = {"name": name, "volumes": definitions, "services": services}
        if initialized:
            for key in targets:
                self.create_volume(key)
            if engine == "mysql":
                self.create_volume("mysql_data")

    def create_volume(self, key):
        name = self.config_value["volumes"][key]["name"]
        root = self.root / "volumes" / name / "_data"
        root.mkdir(parents=True)
        self.volumes[name] = {"Name": name, "Driver": "local", "Options": None, "Mountpoint": str(root),
                              "Labels": {"com.docker.compose.project": self.config_value["name"], "com.docker.compose.volume": key}}

    def config(self):
        return copy.deepcopy(self.config_value)

    def local_root(self):
        return self.root

    def volume(self, name):
        return self.volumes.get(name)

    def image_id(self, name):
        return self.images[name]

    def containers(self):
        return self.containers_value

    def stop(self):
        self.stopped = True
        for container in self.containers_value:
            if container["Config"]["Labels"].get("com.docker.compose.project") == self.config_value["name"]:
                container["State"]["Running"] = False

    def run(self, command):
        if command[:3] != ["docker", "volume", "create"]:
            raise AssertionError(command)
        self.create_volume(next(key for key, item in self.config_value["volumes"].items() if item["name"] == command[-1]))


def populate(plan):
    root = plan.resources["control"].root
    for folder in ("media", "recordings", "media-keys", "upload-leases"):
        (root / folder).mkdir()
    (root / "media/music.mp3").write_bytes(b"not copied media")
    (root / "recordings/take.webm").write_bytes(b"not copied recording")
    (root / "media-keys/media-premaster.key").write_bytes(b"\0\x80\xffpremaster")
    (root / "upload-leases/abcd.lock").write_bytes(b"")
    (root / "node-identity.json").write_text('{"role":"Master"}')
    with closing(sqlite3.connect(root / "frontiercloud.db")) as connection:
        connection.executescript("""CREATE TABLE schema_version(singleton INTEGER PRIMARY KEY, generation INTEGER);
            INSERT INTO schema_version VALUES(1,4);
            CREATE TABLE parent(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE);
            CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id), payload BLOB, optional TEXT);
            INSERT INTO parent(name) VALUES('quote'' and 中文');
            INSERT INTO child VALUES(2,1,X'0080FF0A',NULL);
            CREATE INDEX child_parent ON child(parent_id);
            CREATE VIEW visible AS SELECT name FROM parent;
            CREATE TRIGGER parent_change AFTER UPDATE ON parent BEGIN UPDATE child SET optional='changed' WHERE parent_id=NEW.id; END;
        """)
    for key, resource in plan.resources.items():
        if key == "control":
            continue
        name = {"secrets": "node-vault.key", "certificate_state": "account.json", "certificate_published": "current.json", "redis": "appendonly.aof", "mysql": "ibdata1"}[key]
        (resource.root / name).write_bytes(b"\0\xff" + key.encode())
        os.chmod(resource.root / name, 0o600)


@unittest.skipUnless(sys.platform == "linux", "operator restore uses Linux ownership and Docker volumes")
class OperatorBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.writer_guard = patch.object(backup, "assert_no_open_writers")
        self.writer_guard.start()
        self.addCleanup(self.writer_guard.stop)
        self.docker = FakeDocker(self.base, "source")
        self.plan = backup.Plan.load(self.docker)
        populate(self.plan)

    def snapshot(self, name="snapshot"):
        path = self.base / name
        manifest = backup.backup(self.plan, path)
        self.assertTrue(self.docker.stopped)
        return path, manifest

    def target(self, *, engine="sqlite"):
        docker = FakeDocker(self.base, "target", initialized=False, engine=engine)
        plan = backup.Plan.load(docker, restoring=True, snapshot_resources=set(self.plan.resources))
        return docker, plan

    def test_full_sqlite_sql_roundtrip_and_private_control_snapshot(self):
        path, manifest = self.snapshot()
        self.assertEqual(backup.verify(path), manifest)
        self.assertEqual(manifest["sqlite"]["facts"]["tables"]["child"]["rows"], 1)
        self.assertIn("sqlite_sequence", manifest["sqlite"]["facts"]["tables"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(entry.stat().st_mode & 0o777 == 0o600 for entry in path.iterdir()))
        files = manifest["resources"]["control"]["inventory"]
        self.assertIn("media-keys/media-premaster.key", files)
        self.assertIn("node-identity.json", files)
        self.assertNotIn("media", files)
        self.assertNotIn("recordings", files)
        self.assertNotIn("frontiercloud.db", files)
        self.assertNotIn("certificate_runtime", manifest["resources"])
        self.assertEqual(set(manifest["resources"]), {"control", "secrets", "redis", "certificate_state", "certificate_published"})

    def test_restore_new_project_reproduces_every_table_and_private_fact(self):
        path, manifest = self.snapshot()
        docker, target = self.target()
        backup.restore(target, path)
        self.assertFalse(docker.stopped)
        with closing(sqlite3.connect(target.resources["control"].root / "frontiercloud.db")) as connection:
            self.assertEqual(backup.sqlite_facts(connection), manifest["sqlite"]["facts"])
            self.assertEqual(connection.execute("SELECT payload FROM child").fetchone()[0], b"\0\x80\xff\n")
            connection.execute("UPDATE parent SET name='new' WHERE id=1")
            self.assertEqual(connection.execute("SELECT optional FROM child").fetchone()[0], "changed")
        for key in manifest["resources"]:
            if key == "control":
                self.assertEqual((target.resources[key].root / "media-keys/media-premaster.key").read_bytes(), b"\0\x80\xffpremaster")
            else:
                self.assertEqual(sorted(path.name for path in target.resources[key].root.iterdir()), sorted(path.name for path in self.plan.resources[key].root.iterdir()))

    def test_nonempty_restore_refuses_before_creating_any_other_volume(self):
        path, _ = self.snapshot()
        docker, target = self.target()
        (target.resources["control"].root / "keep").write_bytes(b"protected")
        with self.assertRaisesRegex(backup.BackupError, "empty"):
            backup.restore(target, path)
        self.assertEqual(docker.volumes, {})
        self.assertEqual((target.resources["control"].root / "keep").read_bytes(), b"protected")

    def test_active_project_or_other_writer_blocks_restore_and_backup(self):
        path, _ = self.snapshot()
        docker, target = self.target()
        docker.containers_value = [{"Config": {"Labels": {"com.docker.compose.project": "target", "com.docker.compose.service": "web"}}, "State": {"Running": True}, "Mounts": []}]
        with self.assertRaisesRegex(backup.BackupError, "active"):
            backup.restore(target, path)
        self.docker.stopped = False
        self.docker.containers_value = [{"Config": {"Labels": {}}, "State": {"Running": True}, "Mounts": [{"Source": str(self.docker.data)}]}]
        with self.assertRaisesRegex(backup.BackupError, "another active"):
            backup.backup(self.plan, self.base / "second")
        self.assertFalse(self.docker.stopped)

    def test_corruption_private_permissions_and_unknown_artifacts_are_rejected(self):
        path, _ = self.snapshot()
        sql = path / "all-tables.sql"
        os.chmod(sql, 0o644)
        with self.assertRaisesRegex(backup.BackupError, "private"):
            backup.verify(path)
        os.chmod(sql, 0o600)
        sql.write_bytes(sql.read_bytes() + b"SELECT 1;\n")
        with self.assertRaisesRegex(backup.BackupError, "checksum"):
            backup.verify(path)

    def test_symbolic_links_in_control_state_or_output_are_rejected(self):
        (self.docker.data / "escape").symlink_to(self.base / "outside")
        with self.assertRaisesRegex(backup.BackupError, "symbolic"):
            self.snapshot()
        self.assertFalse((self.base / "snapshot/manifest.json").exists())
        alias = self.base / "alias"
        alias.symlink_to(self.docker.data, target_is_directory=True)
        with self.assertRaisesRegex(backup.BackupError, "symbolic"):
            backup.absolute(alias / "new", exists=False)

    def test_incomplete_snapshot_has_a_clear_error_and_cannot_mutate_restore(self):
        path = self.base / "incomplete"
        path.mkdir(mode=0o700)
        backup.private_write(path / "compose-resolved.json", b"{}")
        with self.assertRaisesRegex(backup.BackupError, "incomplete snapshot.*manifest"):
            backup.verify(path)
        docker, target = self.target()
        with self.assertRaisesRegex(backup.BackupError, "incomplete snapshot"):
            backup.restore(target, path)
        self.assertEqual(docker.volumes, {})
        self.assertEqual(list(target.resources["control"].root.iterdir()), [])

    def test_snapshot_parent_is_synced_before_completion_is_published(self):
        calls = []
        original = backup.fsync_directory
        def synced(path):
            calls.append((path, (self.base / "snapshot/manifest.json").exists()))
            original(path)
        with patch.object(backup, "fsync_directory", side_effect=synced):
            path, manifest = self.snapshot()
        self.assertGreaterEqual(sum(directory == self.base for directory, _ in calls), 2)
        self.assertTrue(all(not complete for directory, complete in calls if directory == self.base))
        self.assertEqual(calls[-1], (path, True))
        self.assertEqual(backup.verify(path), manifest)

    def test_failed_parent_fsync_never_publishes_a_completion_manifest(self):
        original = backup.fsync_directory
        def fail_parent(path):
            if path == self.base:
                raise OSError("injected parent durability failure")
            original(path)
        with patch.object(backup, "fsync_directory", side_effect=fail_parent):
            with self.assertRaises(OSError):
                self.snapshot()
        self.assertFalse((self.base / "snapshot/manifest.json").exists())
        with self.assertRaisesRegex(backup.BackupError, "incomplete snapshot"):
            backup.verify(self.base / "snapshot")

    def test_sqlite_path_outside_control_or_inside_excluded_roots_is_rejected(self):
        for value in ("/tmp/database.db", "/app/data/media/db.sqlite", "/app/data/recordings/db.sqlite", "/app/data/tmp/db.sqlite", "/app/data/.tmp/db.sqlite", "/app/data/../outside.sqlite"):
            with self.subTest(path=value):
                self.docker.config_value["services"]["web"]["environment"]["SQLITE_PATH"] = value
                with self.assertRaises(backup.BackupError):
                    backup.Plan.load(self.docker)

    def test_archive_path_escape_link_duplicate_and_mode_are_rejected(self):
        for name, kind, mode in (("../outside", tarfile.REGTYPE, 0o600), ("link", tarfile.SYMTYPE, 0o600), ("badmode", tarfile.REGTYPE, 0o4600)):
            with self.subTest(name=name):
                path = self.base / ("evil-" + name.replace("/", "_"))
                inventory = {".": {"type": "directory", "mode": 0o700, "uid": 0, "gid": 0}}
                with tarfile.open(path, "w") as archive:
                    root = tarfile.TarInfo(".")
                    root.type, root.mode = tarfile.DIRTYPE, 0o700
                    archive.addfile(root)
                    item = tarfile.TarInfo(name)
                    item.mode, item.type = mode, kind
                    archive.addfile(item, io.BytesIO(b""))
                with self.assertRaises(backup.BackupError):
                    backup.validate_archive(path, inventory)

    def test_unknown_mount_external_volume_and_wrong_ownership_fail_closed(self):
        config = copy.deepcopy(self.docker.config_value)
        self.docker.config_value["services"]["web"]["volumes"].append({"type": "bind", "source": "/root", "target": "/unknown"})
        with self.assertRaisesRegex(backup.BackupError, "unknown"):
            backup.Plan.load(self.docker)
        self.docker.config_value = copy.deepcopy(config)
        self.docker.config_value["volumes"]["runtime_secrets"]["external"] = True
        with self.assertRaisesRegex(backup.BackupError, "external"):
            backup.Plan.load(self.docker)
        self.docker.config_value = config
        self.docker.volumes["source_runtime_secrets"]["Labels"]["com.docker.compose.project"] = "different"
        with self.assertRaisesRegex(backup.BackupError, "ownership"):
            backup.Plan.load(self.docker)

    def test_mysql_cold_volume_requires_same_image_and_can_restore_to_new_project(self):
        mysql = FakeDocker(self.base, "mysql-source", engine="mysql")
        plan = backup.Plan.load(mysql)
        for key, resource in plan.resources.items():
            (resource.root / ("identity" if key == "control" else "bytes")).write_bytes(key.encode())
        path = self.base / "mysql-snapshot"
        backup.backup(plan, path)
        docker, target = self.target(engine="mysql")
        docker.images["mysql"] = {"id": "sha256:different", "architecture": "amd64", "os": "linux"}
        target.images = docker.images
        with self.assertRaisesRegex(backup.BackupError, "exact recorded"):
            backup.restore(target, path)
        docker.images["mysql"] = mysql.images["mysql"]
        backup.restore(target, path)
        self.assertEqual((target.resources["mysql"].root / "bytes").read_bytes(), b"mysql")

    def test_sql_artifact_cannot_attach_or_write_outside_restored_database(self):
        escaped = self.base / "outside.db"
        with closing(sqlite3.connect(":memory:")) as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                backup.sql_restore(connection, "ATTACH DATABASE '" + str(escaped) + "' AS outside; CREATE TABLE outside.stolen(x);")
        self.assertFalse(escaped.exists())

    def test_backup_output_cannot_overlap_source_tree(self):
        with self.assertRaisesRegex(backup.BackupError, "overlaps"):
            backup.backup(self.plan, self.docker.data / "recursive-snapshot")
        self.assertFalse(self.docker.stopped)


class OperationLockTests(unittest.TestCase):
    """Controlled descriptor observations never open or weaken the host lock."""
    def setUp(self):
        stack=ExitStack();self.addCleanup(stack.close)
        self.fcntl=SimpleNamespace(LOCK_EX=2,LOCK_NB=4,flock=Mock())
        stack.enter_context(patch.dict(sys.modules,{'fcntl':self.fcntl}))
        stack.enter_context(patch.object(backup.os,'O_NOFOLLOW',getattr(os,'O_NOFOLLOW',0x20000),create=True))
        self.open=stack.enter_context(patch.object(backup.os,'open',return_value=73))
        self.info=SimpleNamespace(st_mode=stat.S_IFREG|0o600,st_nlink=1,st_uid=0)
        self.fstat=stack.enter_context(patch.object(backup.os,'fstat',return_value=self.info))
        self.close=stack.enter_context(patch.object(backup.os,'close'))

    def test_root_owned_regular_single_link_lock_is_exclusive_and_closed(self):
        with backup.operation_lock('fixture-project'):
            self.open.assert_called_once_with(Path('/var/lock/frontiercloud-snapshot-fixture-project.lock'),
                os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
            self.fstat.assert_called_once_with(73)
            self.fcntl.flock.assert_called_once_with(73,self.fcntl.LOCK_EX|self.fcntl.LOCK_NB)
            self.close.assert_not_called()
        self.close.assert_called_once_with(73)

    def test_nonroot_nonregular_or_multiple_links_refuse_before_flock(self):
        for changes in ({'st_uid':1001},{'st_mode':stat.S_IFIFO|0o600},{'st_nlink':2}):
            with self.subTest(changes=changes):
                self.fstat.return_value=SimpleNamespace(**{**vars(self.info),**changes})
                with self.assertRaisesRegex(backup.BackupError,'^unsafe operator lock$'):
                    with backup.operation_lock('fixture-project'):self.fail('unsafe lock entered')
                self.fcntl.flock.assert_not_called();self.close.assert_called_once_with(73)
                self.close.reset_mock()

    def test_contended_lock_refuses_and_closes_descriptor(self):
        self.fcntl.flock.side_effect=BlockingIOError()
        with self.assertRaisesRegex(backup.BackupError,'another snapshot operation'):
            with backup.operation_lock('fixture-project'):self.fail('contended lock entered')
        self.close.assert_called_once_with(73)

    def test_body_failure_is_preserved_and_descriptor_closed(self):
        error=RuntimeError('unit business failure')
        with self.assertRaises(RuntimeError) as caught:
            with backup.operation_lock('fixture-project'):raise error
        self.assertIs(caught.exception,error);self.close.assert_called_once_with(73)

    def test_open_failure_does_not_lock_or_close_an_unopened_descriptor(self):
        error=OSError('unit no-follow/open failure');self.open.side_effect=error
        with self.assertRaises(OSError) as caught:
            with backup.operation_lock('fixture-project'):self.fail('unopened lock entered')
        self.assertIs(caught.exception,error);self.fstat.assert_not_called()
        self.fcntl.flock.assert_not_called();self.close.assert_not_called()


class SQLiteFactsTests(unittest.TestCase):
    def test_binary_text_null_and_numeric_facts_are_distinct(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.executescript("CREATE TABLE payload(value); INSERT INTO payload VALUES(X'FF00'),('ff00'),(NULL),(1),(1.5);")
            initial = backup.sqlite_facts(connection)
            self.assertEqual(initial["tables"]["payload"]["rows"], 5)
            connection.execute("UPDATE payload SET value=X'FF01' WHERE typeof(value)='blob'")
            self.assertNotEqual(initial, backup.sqlite_facts(connection))

    def test_foreign_key_inconsistency_does_not_receive_successful_proof(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.executescript("CREATE TABLE p(id PRIMARY KEY); CREATE TABLE c(parent REFERENCES p(id)); INSERT INTO c VALUES(9);")
            with self.assertRaisesRegex(backup.BackupError, "foreign key"):
                backup.sqlite_facts(connection)


if __name__ == "__main__":
    unittest.main()
