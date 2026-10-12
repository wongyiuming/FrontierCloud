"""Run on the Linux Docker host: FC_OPERATOR_BACKUP_RUNTIME=1 python -m ...

Disposable projects only; never loads the repository's deployment .env. No
public ports, no media source mounts and no business containers are touched.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
import uuid

from scripts import operator_backup as backup


@unittest.skipUnless(os.environ.get("FC_OPERATOR_BACKUP_RUNTIME") == "1", "explicit Docker-host cold recovery acceptance")
class ColdRecoveryRuntime(unittest.TestCase):
    def command(self, arguments, *, data=None):
        result = subprocess.run(arguments, input=data, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace")[-1200:])
        return result.stdout.decode().strip()

    def compose(self, directory, *arguments, data=None):
        return self.command(["docker", "compose", "--project-directory", str(directory), "-f", str(directory / "compose.json"), *arguments], data=data)

    def prepare(self, base, project, engine):
        directory = base / project
        directory.mkdir()
        (directory / "data").mkdir()
        app = "frontiercloud-python-archive:db-test-tools"
        shared = {"image": app, "command": ["python", "-c", "import time; time.sleep(3600)"], "cpus": 1,
                  "mem_limit": "128m", "read_only": True, "user": "0:0", "environment": {"PYTHONDONTWRITEBYTECODE": "1"}}
        config = {
            "name": project,
            "services": {
                "web": dict(shared, environment={"DB_TYPE": engine, "SQLITE_PATH": "/app/data/frontiercloud.db"},
                            volumes=[str(directory / "data") + ":/app/data", "runtime_secrets:/run/frontiercloud-secrets"]),
                "certificate-agent": dict(shared, volumes=["certificate_state:/var/lib/frontiercloud-certificate", "certificate_published:/run/frontiercloud-tls", "certificate_runtime:/run/frontiercloud-certificate"]),
                "redis": {"image": "redis:7.4.11-alpine", "command": ["redis-server", "--appendonly", "yes"], "cpus": 1, "mem_limit": "128m", "volumes": ["redis_data:/data"]},
            },
            "networks": {"default": {"internal": True}},
            "volumes": {key: {} for key in ("runtime_secrets", "redis_data", "certificate_state", "certificate_published", "certificate_runtime")},
        }
        if engine == "mysql":
            config["services"]["mysql"] = {"image": "mysql:8.4.11", "environment": {"MYSQL_ALLOW_EMPTY_PASSWORD": "yes", "MYSQL_DATABASE": "office_automation"},
                                            "command": ["--innodb-buffer-pool-size=64M", "--character-set-server=utf8mb4", "--collation-server=utf8mb4_unicode_ci"],
                                            "volumes": ["mysql_data:/var/lib/mysql", "mysql_socket:/var/run/mysqld"], "cpus": 1, "mem_limit": "512m"}
            config["volumes"] |= {"mysql_data": {}, "mysql_socket": {}}
        (directory / "compose.json").write_text(json.dumps(config))
        self.addCleanup(self.compose, directory, "down", "--volumes", "--remove-orphans", "--timeout", "10")
        return directory

    def wait(self, directory, service):
        for _ in range(90):
            command = ["redis-cli", "ping"] if service == "redis" else ["mysqladmin", "ping", "-h", "127.0.0.1", "-uroot", "--silent"]
            result = subprocess.run(["docker", "compose", "--project-directory", str(directory), "-f", str(directory / "compose.json"), "exec", "-T", service, *command], capture_output=True)
            if result.returncode == 0:
                return
            time.sleep(1)
        self.fail(service + " did not start in disposable fixture")

    def mysql(self, directory, sql):
        return self.compose(directory, "exec", "-T", "mysql", "mysql", "-uroot", "--batch", "--raw", "--skip-column-names", data=sql.encode())

    def bootstrap_real_database(self, directory, engine):
        source = Path(__file__).resolve().parents[1]
        config = backup.Docker(directory, [directory / "compose.json"]).config()
        program = "import asyncio; from app.core import db; "
        if engine == "mysql":
            program += 'from sqlalchemy.ext.asyncio import create_async_engine; db.engine=create_async_engine("mysql+asyncmy://root@mysql:3306/office_automation"); '
        program += "asyncio.run(db.init_db())"
        self.command(["docker", "run", "--rm", "--cpus=1", "--memory=512m", "--network", config["networks"]["default"]["name"],
                      "--tmpfs", "/tmp:size=2g", "-v", str(source) + ":/workspace:ro", "-v", str(directory / "data") + ":/app/data",
                      "-w", "/workspace", "-e", "DB_TYPE=" + engine, "-e", "SQLITE_PATH=/app/data/frontiercloud.db",
                      "-e", "PYTHONDONTWRITEBYTECODE=1", "frontiercloud-python-archive:db-test-tools", "python", "-c", program])

    def mysql_facts(self, directory):
        rows = self.mysql(directory, "SELECT TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,DATA_TYPE FROM information_schema.columns WHERE TABLE_SCHEMA NOT IN ('sys','mysql','performance_schema','information_schema') ORDER BY TABLE_SCHEMA,TABLE_NAME,ORDINAL_POSITION;")
        tables = {}
        definitions = {}
        for row in rows.splitlines():
            database, name, column, kind = row.split("\t")
            definitions.setdefault((database, name), []).append([column, kind])
        def literal(value):
            return "'" + value.replace("'", "''") + "'"
        def identifier(value):
            return "`" + value.replace("`", "``") + "`"
        statements = []
        records = {}
        for (database, name), typed_columns in definitions.items():
            values = ["CASE WHEN " + identifier(column) + " IS NULL THEN NULL ELSE HEX(CAST(" + identifier(column) + " AS BINARY)) END" for column, _kind in typed_columns]
            key = database + "." + name
            records[key] = []
            statements.append("SELECT JSON_OBJECT('table'," + literal(key) + ",'row',JSON_ARRAY(" + ",".join(values) + ")) FROM " + identifier(database) + "." + identifier(name) + ";")
        if statements:
            for line in self.mysql(directory, "\n".join(statements)).splitlines():
                value = json.loads(line)
                records[value["table"]].append(json.dumps(value["row"], ensure_ascii=False))
        for (database, name), typed_columns in definitions.items():
            encoded = sorted(records[database + "." + name])
            tables[database + "." + name] = {"columns": typed_columns, "rows": len(encoded), "sha256": hashlib.sha256(backup.json_bytes(encoded)).hexdigest()}
        # Verify recovery of account identity as well; volatile engine statistics
        # change during normal startup and are not application recovery facts.
        users = self.mysql(directory, "SELECT User,Host,plugin,authentication_string FROM mysql.user ORDER BY User,Host;")
        return {"tables": tables, "mysql_users_sha256": hashlib.sha256(users.encode()).hexdigest()}

    def recover(self, engine):
        self.assertEqual(os.geteuid(), 0, "run host fixture after sudo su")
        parent = Path("/root/frontiercloud-dev")
        parent.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="operator-fixture-", dir=parent)
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        nonce = uuid.uuid4().hex[:12]
        source = self.prepare(base, "fc-op-src-" + nonce, engine)
        destination = self.prepare(base, "fc-op-dst-" + nonce, engine)
        self.compose(source, "up", "-d")
        self.wait(source, "redis")
        docker = backup.Docker(source, [source / "compose.json"])
        plan = backup.Plan.load(docker)
        data = plan.resources["control"].root
        for folder in ("media", "recordings", "media-keys", "upload-leases"):
            (data / folder).mkdir()
        (data / "media/already-backed-up.mp3").write_bytes(b"media excluded")
        (data / "recordings/already-backed-up.webm").write_bytes(b"recording excluded")
        premaster = os.urandom(32)
        (data / "media-keys/media-premaster.key").write_bytes(premaster)
        os.chmod(data / "media-keys/media-premaster.key", 0o600)
        for key, name in (("secrets", "node-vault.key"), ("certificate_state", "account.json"), ("certificate_published", "current.json")):
            (plan.resources[key].root / name).write_bytes(b"test-private-" + key.encode())
            os.chmod(plan.resources[key].root / name, 0o600)
        runtime = docker.volume(plan.name + "_certificate_runtime")
        (Path(runtime["Mountpoint"]) / "not-restored-marker").write_bytes(b"ephemeral")
        if engine == "mysql":
            self.wait(source, "mysql")
        self.bootstrap_real_database(source, engine)
        if engine == "sqlite":
            with closing(sqlite3.connect(data / "frontiercloud.db")) as connection:
                connection.executescript("""PRAGMA journal_mode=WAL;
                    CREATE TABLE payload(id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, bytes BLOB);
                    INSERT INTO payload(label,bytes) VALUES('中文 binary',X'0080FF');
                    CREATE TABLE identity(singleton INTEGER PRIMARY KEY, public_id TEXT); INSERT INTO identity VALUES(1,'stable-node');
                """)
                facts = backup.sqlite_facts(connection)
        else:
            self.mysql(source, """USE office_automation;
                CREATE TABLE payload(id BIGINT AUTO_INCREMENT PRIMARY KEY, label VARCHAR(100), bytes BLOB); INSERT INTO payload(label,bytes) VALUES('中文 binary',X'0080FF');
                CREATE TABLE identity(singleton INTEGER PRIMARY KEY,public_id VARCHAR(100)); INSERT INTO identity VALUES(1,'stable-node');
            """)
            facts = self.mysql_facts(source)
        self.assertEqual(self.compose(source, "exec", "-T", "redis", "redis-cli", "SET", "cold-proof", "durable-redis-state"), "OK")
        output = base / "snapshot"
        # Exercise the real CLI as well as private archive implementation.
        self.command(["python3", str(Path(backup.__file__)), "backup", "--project-directory", str(source), "--compose-file", str(source / "compose.json"), "--output", str(output)])
        manifest = backup.verify(output)
        self.assertNotIn("certificate_runtime", manifest["resources"])
        self.assertNotIn("mysql_socket", manifest["resources"])
        self.assertFalse(any(item["State"]["Running"] for item in docker.containers() if item.get("Config", {}).get("Labels", {}).get("com.docker.compose.project") == plan.name))
        self.command(["python3", str(Path(backup.__file__)), "restore", "--project-directory", str(destination), "--compose-file", str(destination / "compose.json"), "--snapshot", str(output)])
        restored = backup.Plan.load(backup.Docker(destination, [destination / "compose.json"]))
        self.assertEqual((restored.resources["control"].root / "media-keys/media-premaster.key").read_bytes(), premaster)
        self.assertFalse((restored.resources["control"].root / "media").exists())
        self.assertFalse((restored.resources["control"].root / "recordings").exists())
        if engine == "sqlite":
            with closing(sqlite3.connect(destination / "data/frontiercloud.db")) as connection:
                self.assertEqual(backup.sqlite_facts(connection), facts)
        self.compose(destination, "up", "-d")
        self.wait(destination, "redis")
        self.assertEqual(self.compose(destination, "exec", "-T", "redis", "redis-cli", "GET", "cold-proof"), "durable-redis-state")
        target_docker = restored.docker
        target_runtime = target_docker.volume(restored.name + "_certificate_runtime")
        self.assertFalse((Path(target_runtime["Mountpoint"]) / "not-restored-marker").exists())
        if engine == "mysql":
            self.wait(destination, "mysql")
            self.assertEqual(self.mysql_facts(destination), facts)
        for key, name in (("secrets", "node-vault.key"), ("certificate_state", "account.json"), ("certificate_published", "current.json")):
            self.assertEqual((restored.resources[key].root / name).read_bytes(), b"test-private-" + key.encode())
        proof = {"engine": engine, "snapshot_verified": True, "actual_restore": True, "redis_restored": True,
                 "media_excluded": True, "private_keys_restored": True, "runtime_not_restored": True,
                 "database_fact_sha256": hashlib.sha256(backup.json_bytes(facts)).hexdigest(),
                 "tables": sorted(facts["tables"]), "table_facts": facts["tables"],
                 "images": manifest["images"]}
        evidence = os.environ.get("FC_OPERATOR_BACKUP_EVIDENCE")
        if evidence:
            target = backup.absolute(evidence)
            backup.private_write(target / ("operator-" + engine + "-proof.json"), backup.json_bytes(proof))

    def test_sqlite_compose_cold_snapshot_and_actual_sql_restore(self):
        self.recover("sqlite")

    def test_mysql_84_cold_volume_restore_with_all_fixture_tables(self):
        self.recover("mysql")


if __name__ == "__main__":
    unittest.main()
