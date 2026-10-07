"""Fast schema/protocol asset checks, not a substitute for real-driver acceptance."""
import json
from pathlib import Path
import sqlite3

from check_ci_budget import check_workflows


root = Path(__file__).resolve().parents[1]
issues = check_workflows(root / ".github/workflows")
if issues:
    raise SystemExit("\n".join(issues))
for path in (root / "protocol").rglob("*.json"):
    json.loads(path.read_text(encoding="utf-8"))
documents = [json.loads((root / "migrations" / kind / "0002-schema.json").read_text(encoding="utf-8"))
             for kind in ("sqlite", "mysql")]
assert all(document["generation"] == 2 for document in documents)
with sqlite3.connect(":memory:") as database:
    for statement in documents[0]["statements"]:
        database.execute(statement)
    assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert database.execute("SELECT COUNT(*) FROM sqlite_schema WHERE type='table'").fetchone()[0] == 34
print("Protocol JSON and canonical SQLite schema assets passed; full acceptance remains external")
