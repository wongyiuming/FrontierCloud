#!/usr/bin/env python3
"""Offline, operator-only Compose snapshots. Never imported by the Web service.

Backup stops the whole selected Compose project and leaves it stopped. Restore
requires empty, inactive destinations; it never removes existing data. Media
and recordings are deliberately excluded and must be restored separately.
Named volumes use Docker's local driver on this Linux host. MySQL recovery is a
cold volume recovery with the exact same image, not a cross-version SQL import.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

VERSION = 1
EXCLUDED_DATA = frozenset({"media", "recordings"})
SQLITE_EXCLUDED_ROOTS = EXCLUDED_DATA | {"tmp", ".tmp"}
PERSISTENT_TARGETS = {
    "/app/data": "control", "/run/frontiercloud-secrets": "secrets",
    "/var/lib/frontiercloud-certificate": "certificate_state",
    "/run/frontiercloud-tls": "certificate_published",
}
EPHEMERAL_TARGETS = frozenset({"/tmp", "/run/frontiercloud-certificate", "/var/run/mysqld"})
KNOWN_SERVICES = frozenset({"web", "redis", "mysql", "nginx", "stun", "secrets-init", "media-init", "certificate-init", "certificate-agent"})
RESOURCE_KEYS = frozenset(PERSISTENT_TARGETS.values()) | {"redis", "mysql"}
NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")


class BackupError(RuntimeError):
    pass


def json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()


def relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise BackupError("invalid relative artifact path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise BackupError("artifact path escapes its destination")
    return path


def absolute(value: str | Path, *, exists=True) -> Path:
    """Check every existing component without resolving/following symlinks."""
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise BackupError("an explicit absolute non-root path is required")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            if exists:
                raise BackupError("required path does not exist") from None
            continue
        if stat.S_ISLNK(info.st_mode):
            raise BackupError("symbolic links are not supported in snapshot paths")
    if exists and not path.exists():
        raise BackupError("required path does not exist")
    return path


def digest(path: Path) -> tuple[int, str]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BackupError("snapshot files must be regular, unlinked files")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    result = hashlib.sha256()
    size = 0
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise BackupError("snapshot file changed while opening")
        while chunk := source.read(1024 * 1024):
            result.update(chunk)
            size += len(chunk)
        after = os.fstat(source.fileno())
    if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise BackupError("snapshot file changed while reading")
    return size, result.hexdigest()


def private_write(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def bounded_json(path: Path, limit=64 * 1024 * 1024):
    size, _ = digest(path)
    if size > limit:
        raise BackupError("snapshot metadata exceeds its limit")
    with path.open("rb") as stream:
        try:
            return json.load(stream)
        except (ValueError, TypeError):
            raise BackupError("invalid snapshot metadata") from None


def quoted(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sqlite_facts(connection: sqlite3.Connection) -> dict:
    """All real tables, including sqlite_sequence; typed binary-safe row proof."""
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise BackupError("SQLite integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise BackupError("SQLite foreign key check failed")
    tables = {}
    for (name,) in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"):
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted(name)})")]
        order = ",".join(quoted(column) for column in columns)
        checksum = hashlib.sha256()
        count = 0
        for row in connection.execute(f"SELECT * FROM {quoted(name)} ORDER BY {order}"):
            # Distinguish blob/text/null/numeric values rather than JSON coercion.
            encoded = json_bytes([["blob", base64.b64encode(value).decode()] if isinstance(value, bytes)
                                  else [type(value).__name__, value] for value in row])
            checksum.update(len(encoded).to_bytes(8, "big"))
            checksum.update(encoded)
            count += 1
        tables[name] = {"columns": columns, "rows": count, "sha256": checksum.hexdigest()}
    schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name").fetchall()
    return {"tables": tables, "schema_sha256": hashlib.sha256(json_bytes(schema)).hexdigest()}


def sql_restore(connection: sqlite3.Connection, sql: str | Path) -> None:
    """An archive is data, never permission to attach/write another database."""
    def authorize(action, first, second, _database, _trigger):
        if action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_CREATE_VTABLE}:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and (first.lower() != "foreign_keys" or (second or "").lower() not in {"on", "off", "0", "1"}):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION and (second or "").lower() in {"load_extension", "readfile", "writefile"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    connection.set_authorizer(authorize)
    try:
        # Keep memory bounded by one SQL statement instead of the entire dump.
        with (sql.open("r", encoding="utf-8") if isinstance(sql, Path) else io.StringIO(sql)) as source:
            statement = ""
            for line in source:
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ""
            if statement.strip():
                raise BackupError("SQLite SQL dump contains an incomplete statement")
    finally:
        connection.set_authorizer(None)


def sqlite_snapshot(source: Path, output: Path) -> dict:
    digest(source)  # No symlink/hardlink; SQLite URI is constructed by pathlib.
    database = output / "sqlite.db"
    private_write(database, b"")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original, closing(sqlite3.connect(database)) as copied:
        original.backup(copied)
        # Seal a standalone artifact, independent of the source's live WAL mode.
        copied.execute("PRAGMA journal_mode=DELETE")
        facts = sqlite_facts(copied)
        descriptor = os.open(output / "all-tables.sql", os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as sql_file:
            for statement in copied.iterdump():
                sql_file.write(statement + "\n")
            sql_file.flush()
            os.fsync(sql_file.fileno())
    private_write(output / "sqlite-proof.json", json_bytes(facts))
    # Test the *actual generated SQL*, not merely the backup() copy.
    with tempfile.TemporaryDirectory(prefix="sql-proof-", dir=output) as temporary:
        restored = Path(temporary) / "restored.db"
        with closing(sqlite3.connect(restored)) as connection:
            sql_restore(connection, output / "all-tables.sql")
            if sqlite_facts(connection) != facts:
                raise BackupError("SQLite SQL round-trip changed database facts")
    return facts


@dataclass
class Resource:
    key: str
    kind: str
    source: str
    root: Path | None = None
    volume_key: str | None = None


class Docker:
    def __init__(self, directory: Path, files: list[Path]):
        self.directory = absolute(directory)
        self.base = ["docker", "compose", "--project-directory", str(self.directory)]
        for path in files:
            self.base += ["-f", str(absolute(path))]

    def run(self, arguments: list[str], *, json_output=False):
        result = subprocess.run(arguments, cwd=self.directory, capture_output=True, check=False)
        if result.returncode:
            # Docker config errors can contain expanded secrets; never echo them.
            raise BackupError("Docker command failed; inspect Docker locally for diagnostics")
        if json_output:
            try:
                return json.loads(result.stdout)
            except ValueError:
                raise BackupError("Docker returned invalid JSON") from None
        return result.stdout.decode().strip()

    def config(self):
        return self.run(self.base + ["--profile", "*", "config", "--format", "json"], json_output=True)

    def containers(self):
        ids = self.run(["docker", "ps", "-aq"]).splitlines()
        return self.run(["docker", "inspect", *ids], json_output=True) if ids else []

    def volume(self, name: str):
        names = self.run(["docker", "volume", "ls", "--format", "{{.Name}}"]).splitlines()
        if name not in names:
            return None
        return self.run(["docker", "volume", "inspect", name], json_output=True)[0]

    def image_id(self, name: str):
        value = self.run(["docker", "image", "inspect", name], json_output=True)[0]
        return {"id": value["Id"], "architecture": value["Architecture"], "os": value["Os"]}

    def stop(self):
        self.run(self.base + ["--profile", "*", "stop", "--timeout", "30"])

    def local_root(self):
        contexts = self.run(["docker", "context", "inspect"], json_output=True)
        if not contexts[0]["Endpoints"]["docker"]["Host"].startswith("unix://"):
            raise BackupError("cold-volume snapshots require a local Linux Docker context")
        info = self.run(["docker", "info", "--format", "{{json .}}"], json_output=True)
        if info.get("OSType") != "linux":
            raise BackupError("cold-volume snapshots require a local Linux Docker daemon")
        return absolute(info["DockerRootDir"])


@dataclass
class Plan:
    docker: Docker
    config: dict
    name: str
    engine: str
    resources: dict[str, Resource]
    sqlite_path: PurePosixPath | None
    images: dict
    docker_root: Path

    @classmethod
    def load(cls, docker: Docker, *, restoring=False, snapshot_resources=None):
        config = docker.config()
        name = config.get("name", "")
        if not NAME.fullmatch(name) or not isinstance(config.get("services"), dict):
            raise BackupError("invalid Compose project")
        services = config["services"]
        if "web" not in services or "redis" not in services or set(services) - KNOWN_SERVICES:
            raise BackupError("unknown Compose service topology; review it before taking a snapshot")
        engine = services["web"].get("environment", {}).get("DB_TYPE", "sqlite")
        if engine not in {"sqlite", "mysql"}:
            raise BackupError("unsupported database engine")
        resources = {}
        volumes = config.get("volumes", {})
        for service, definition in services.items():
            for mount in definition.get("volumes", []):
                target = mount.get("target")
                key = PERSISTENT_TARGETS.get(target)
                if target == "/data" and service == "redis":
                    key = "redis"
                if target == "/var/lib/mysql" and service == "mysql":
                    key = "mysql"
                if key is None:
                    if target in EPHEMERAL_TARGETS:
                        continue
                    raise BackupError("unknown persistent mount; snapshot would be incomplete")
                kind, source = mount.get("type"), mount.get("source")
                if not isinstance(source, str) or (key == "control" and kind != "bind") or (key != "control" and kind != "volume"):
                    raise BackupError("unsupported persistent mount type")
                if kind == "volume":
                    definition = volumes.get(source)
                    if not isinstance(definition, dict) or definition.get("external") or definition.get("driver", "local") != "local" or definition.get("driver_opts"):
                        raise BackupError("external or non-local volumes require their own backup procedure")
                    actual = definition.get("name", name + "_" + source)
                    if not NAME.fullmatch(actual):
                        raise BackupError("invalid volume name")
                else:
                    actual = str(absolute(source, exists=not restoring))
                    if len(Path(actual).parts) < 3 or actual in {"/var/lib", "/root", "/home", "/etc", "/usr", "/var/lib/docker"}:
                        raise BackupError("refusing a broad/system data directory")
                resource = Resource(key, kind, actual, volume_key=source if kind == "volume" else None)
                if key in resources and (resources[key].kind, resources[key].source) != (kind, actual):
                    raise BackupError("multiple distinct mounts claim the same persistent resource")
                resources[key] = resource
        if not {"control", "secrets", "redis"}.issubset(resources):
            raise BackupError("required control/secrets/Redis resources are missing")
        if engine == "mysql" and "mysql" not in resources:
            raise BackupError("MySQL data volume is missing")
        if ("certificate_state" in resources) != ("certificate_published" in resources):
            raise BackupError("certificate private/published resources must be backed up together")
        sqlite_path = None
        if engine == "sqlite":
            value = services["web"].get("environment", {}).get("SQLITE_PATH", "/app/data/frontiercloud.db")
            if not isinstance(value, str) or not value.startswith("/app/data/"):
                raise BackupError("SQLite must reside within the persistent DATA_DIRECTORY")
            sqlite_path = relative(value[len("/app/data/"):])
            if sqlite_path.parts[0] in SQLITE_EXCLUDED_ROOTS:
                raise BackupError("SQLite cannot reside in excluded media or temporary directories")
        docker_root = docker.local_root()
        plan = cls(docker, config, name, engine, resources, sqlite_path, {}, docker_root)
        for key in list(resources):
            resource = resources[key]
            resource.root = plan.volume_root(resource) if resource.kind == "volume" else absolute(resource.source, exists=not restoring)
            if resource.root is None:
                if key == "mysql" and engine != "mysql" and (not restoring or snapshot_resources is not None and key not in snapshot_resources):
                    del resources[key]
                elif not restoring:
                    raise BackupError("required persistent volume does not exist; initialize the deployment first")
        for service in ("web", "mysql", "redis", "certificate-agent", "nginx"):
            if service in services and (service != "mysql" or "mysql" in resources):
                image = services[service].get("image")
                if not isinstance(image, str) or not image:
                    raise BackupError("persistent services require a named, locally available image")
                plan.images[service] = docker.image_id(image)
        roots = [resource.root for resource in resources.values() if resource.root]
        for number, root in enumerate(roots):
            if any(root == other or root in other.parents or other in root.parents for other in roots[number + 1:]):
                raise BackupError("persistent source directories overlap")
        return plan

    def volume_root(self, resource: Resource):
        value = self.docker.volume(resource.source)
        if value is None:
            return None
        if value.get("Driver") != "local" or value.get("Options"):
            raise BackupError("only plain local Docker volumes can be snapshotted")
        labels = value.get("Labels") or {}
        if labels.get("com.docker.compose.project") != self.name or labels.get("com.docker.compose.volume") != resource.volume_key:
            raise BackupError("volume ownership does not match the selected Compose project")
        root = absolute(value["Mountpoint"])
        if root != self.docker_root / "volumes" / resource.source / "_data" or not root.is_dir():
            raise BackupError("Docker volume mountpoint is outside its expected local directory")
        return root

    def assert_cold(self, *, allow_project=False, allow_services=frozenset()):
        roots = [resource.root for key, resource in self.resources.items() if resource.root and not (key == "mysql" and "mysql" in allow_services)]
        names = {resource.source for resource in self.resources.values() if resource.kind == "volume"}
        for container in self.docker.containers():
            labels = container.get("Config", {}).get("Labels") or {}
            own = labels.get("com.docker.compose.project") == self.name
            if own and labels.get("com.docker.compose.service") not in self.config["services"]:
                raise BackupError("unknown old project container must be reviewed/stopped separately")
            if not container.get("State", {}).get("Running"):
                continue
            if own and allow_project:
                continue
            if own and labels.get("com.docker.compose.service") in allow_services:
                continue
            if own:
                raise BackupError("project services are active; stop them before restore")
            for mount in container.get("Mounts", []):
                value = Path(mount.get("Source", "/__unknown__"))
                if mount.get("Name") in names or any(root == value or root in value.parents or value in root.parents for root in roots):
                    raise BackupError("another active container uses snapshot resources")
        if not allow_project:
            assert_no_open_writers(roots)

    def create_missing_volumes(self):
        for resource in self.resources.values():
            if resource.kind == "volume" and resource.root is None:
                self.docker.run(["docker", "volume", "create", "--label", "com.docker.compose.project=" + self.name,
                                 "--label", "com.docker.compose.volume=" + resource.volume_key, resource.source])
                resource.root = self.volume_root(resource)


def assert_no_open_writers(roots: list[Path]) -> None:
    """Also catch ordinary host processes writing these trees outside Compose."""
    proc = Path("/proc")
    if not proc.is_dir():
        raise BackupError("Linux process inventory is required for offline snapshots")
    for process in proc.iterdir():
        if not process.name.isdecimal() or int(process.name) == os.getpid():
            continue
        try:
            descriptors = list((process / "fd").iterdir())
        except FileNotFoundError:
            continue
        except PermissionError:
            raise BackupError("cannot inventory host writers; root access is required") from None
        for descriptor in descriptors:
            try:
                target = Path(os.readlink(descriptor).removesuffix(" (deleted)"))
                if not any(target == root or root in target.parents for root in roots):
                    continue
                flags = re.search(r"^flags:\s+([0-7]+)$", (process / "fdinfo" / descriptor.name).read_text(), re.M)
                if not flags or int(flags[1], 8) & os.O_ACCMODE:
                    raise BackupError("an active host process can write snapshot resources")
            except FileNotFoundError:
                continue


@contextmanager
def operation_lock(name: str):
    import fcntl
    path = Path("/var/lock") / ("frontiercloud-snapshot-" + name + ".lock")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0:
            raise BackupError("unsafe operator lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("another snapshot operation owns this project") from None
        yield
    finally:
        os.close(descriptor)


def archive_tree(root: Path, destination: Path, *, excluded=frozenset()) -> dict:
    """Store original permissions/ownership inside a private regular tar file."""
    absolute(root)
    if not root.is_dir():
        raise BackupError("persistent resource is not a directory")
    initial = root.lstat()
    inventory = {}
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream, tarfile.open(fileobj=stream, mode="w") as archive:
        def add(path: Path, name: str):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
                raise BackupError("persistent trees contain a symbolic link/socket/special file")
            entry = tarfile.TarInfo(name)
            entry.uid, entry.gid, entry.mode, entry.mtime = info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode) & 0o777, info.st_mtime
            if stat.S_ISDIR(info.st_mode):
                entry.type = tarfile.DIRTYPE
                archive.addfile(entry)
                inventory[name] = {"type": "directory", "mode": entry.mode, "uid": entry.uid, "gid": entry.gid}
                for child in sorted(path.iterdir()):
                    child_name = child.relative_to(root).as_posix()
                    if child_name in excluded or any(child_name.startswith(value + "/") for value in excluded):
                        continue
                    relative(child_name)
                    add(child, child_name)
                after = path.lstat()
                if (info.st_dev, info.st_ino, info.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_mtime_ns):
                    raise BackupError("persistent directory changed during snapshot")
            else:
                if info.st_nlink != 1:
                    raise BackupError("hard links are not supported in persistent snapshots")
                size, checksum = digest(path)
                entry.size = size
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as source:
                    opened = os.fstat(source.fileno())
                    if (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns) != (opened.st_dev, opened.st_ino, opened.st_mtime_ns, opened.st_ctime_ns):
                        raise BackupError("persistent file changed before snapshot")
                    archive.addfile(entry, source)
                    after = os.fstat(source.fileno())
                if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise BackupError("persistent file changed during snapshot")
                inventory[name] = {"type": "file", "mode": entry.mode, "uid": entry.uid, "gid": entry.gid, "size": size, "sha256": checksum}
        add(root, ".")
        stream.flush()
        os.fsync(stream.fileno())
    if (initial.st_dev, initial.st_ino) != (root.lstat().st_dev, root.lstat().st_ino):
        raise BackupError("persistent root changed during snapshot")
    validate_archive(destination, inventory)
    return inventory


def validate_archive(path: Path, inventory: dict):
    if not isinstance(inventory, dict) or "." not in inventory or inventory["."]["type"] != "directory":
        raise BackupError("invalid archive inventory")
    seen = set()
    with tarfile.open(path, "r:") as archive:
        for entry in archive:
            if entry.name != ".":
                relative(entry.name)
                parent = PurePosixPath(entry.name).parent.as_posix()
                if parent not in seen:
                    raise BackupError("archive parent directory is missing or unordered")
            if entry.name in seen or not (entry.isdir() or entry.isreg()) or entry.issparse():
                raise BackupError("archive contains duplicate/link/special entries")
            seen.add(entry.name)
            if entry.name not in inventory:
                raise BackupError("archive differs from its inventory")
            item = inventory[entry.name]
            expected = {"type": "directory" if entry.isdir() else "file", "mode": entry.mode, "uid": entry.uid, "gid": entry.gid}
            if entry.mode & ~0o777 or not 0 <= entry.uid < 2 ** 31 or not 0 <= entry.gid < 2 ** 31:
                raise BackupError("unsafe archive permissions or ownership")
            if entry.isreg():
                checksum = hashlib.sha256()
                with archive.extractfile(entry) as data:
                    while chunk := data.read(1024 * 1024):
                        checksum.update(chunk)
                expected |= {"size": entry.size, "sha256": checksum.hexdigest()}
            if item != expected:
                raise BackupError("archive entry integrity check failed")
    if seen != set(inventory):
        raise BackupError("archive inventory is incomplete")


def restore_archive(path: Path, destination: Path, inventory: dict):
    validate_archive(path, inventory)
    absolute(destination, exists=False)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise BackupError("restore destination is not empty")
    # Keep the creation chain: fsyncing only the restored root does not persist
    # the directory entries that link a new bind root into its new ancestors.
    created = []
    ancestor = destination
    while not ancestor.exists():
        created.append(ancestor)
        ancestor = ancestor.parent
    for directory in reversed(created):
        absolute(directory, exists=False)
        directory.mkdir(mode=0o700)
    with tarfile.open(path, "r:") as archive:
        directories = []
        for entry in archive:
            target = destination if entry.name == "." else destination.joinpath(*relative(entry.name).parts)
            absolute(target, exists=False)
            if entry.isdir():
                if target != destination:
                    target.mkdir(mode=0o700)
                directories.append((target, entry))
                continue
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(descriptor, "wb") as restored, archive.extractfile(entry) as source:
                while chunk := source.read(1024 * 1024):
                    restored.write(chunk)
                restored.flush()
                os.fchown(restored.fileno(), entry.uid, entry.gid)
                os.fchmod(restored.fileno(), entry.mode)
                os.utime(restored.fileno(), (entry.mtime, entry.mtime))
                # The final barrier includes contents and all restored metadata.
                os.fsync(restored.fileno())
        for target, entry in reversed(directories):
            os.chown(target, entry.uid, entry.gid, follow_symlinks=False)
            os.chmod(target, entry.mode, follow_symlinks=False)
            os.utime(target, (entry.mtime, entry.mtime), follow_symlinks=False)
            descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in created:  # Deepest parent first, through the existing one.
        fsync_directory(directory.parent)


def backup(plan: Plan, output: Path) -> dict:
    output = absolute(output, exists=False)
    if output.exists() or not output.parent.is_dir():
        raise BackupError("snapshot output must be new with an existing parent directory")
    if any(resource.root == output or resource.root in output.parents or output in resource.root.parents for resource in plan.resources.values() if resource.root):
        raise BackupError("snapshot output overlaps persistent deployment data")
    plan.assert_cold(allow_project=True)
    plan.docker.stop()
    plan.assert_cold()
    output.mkdir(mode=0o700)
    os.chmod(output, 0o700)
    # Persist the new snapshot directory entry before publishing completion.
    # Syncing the child alone does not make its parent's directory entry durable.
    fsync_directory(output.parent)
    manifest = {"version": VERSION, "complete": True, "created_at": int(time.time()), "project": plan.name,
                "database_engine": plan.engine, "images": plan.images, "excluded_data": sorted(EXCLUDED_DATA),
                "resources": {}, "files": {}}
    private_write(output / "compose-resolved.json", json_bytes(plan.config))
    sqlite_excluded = set()
    if plan.engine == "sqlite":
        source = plan.resources["control"].root.joinpath(*plan.sqlite_path.parts)
        absolute(source)
        info = source.lstat()
        manifest["sqlite"] = {"path": plan.sqlite_path.as_posix(), "mode": stat.S_IMODE(info.st_mode) & 0o777, "uid": info.st_uid, "gid": info.st_gid,
                              "facts": sqlite_snapshot(source, output)}
        sqlite_excluded = {plan.sqlite_path.as_posix() + suffix for suffix in ("", "-wal", "-shm", "-journal")}
    for key, resource in sorted(plan.resources.items()):
        plan.assert_cold()
        archive = key + ".tar"
        excluded = EXCLUDED_DATA | sqlite_excluded if key == "control" else frozenset()
        if key == "mysql":
            # The official image links its runtime socket into the datadir. This
            # *one* known ephemeral alias is never followed, archived or restored.
            socket = resource.root / "mysql.sock"
            if socket.is_symlink():
                if os.readlink(socket) != "/var/run/mysqld/mysqld.sock":
                    raise BackupError("unknown MySQL socket symbolic link")
                excluded |= {"mysql.sock"}
            elif socket.exists():
                raise BackupError("unknown persistent MySQL socket entry")
        inventory = archive_tree(resource.root, output / archive, excluded=excluded)
        manifest["resources"][key] = {"kind": resource.kind, "source": resource.source, "archive": archive, "inventory": inventory}
    plan.assert_cold()
    for artifact in sorted(output.iterdir()):
        size, checksum = digest(artifact)
        manifest["files"][artifact.name] = {"bytes": size, "sha256": checksum}
    fsync_directory(output)
    fsync_directory(output.parent)
    try:
        private_write(output / "manifest.json", json_bytes(manifest))
        fsync_directory(output)
    except BaseException:
        (output / "manifest.json").unlink(missing_ok=True)
        fsync_directory(output)
        raise
    return manifest


def verify(snapshot: Path) -> dict:
    snapshot = absolute(snapshot)
    if not snapshot.is_dir() or snapshot.stat().st_mode & 0o077:
        raise BackupError("snapshot directory must be private (0700)")
    try:
        manifest_info = (snapshot / "manifest.json").lstat()
    except FileNotFoundError:
        raise BackupError("incomplete snapshot: completion manifest is missing") from None
    if manifest_info.st_mode & 0o077:
        raise BackupError("snapshot manifest must be private (0600)")
    manifest = bounded_json(snapshot / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("version") != VERSION or manifest.get("complete") is not True or not NAME.fullmatch(manifest.get("project", "")):
        raise BackupError("incomplete or unsupported snapshot")
    resources = manifest.get("resources")
    if not isinstance(resources, dict) or set(resources) - RESOURCE_KEYS or not {"control", "secrets", "redis"}.issubset(resources):
        raise BackupError("unknown or incomplete snapshot resources")
    engine = manifest.get("database_engine")
    if engine not in {"sqlite", "mysql"} or (engine == "mysql" and "mysql" not in resources):
        raise BackupError("unsupported or missing database snapshot")
    if manifest.get("excluded_data") != sorted(EXCLUDED_DATA):
        raise BackupError("unknown media exclusion contract")
    files = manifest.get("files")
    expected_files = {key + ".tar" for key in resources} | {"compose-resolved.json"}
    if engine == "sqlite":
        value = manifest["sqlite"]
        sql_path = relative(value["path"])
        if sql_path.parts[0] in SQLITE_EXCLUDED_ROOTS or not isinstance(value["mode"], int) or value["mode"] & ~0o777 or any(not isinstance(value[key], int) or not 0 <= value[key] < 2 ** 31 for key in ("uid", "gid")):
            raise BackupError("invalid SQLite destination metadata")
        expected_files |= {"sqlite.db", "all-tables.sql", "sqlite-proof.json"}
    if not isinstance(files, dict) or set(files) != expected_files or {entry.name for entry in snapshot.iterdir()} != expected_files | {"manifest.json"}:
        raise BackupError("snapshot file inventory is incomplete or contains unknown files")
    for name, expected in files.items():
        relative(name)
        path = snapshot / name
        if path.lstat().st_mode & 0o077:
            raise BackupError("snapshot artifact is not private (0600)")
        size, checksum = digest(path)
        if expected != {"bytes": size, "sha256": checksum}:
            raise BackupError("snapshot file checksum failed")
    for key, value in resources.items():
        if value.get("archive") != key + ".tar" or value.get("kind") != ("bind" if key == "control" else "volume"):
            raise BackupError("unknown resource mapping")
        validate_archive(snapshot / value["archive"], value["inventory"])
        if key == "control":
            exclusions = set(EXCLUDED_DATA)
            if engine == "sqlite":
                sql_path = relative(manifest["sqlite"]["path"]).as_posix()
                exclusions |= {sql_path + suffix for suffix in ("", "-wal", "-shm", "-journal")}
            if any(name in exclusions or any(name.startswith(value + "/") for value in exclusions) for name in value["inventory"]):
                raise BackupError("control archive unexpectedly includes excluded data")
    if engine == "sqlite":
        with closing(sqlite3.connect((snapshot / "sqlite.db").as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            if sqlite_facts(connection) != manifest["sqlite"]["facts"] or bounded_json(snapshot / "sqlite-proof.json") != manifest["sqlite"]["facts"]:
                raise BackupError("SQLite binary snapshot facts differ")
        with tempfile.TemporaryDirectory(prefix="frontiercloud-sql-verify-") as temporary:
            with closing(sqlite3.connect(Path(temporary) / "proof.db")) as connection:
                sql_restore(connection, snapshot / "all-tables.sql")
                if sqlite_facts(connection) != manifest["sqlite"]["facts"]:
                    raise BackupError("SQLite SQL recovery facts differ")
    return manifest


def restore(plan: Plan, snapshot: Path) -> dict:
    manifest = verify(snapshot)
    if manifest["database_engine"] != plan.engine or set(manifest["resources"]) != set(plan.resources):
        raise BackupError("restore target resource/database topology differs from snapshot")
    for service in ("mysql", "redis"):
        if service in manifest["images"] and plan.images.get(service) != manifest["images"][service]:
            raise BackupError("cold database volumes require the exact recorded image/platform")
    if plan.engine == "sqlite" and manifest["sqlite"]["path"] != plan.sqlite_path.as_posix():
        raise BackupError("restore target SQLITE_PATH differs from snapshot")
    plan.assert_cold()
    for resource in plan.resources.values():
        if resource.root is not None and resource.root.exists() and (not resource.root.is_dir() or any(resource.root.iterdir())):
            raise BackupError("all restore targets must be empty; existing data is never overwritten")
    plan.create_missing_volumes()
    plan.assert_cold()
    for key, resource in sorted(plan.resources.items()):
        plan.assert_cold()
        restore_archive(snapshot / (key + ".tar"), resource.root, manifest["resources"][key]["inventory"])
    if plan.engine == "sqlite":
        value = manifest["sqlite"]
        target = plan.resources["control"].root.joinpath(*relative(value["path"]).parts)
        absolute(target, exists=False)
        if not target.parent.is_dir() or not 0 <= value["uid"] < 2 ** 31 or not 0 <= value["gid"] < 2 ** 31 or value["mode"] & ~0o777:
            raise BackupError("invalid SQLite destination metadata")
        private_write(target, b"")
        with closing(sqlite3.connect(target)) as connection:
            sql_restore(connection, snapshot / "all-tables.sql")
            if sqlite_facts(connection) != value["facts"]:
                raise BackupError("restored SQLite facts differ")
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fchown(descriptor, value["uid"], value["gid"])
            os.fchmod(descriptor, value["mode"])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_directory(target.parent)
    plan.assert_cold()
    return manifest


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--project-directory", required=True, type=Path)
        command.add_argument("--compose-file", action="append", required=True, type=Path)
        command.add_argument("--output" if name == "backup" else "--snapshot", required=True, type=Path)
    commands.add_parser("verify").add_argument("--snapshot", required=True, type=Path)
    options = parser.parse_args(arguments)
    try:
        if options.command == "verify":
            value = verify(options.snapshot)
        else:
            if sys.platform != "linux" or os.geteuid() != 0:
                raise BackupError("run this offline operator tool as root on the local Linux Docker host")
            snapshot_resources = set(verify(options.snapshot)["resources"]) if options.command == "restore" else None
            plan = Plan.load(Docker(options.project_directory, options.compose_file), restoring=options.command == "restore", snapshot_resources=snapshot_resources)
            with operation_lock(plan.name):
                value = backup(plan, options.output) if options.command == "backup" else restore(plan, options.snapshot)
        print(json.dumps({"operation": options.command, "project": value["project"], "database_engine": value["database_engine"],
                          "resources": sorted(value["resources"]), "media_included": False, "services_started": False}))
        return 0
    except (BackupError, OSError, ValueError, sqlite3.Error, tarfile.TarError, KeyError, TypeError) as error:
        # Do not print content from certificates/secrets/SQL or Docker diagnostics.
        message = str(error) if isinstance(error, BackupError) else type(error).__name__
        print("Snapshot operation failed: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
