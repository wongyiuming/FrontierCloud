"""Stage immutable public Python source without touching a running deployment."""
from __future__ import annotations

import hashlib
import gzip
import io
import os
from pathlib import Path
import shutil
import stat
import tarfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from scripts import operator_backup as backup
from scripts import operator_release as release


MAX_FILES = 20000
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024


def _refuse(message):
    release.refuse("source_integrity_failed", message)


def _git_hash(kind, data):
    return hashlib.sha1(kind.encode() + b" " + str(len(data)).encode() + b"\0" + data).hexdigest()


def _path(value):
    try:
        return backup.relative(value)
    except backup.BackupError:
        _refuse("source contains an unsafe relative path")


def inventory(api, tree_sha):
    value = api.get(release.PREFIX + "/git/trees/" + release._sha(tree_sha), recursive=1)
    if not isinstance(value, dict) or value.get("sha") != tree_sha or value.get("truncated") is not False or not isinstance(value.get("tree"), list):
        _refuse("source Git tree is incomplete or does not match the selected commit")
    if len(value["tree"]) > MAX_FILES:
        _refuse("source tree exceeds its entry limit")
    entries, directories, files, size = {}, {"": tree_sha}, {}, 0
    for item in value["tree"]:
        if not isinstance(item, dict):
            _refuse("source tree contains an invalid entry")
        path = _path(item.get("path"))
        name = path.as_posix()
        if len(name.encode()) > 4096 or ".git" in path.parts or name in entries:
            _refuse("source tree contains an unsafe or duplicate path")
        digest = release._sha(item.get("sha"))
        mode, kind = item.get("mode"), item.get("type")
        if (mode, kind) == ("040000", "tree"):
            directories[name] = digest
        elif kind == "blob" and mode in {"100644", "100755"}:
            count = item.get("size")
            if type(count) is not int or not 0 <= count <= MAX_FILE_BYTES:
                _refuse("source file exceeds its byte limit")
            size += count
            files[name] = {"git_sha": digest, "bytes": count, "mode": int(mode, 8) & 0o777}
        else:
            _refuse("source links, submodules and special objects are unsupported")
        entries[name] = {"sha": digest, "mode": mode, "type": kind}
    if not files or size > MAX_SOURCE_BYTES:
        _refuse("source tree is empty or exceeds its byte limit")
    children = {directory: [] for directory in directories}
    for name, item in entries.items():
        parent = str(_path(name).parent)
        parent = "" if parent == "." else parent
        if parent not in directories:
            _refuse("source Git tree omits a parent directory")
        children[parent].append((name.rsplit("/", 1)[-1], item))
    for directory in sorted(directories, key=lambda name: (name.count("/"), len(name)), reverse=True):
        rows = sorted(children[directory], key=lambda row: row[0].encode() + (b"/" if row[1]["type"] == "tree" else b""))
        body = b"".join(("40000" if item["type"] == "tree" else item["mode"]).encode() + b" " + name.encode() + b"\0" + bytes.fromhex(item["sha"])
                        for name, item in rows)
        if _git_hash("tree", body) != directories[directory]:
            _refuse("source inventory does not reproduce the selected Git tree")
    return files, frozenset(directories) - {""}, size


def download(revision, destination, *, opener=None):
    """Follow only the official pinned-repository archive redirect, without auth."""
    release._sha(revision)
    opener = opener if opener is not None else build_opener(release.NoRedirect())
    request = Request(release.ORIGIN + release.PREFIX + "/tarball/" + revision,
                      headers={"Accept": "application/vnd.github+json", "User-Agent": "FrontierCloud-Python-Operator/1"})
    try:
        try:
            response = opener.open(request, timeout=15)
        except HTTPError as error:
            code, location, headers = error.code, error.headers.get("Location", ""), error.headers
            error.close()
            if code == 429 or code == 403 and (headers.get("X-RateLimit-Remaining") == "0" or headers.get("Retry-After")):
                release.refuse("source_rate_limited", "GitHub rate limiting prevented source download")
            if code not in {302, 307}:
                release.refuse("source_archive_unavailable", "GitHub did not provide the selected source archive (HTTP " + str(code) + ")")
        else:
            response.close()
            release.refuse("source_archive_redirect_invalid", "GitHub did not return its expected source archive redirect")
        parsed = urlsplit(location)
        if parsed.scheme != "https" or parsed.hostname != "codeload.github.com" or parsed.port not in {None, 443} or parsed.username or parsed.password or parsed.query or parsed.fragment or (
                parsed.path != "/" + release.REPOSITORY + "/legacy.tar.gz/" + revision):
            release.refuse("source_archive_redirect_invalid", "refusing an unexpected source archive destination")
        with opener.open(Request(location, headers={"User-Agent": "FrontierCloud-Python-Operator/1"}), timeout=30) as response:
            descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        release.refuse("source_archive_limit", "source archive exceeds its compressed byte limit")
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
    except (URLError, OSError) as error:
        # Same certificate-verifying urllib stack as provenance. No exception
        # text or redirect URL is emitted; either can contain service details.
        import ssl
        reason = error.reason if isinstance(error, URLError) else error
        code = "source_tls_failed" if isinstance(reason, ssl.SSLError) else "source_transport_timeout" if isinstance(reason, TimeoutError) else "source_transport_failed"
        release.refuse(code, "the selected public source archive could not be downloaded")
    except ValueError:
        release.refuse("source_archive_redirect_invalid", "GitHub returned an invalid source archive destination")


def _directory(root, relative):
    current = root
    for part in relative.parts:
        current /= part
        try:
            current.mkdir(mode=0o755)
            os.chmod(current, 0o755)
        except FileExistsError:
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                _refuse("source extraction encountered a replaced directory")
    return current


class ExpandedLimit(io.RawIOBase):
    def __init__(self, source, limit):
        self.source, self.remaining = source, limit

    def read(self, size=-1):
        # Tar's stream parser must never request an unbounded gzip expansion.
        if size < 0:
            size = min(1024 * 1024, self.remaining + 1)
        value = self.source.read(min(size, self.remaining + 1))
        self.remaining -= len(value)
        if self.remaining < 0:
            _refuse("source archive exceeds its expanded byte limit")
        return value


def _tar_info():
    class BoundedInfo(tarfile.TarInfo):
        count = 0

        @classmethod
        def checked(cls, value):
            cls.count += 1
            if cls.count > MAX_FILES * 2 + 1024:
                _refuse("source archive exceeds its header limit")
            if value.type in {tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK} and value.size > 64 * 1024:
                _refuse("source archive metadata exceeds its byte limit")
            if value.size < 0 or value.size > MAX_FILE_BYTES:
                _refuse("source archive header exceeds its byte limit")
            return value

        @classmethod
        def frombuf(cls, buffer, encoding, errors):
            return cls.checked(super().frombuf(buffer, encoding, errors))

        @classmethod
        def _frombuf(cls, buffer, encoding, errors, **options):
            # Python 3.14's hardened parser uses this hook directly, including
            # PAX successors. Guard it before _proc_member can allocate metadata.
            return cls.checked(super()._frombuf(buffer, encoding, errors, **options))
    return BoundedInfo


def extract(archive, destination, files, directories, revision):
    root, root_seen, seen, receipts = None, False, set(), {}
    try:
        expanded_limit = sum(row["bytes"] for row in files.values()) + (len(files) + len(directories) + 1) * 2048 + 2 * 1024 * 1024
        with gzip.open(archive, "rb") as compressed, tarfile.open(fileobj=ExpandedLimit(compressed, expanded_limit), mode="r|", tarinfo=_tar_info()) as source:
            for member in source:
                path = _path(member.name.rstrip("/") if member.isdir() else member.name)
                first = path.parts[0]
                if root is None:
                    prefix = release.REPOSITORY.replace("/", "-") + "-"
                    suffix = first.removeprefix(prefix)
                    if not first.startswith(prefix) or not 7 <= len(suffix) <= 40 or not revision.startswith(suffix):
                        _refuse("source archive root does not match the selected repository revision")
                    root = first
                if first != root:
                    _refuse("source archive contains multiple roots or duplicate members")
                if len(path.parts) == 1:
                    if not member.isdir() or member.size or root_seen:
                        _refuse("source archive root must be a directory")
                    root_seen = True
                    continue
                relative = Path(*path.parts[1:])
                name = relative.as_posix()
                if name in seen:
                    _refuse("source archive contains duplicate paths")
                seen.add(name)
                if member.isdir():
                    if name not in directories or member.size:
                        _refuse("source archive contains an untracked directory")
                    _directory(destination, relative)
                    continue
                expected = files.get(name)
                if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE} or member.sparse is not None or expected is None or member.size != expected["bytes"] or member.mode & ~0o777:
                    _refuse("source archive contains unknown, linked, special or incorrectly sized files")
                parent = _directory(destination, relative.parent)
                descriptor = os.open(parent / relative.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, expected["mode"])
                git_hash = hashlib.sha1(b"blob " + str(member.size).encode() + b"\0")
                content_hash, count = hashlib.sha256(), 0
                with os.fdopen(descriptor, "wb") as target, source.extractfile(member) as contents:
                    while chunk := contents.read(1024 * 1024):
                        count += len(chunk)
                        if count > expected["bytes"]:
                            _refuse("source archive file exceeded its admitted size")
                        target.write(chunk)
                        git_hash.update(chunk)
                        content_hash.update(chunk)
                    if count != expected["bytes"] or git_hash.hexdigest() != expected["git_sha"]:
                        _refuse("source archive bytes do not match the selected Git tree")
                    os.fchmod(target.fileno(), expected["mode"])
                    target.flush()
                    os.fsync(target.fileno())
                receipts[name] = expected | {"sha256": content_hash.hexdigest()}
            # Force gzip CRC/truncation validation and reject a second hidden
            # archive after tar's first end marker. Ordinary zero padding stays
            # bounded by ExpandedLimit and is harmless.
            while tail := source.fileobj.read(1024 * 1024):
                if tail.strip(b"\0"):
                    _refuse("source archive contains trailing non-padding data")
    except (tarfile.TarError, EOFError, gzip.BadGzipFile):
        _refuse("source archive is corrupt or incomplete")
    if set(receipts) != set(files):
        _refuse("source archive is missing tracked files")
    for directory in sorted([destination, *[destination / name for name in directories]], key=lambda path: len(path.parts), reverse=True):
        backup.fsync_directory(directory)
    return receipts


def stage(destination, *, api=None, fetch=download, _selected=None):
    """No CLI candidate/proof injection: production callers obtain public proof."""
    api = api if api is not None else release.GitHub()
    # A host bootstrap may already have selected this proof for its no-op check.
    # The public recheck below is still mandatory before a completed receipt.
    proof = release.select_latest(api) if _selected is None else _selected
    files, directories, total = inventory(api, proof["tree_sha"])
    destination = backup.absolute(destination, exists=False)
    if destination.exists() or not destination.parent.is_dir():
        release.refuse("source_destination_exists", "source staging requires a new directory with an existing parent")
    if shutil.disk_usage(destination.parent).free < total + MAX_ARCHIVE_BYTES + 16 * 1024 * 1024:
        release.refuse("source_space_insufficient", "source staging has insufficient free space")
    destination.mkdir(mode=0o700)
    os.chmod(destination, 0o700)
    backup.fsync_directory(destination.parent)
    source = destination / "source"
    source.mkdir(mode=0o755)
    os.chmod(source, 0o755)
    archive = destination / "source.tar.gz"
    fetch(proof["revision"], archive)
    receipts = extract(archive, source, files, directories, proof["revision"])
    # A finished staging receipt never outlives a moved main or invalidated CI.
    if release.select_latest(api) != proof:
        release.refuse("release_proof_changed", "public release proof changed while source was staged")
    size, digest = backup.digest(archive)
    receipt = {"version": 1, "phase": "source_staged", "provenance": proof, "files": receipts, "directories": sorted(directories),
               "archive": {"bytes": size, "sha256": digest}, "deployment_changed": False}
    receipt["stage_sha256"] = hashlib.sha256(backup.json_bytes(receipt)).hexdigest()
    backup.private_write(destination / "source-receipt.json", backup.json_bytes(receipt))
    backup.fsync_directory(destination)
    backup.fsync_directory(destination.parent)
    return receipt


def verify_stage(directory):
    """Local tamper check only; does not assert that public CI is still current."""
    directory = backup.absolute(directory)
    if directory.stat().st_mode & 0o077:
        _refuse("source staging directory is not private")
    receipt = backup.bounded_json(directory / "source-receipt.json")
    if not isinstance(receipt, dict):
        _refuse("source staging receipt is invalid")
    digest = receipt.get("stage_sha256")
    if digest != hashlib.sha256(backup.json_bytes({key: value for key, value in receipt.items() if key != "stage_sha256"})).hexdigest():
        _refuse("source staging receipt checksum failed")
    if receipt.get("version") != 1 or receipt.get("phase") != "source_staged" or not isinstance(receipt.get("files"), dict):
        _refuse("source staging is incomplete")
    if not 0 < len(receipt["files"]) <= MAX_FILES or {path.name for path in directory.iterdir()} != {"source", "source.tar.gz", "source-receipt.json"}:
        _refuse("source staging has an incomplete or unexpected inventory")
    source = backup.absolute(directory / "source")
    found, directories = set(), set(receipt.get("directories", []))
    for name in directories:
        path = _path(name)
        target = backup.absolute(source.joinpath(*path.parts))
        if not target.is_dir() or stat.S_IMODE(target.stat().st_mode) != 0o755:
            _refuse("staged source directory permissions changed after verification")
    if stat.S_IMODE(source.stat().st_mode) != 0o755:
        _refuse("staged source root permissions changed after verification")
    for name, row in receipt["files"].items():
        path = _path(name)
        target = backup.absolute(source.joinpath(*path.parts))
        size, digest = backup.digest(target)
        if (size, digest, stat.S_IMODE(target.stat().st_mode)) != (row["bytes"], row["sha256"], row["mode"]):
            _refuse("staged source was modified after verification")
        found.add(name)
        directories.update(parent.as_posix() for parent in path.parents if parent.as_posix() != ".")
    for root, names, files in os.walk(source, followlinks=False):
        parent = Path(root).relative_to(source)
        for name in names + files:
            path = parent / name
            if path.as_posix() not in found | directories or (source / path).is_symlink():
                _refuse("staged source contains an untracked file or link")
    size, digest = backup.digest(directory / "source.tar.gz")
    if receipt["archive"] != {"bytes": size, "sha256": digest}:
        _refuse("staged source archive changed after verification")
    return receipt
