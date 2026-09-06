"""Publish an atomic, non-executable Nginx ban snapshot; MySQL owns the state."""
from datetime import datetime, timezone
import ipaddress
import os
from pathlib import Path
import tempfile

from sqlalchemy import text

from app.core.client_ip import is_security_exempt, normalize_ip
from app.core.db import engine

SNAPSHOT_PATH = Path("data/.ip-security/active-bans.tsv")


def write_snapshot(rows, whitelist, path: Path = SNAPSHOT_PATH) -> None:
    """Store only validated addresses and numeric expiry, never Nginx syntax."""
    allowed = {normalize_ip(ip) for ip in whitelist}
    bans = {}
    for row in rows:
        ip = normalize_ip(row["ip_address"])
        if ip in allowed or is_security_exempt(ip):
            continue
        expiry = (0 if row["ban_kind"] == "permanent" else
                  int(row["expires_at"].replace(tzinfo=timezone.utc).timestamp()))
        bans[ip] = expiry
    addresses = sorted(bans, key=lambda ip: (ipaddress.ip_address(ip).version,
                                            int(ipaddress.ip_address(ip))))
    content = "# frontiercloud-ip-security-v1\n" + "".join(
        f"{ip}\t{bans[ip]}\n" for ip in addresses
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.read_text(encoding="ascii") == content:
            return
    except FileNotFoundError:
        pass
    descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def publish_edge_snapshot() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with engine.connect() as conn:
        whitelist = (await conn.execute(text(
            "SELECT ip_address FROM ip_permanent_whitelist"
        ))).scalars().all()
        rows = (await conn.execute(text("""
            SELECT ip_address, expires_at, ban_kind FROM ip_auto_ban_events
            WHERE status='active' AND expires_at > :now
        """), {"now": now})).mappings().all()
    # This short atomic replacement deliberately has no cancellation point.
    write_snapshot(rows, whitelist)
