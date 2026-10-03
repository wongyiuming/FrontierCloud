"""Bounded runtime-neutral manifest parsing; never standalone release authority."""
from __future__ import annotations

import copy
import hashlib
import json
import re

from app.services.federation import protocol as p

MAX_MANIFEST_BYTES = 8192
MANIFEST_CAPABILITY = "release-manifest-v1"
_sha = re.compile(r"^[a-f0-9]{40}$")
_version = re.compile(r"^[0-9][a-zA-Z0-9._-]{0,63}$")
_policies = {"main": "dev", "gin_main": "gin_dev"}


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise p.ProtocolError("Duplicate release manifest field")
        result[key] = value
    return result


def parse(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or len(raw) > MAX_MANIFEST_BYTES:
        raise p.ProtocolError("Invalid bounded release manifest")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (not isinstance(value, dict) or set(value) != {
            "format", "version", "release_version", "protocol", "schema_generation", "artifacts",
        } or value["format"] != "frontiercloud-release-manifest"
                or type(value["version"]) is not int or value["version"] != 1
                or type(value["protocol"]) is not int or value["protocol"] != p.PROTOCOL_VERSION
                or type(value["schema_generation"]) is not int or value["schema_generation"] != 2
                or not isinstance(value["release_version"], str)
                or not _version.fullmatch(value["release_version"])):
            raise ValueError()
        artifacts = value["artifacts"]
        if not isinstance(artifacts, dict) or set(artifacts) != set(_policies):
            raise ValueError()
        for artifact in artifacts.values():
            if (not isinstance(artifact, dict) or set(artifact) != {
                "kind", "commit_sha", "source_sha", "tree_sha",
            } or artifact["kind"] != "git-archive" or any(
                not isinstance(artifact[key], str) or not _sha.fullmatch(artifact[key])
                for key in ("commit_sha", "source_sha", "tree_sha")
            )):
                raise ValueError()
        # An int-typed protocol field must not accept JSON 2.0 or true.
        return value
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError) as exc:
        raise p.ProtocolError("Invalid or incompatible release manifest") from exc


def identifier(value: dict) -> str:
    checked = parse(p.canonical(value))
    return hashlib.sha256(p.canonical(checked)).hexdigest()


def select(value: dict, branch: str, source: str) -> dict:
    checked = parse(p.canonical(value))
    if _policies.get(branch) != source:
        raise p.ProtocolError("Invalid local release policy")
    return copy.deepcopy(checked["artifacts"][branch])


def check_evidence(value: dict, branch: str, source: str, evidence: dict) -> None:
    artifact = select(value, branch, source)
    if (evidence.get("publishable") is not True or evidence.get("available") is not True
            or evidence.get("branch") != branch or evidence.get("source_branch") != source
            or evidence.get("sha") != artifact["commit_sha"]
            or evidence.get("ci_sha") != artifact["source_sha"]
            or evidence.get("tree_sha") != artifact["tree_sha"]
            or evidence.get("status") != "completed" or evidence.get("conclusion") != "success"):
        raise p.ProtocolError("Release artifact lacks exact reviewed CI evidence")
