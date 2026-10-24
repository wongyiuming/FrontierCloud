"""Real Git tree reproduction and bounded source staging without deployment I/O."""
import copy
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import operator_release as release
from scripts import operator_source as source
from tests.test_operator_release import MAIN, PublicFixture


@unittest.skipUnless(sys.platform == "linux", "source staging uses Linux host filesystem semantics")
class SourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.git = self.base / "git-objects"
        self.git.mkdir()
        self.command("git", "init", "--bare", "--quiet")
        self.data = {"main.py": b"print('fixture')\n", "app/start.py": b"# exact source\n", "scripts/start.sh": b"#!/bin/sh\nexit 0\n"}
        self.mode = {name: "100755" if name.endswith(".sh") else "100644" for name in self.data}
        blobs = {name: self.command("git", "hash-object", "-w", "--stdin", data=value) for name, value in self.data.items()}
        app_tree = self.command("git", "mktree", data=("100644 blob " + blobs["app/start.py"] + "\tstart.py\n").encode())
        script_tree = self.command("git", "mktree", data=("100755 blob " + blobs["scripts/start.sh"] + "\tstart.sh\n").encode())
        self.tree = self.command("git", "mktree", data=("040000 tree " + app_tree + "\tapp\n100644 blob " + blobs["main.py"] + "\tmain.py\n040000 tree " + script_tree + "\tscripts\n").encode())
        self.entries = []
        for row in self.command_bytes("git", "ls-tree", "-r", "-t", "--long", "-z", self.tree).split(b"\0"):
            if not row:
                continue
            meta, name = row.split(b"\t")
            mode, kind, digest, size = meta.split()
            item = {"mode": mode.decode(), "type": kind.decode(), "sha": digest.decode(), "path": name.decode()}
            if kind == b"blob":
                item["size"] = int(size)
            self.entries.append(item)
        self.tree_response = {"sha": self.tree, "truncated": False, "tree": self.entries}
        self.api = PublicFixture()
        for commit in self.api.commits.values():
            commit["commit"]["tree"]["sha"] = self.tree
        original = self.api.get
        def get(path, **query):
            if "/git/trees/" in path:
                return copy.deepcopy(self.tree_response)
            return original(path, **query)
        self.api.get = get
        self.root = release.REPOSITORY.replace("/", "-") + "-" + MAIN[:7]

    def command_bytes(self, *args, data=None):
        result = subprocess.run(args, input=data, cwd=self.git, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def command(self, *args, data=None):
        return self.command_bytes(*args, data=data).decode().strip()

    def archive(self, *, omit=None, change=None, extra=None, raw_tail=b""):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
            root = tarfile.TarInfo(self.root)
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            for directory in ("app", "scripts"):
                info = tarfile.TarInfo(self.root + "/" + directory)
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            for name, data in self.data.items():
                if name == omit:
                    continue
                data = change.get(name, data) if change else data
                info = tarfile.TarInfo(self.root + "/" + name)
                info.size, info.mode = len(data), int(self.mode[name], 8) & 0o777
                archive.addfile(info, io.BytesIO(data))
            if extra:
                extra(archive)
        return gzip.compress(raw.getvalue() + raw_tail)

    def extract(self, data):
        archive = self.base / "candidate.tar.gz"
        archive.write_bytes(data)
        target = self.base / "source"
        target.mkdir()
        files, directories, _ = source.inventory(self.api, self.tree)
        return source.extract(archive, target, files, directories, MAIN)

    def stage(self, data=None):
        archive = data if data is not None else self.archive()
        def fetch(revision, target):
            self.assertEqual(revision, MAIN)
            target.write_bytes(archive)
            os.chmod(target, 0o600)
        target = self.base / "staged"
        return target, source.stage(target, api=self.api, fetch=fetch)

    def test_inventory_reproduces_actual_git_mktree_and_preserves_file_modes(self):
        files, directories, total = source.inventory(self.api, self.tree)
        self.assertEqual(set(files), set(self.data))
        self.assertEqual(directories, {"app", "scripts"})
        self.assertEqual(total, sum(map(len, self.data.values())))
        self.assertEqual(files["scripts/start.sh"]["mode"], 0o755)
        self.assertEqual(self.command("git", "for-each-ref"), "")

    def test_tree_hash_not_merely_a_list_of_claimed_blob_hashes(self):
        self.tree_response["tree"][0]["sha"] = "4" * 40
        with self.assertRaisesRegex(release.ReleaseError, "reproduce"):
            source.inventory(self.api, self.tree)

    def test_truncated_tree_unknown_object_and_unsafe_path_refuse(self):
        original = copy.deepcopy(self.tree_response)
        for mutate in (lambda value: value.update(truncated=True),
                       lambda value: value["tree"][0].update(type="commit", mode="160000"),
                       lambda value: value["tree"][0].update(path="../outside"),
                       lambda value: value["tree"].append(copy.deepcopy(value["tree"][0]))):
            with self.subTest(mutate=mutate):
                self.tree_response = copy.deepcopy(original)
                mutate(self.tree_response)
                with self.assertRaises(release.ReleaseError):
                    source.inventory(self.api, self.tree)

    def test_source_size_limit_refuses_before_download(self):
        self.tree_response["tree"][-1]["size"] = source.MAX_FILE_BYTES + 1
        with self.assertRaisesRegex(release.ReleaseError, "byte limit"):
            source.inventory(self.api, self.tree)

    def test_complete_stage_and_local_tamper_check_leave_exact_source(self):
        target, receipt = self.stage()
        self.assertEqual(receipt, source.verify_stage(target))
        self.assertEqual(set(receipt["files"]), set(self.data))
        for name, data in self.data.items():
            self.assertEqual((target / "source" / name).read_bytes(), data)
        self.assertEqual((target.stat().st_mode & 0o777, (target / "source-receipt.json").stat().st_mode & 0o777), (0o700, 0o600))
        self.assertEqual((target / "source/scripts/start.sh").stat().st_mode & 0o777, 0o755)
        self.assertFalse(receipt["deployment_changed"])

    def test_bootstrap_selected_proof_still_requires_actual_public_revalidation(self):
        selected=source.release.select_latest(self.api)
        selected['source_sha']='f'*40
        def fetch(_revision,target):
            target.write_bytes(self.archive());target.chmod(0o600)
        target=self.base/'selected-changed'
        # Archive/tree validation succeeds; the mandatory final public check
        # still refuses a caller's different selected promotion.
        with self.assertRaises(source.release.ReleaseError):source.stage(target,api=self.api,fetch=fetch,_selected=selected)
        self.assertFalse((target/'source-receipt.json').exists())

    def test_changed_source_and_untracked_material_refuse(self):
        target, _ = self.stage()
        (target / "source/main.py").write_bytes(b"changed")
        with self.assertRaisesRegex(release.ReleaseError, "modified"):
            source.verify_stage(target)
        (target / "source/main.py").write_bytes(self.data["main.py"])
        (target / "source/untracked").mkdir()
        with self.assertRaisesRegex(release.ReleaseError, "untracked"):
            source.verify_stage(target)

    def test_private_host_umask_does_not_make_container_source_unreadable(self):
        old = os.umask(0o077)
        try:
            target, _ = self.stage()
        finally:
            os.umask(old)
        for directory in ("source", "source/app", "source/scripts"):
            self.assertEqual((target / directory).stat().st_mode & 0o777, 0o755)
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        source.verify_stage(target)

    def test_missing_or_same_size_changed_archive_file_refuse(self):
        for data in (self.archive(omit="main.py"), self.archive(change={"main.py": b"X" * len(self.data["main.py"])})):
            with self.subTest():
                target = self.base / "source"
                if target.exists():
                    # Cleanup only this known disposable test directory.
                    import shutil
                    shutil.rmtree(target)
                with self.assertRaises(release.ReleaseError):
                    self.extract(data)

    def test_symlink_and_parent_traversal_are_not_materialized(self):
        def malicious(archive):
            item = tarfile.TarInfo(self.root + "/../outside")
            item.type, item.linkname = tarfile.SYMTYPE, "/etc/passwd"
            archive.addfile(item)
        with self.assertRaises(release.ReleaseError):
            self.extract(self.archive(extra=malicious))
        self.assertFalse((self.base / "outside").exists())

    def test_duplicate_regular_file_refuses_and_preserves_first_bytes(self):
        def duplicate(archive):
            item = tarfile.TarInfo(self.root + "/main.py")
            item.size = 3
            archive.addfile(item, io.BytesIO(b"bad"))
        with self.assertRaisesRegex(release.ReleaseError, "duplicate"):
            self.extract(self.archive(extra=duplicate))
        self.assertEqual((self.base / "source/main.py").read_bytes(), self.data["main.py"])

    def test_oversized_pax_metadata_is_rejected_before_parsing(self):
        item = tarfile.TarInfo("oversized-pax")
        item.type, item.size = tarfile.XHDTYPE, 65537
        raw = item.tobuf() + b"X" * 65537
        with self.assertRaisesRegex(release.ReleaseError, "metadata"):
            self.extract(gzip.compress(raw))

    def test_trailing_archive_and_expansion_bomb_refuse(self):
        for tail in (b"hidden-second-archive", b"\0" * (3 * 1024 * 1024)):
            with self.subTest(size=len(tail)):
                target = self.base / "source"
                if target.exists():
                    import shutil
                    shutil.rmtree(target)
                with self.assertRaises(release.ReleaseError):
                    self.extract(self.archive(raw_tail=tail))

    def test_download_failure_leaves_no_completion_receipt_or_deleted_partial_state(self):
        target = self.base / "failed"
        def fail(revision, archive):
            archive.write_bytes(b"partial")
            raise release.ReleaseError("source_transport_failed", "fixture")
        with self.assertRaises(release.ReleaseError):
            source.stage(target, api=self.api, fetch=fail)
        self.assertEqual((target / "source.tar.gz").read_bytes(), b"partial")
        self.assertFalse((target / "source-receipt.json").exists())

    def test_existing_staging_destination_is_never_overwritten(self):
        target = self.base / "staged"
        target.mkdir()
        marker = target / "retain"
        marker.write_bytes(b"existing")
        with self.assertRaises(release.ReleaseError) as caught:
            self.stage()
        self.assertEqual(caught.exception.code, "source_destination_exists")
        self.assertEqual(marker.read_bytes(), b"existing")

    def test_free_space_failure_is_before_staging_or_download(self):
        from collections import namedtuple
        Usage = namedtuple("Usage", "total used free")
        with patch.object(source.shutil, "disk_usage", return_value=Usage(1, 1, 0)):
            with self.assertRaises(release.ReleaseError) as caught:
                self.stage()
        self.assertEqual(caught.exception.code, "source_space_insufficient")
        self.assertFalse((self.base / "staged").exists())

    def test_changed_public_proof_during_staging_cannot_publish_completion(self):
        target = self.base / "changed-proof"
        first = release.select_latest(self.api)
        second = copy.deepcopy(first)
        second["revision"] = "4" * 40
        def fetch(revision, archive):
            archive.write_bytes(self.archive())
        with patch.object(release, "select_latest", side_effect=[first, second]):
            with self.assertRaises(release.ReleaseError) as caught:
                source.stage(target, api=self.api, fetch=fetch)
        self.assertEqual(caught.exception.code, "release_proof_changed")
        self.assertFalse((target / "source-receipt.json").exists())
        self.assertEqual((target / "source/main.py").read_bytes(), self.data["main.py"])

    def test_redirect_download_is_pinned_and_does_not_send_credentials(self):
        owner = self
        class Opener:
            def __init__(self, destination):
                self.requests, self.destination = [], destination
            def open(self, request, timeout):
                self.requests.append(request)
                if len(self.requests) == 1:
                    raise HTTPError(request.full_url, 302, "", {"Location": self.destination}, io.BytesIO())
                return io.BytesIO(owner.archive())
        location = "https://codeload.github.com/" + release.REPOSITORY + "/legacy.tar.gz/" + MAIN
        opener = Opener(location)
        target = self.base / "download.tar.gz"
        source.download(MAIN, target, opener=opener)
        self.assertTrue(target.stat().st_size > 0)
        self.assertTrue(all(request.get_header("Authorization") is None for request in opener.requests))
        bad = Opener(location.replace("codeload.github.com", "foreign.invalid"))
        with self.assertRaises(release.ReleaseError) as caught:
            source.download(MAIN, self.base / "bad.tar.gz", opener=bad)
        self.assertEqual(caught.exception.code, "source_archive_redirect_invalid")
        self.assertFalse((self.base / "bad.tar.gz").exists())
