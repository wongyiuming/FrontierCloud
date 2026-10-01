"""Read the same physical schema assets embedded by the Go runtime."""
import json
import re
from importlib.resources import files


SCHEMA_ROOT = files("migrations")


def schema_statements(backend: str, generation: int) -> list[str]:
    if backend not in {"mysql", "sqlite"}:
        raise ValueError("Unsupported database backend")
    document = json.loads((SCHEMA_ROOT / backend / f"{generation:04d}-schema.json").read_text(encoding="utf-8"))
    if document["generation"] != generation:
        raise RuntimeError("Shared schema generation does not match its asset name")
    return document["statements"]


def schema_tables(backend: str, generation: int) -> set[str]:
    return {
        match.group(1)
        for statement in schema_statements(backend, generation)
        if (match := re.match(r"CREATE TABLE(?: IF NOT EXISTS)?\s+`?(\w+)", statement))
    }
