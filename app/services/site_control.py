"""Admin-controlled site availability layered on top of updater maintenance."""
from __future__ import annotations

from pathlib import Path

from app.services import release_control

DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
MANUAL_MAINTENANCE = DATA_ROOT / ".frontiercloud-maintenance"
FORCE_OPEN = DATA_ROOT / ".frontiercloud-force-open"
BUSY_STATES = {"queued", "running", "distributing"}


def _touch(path: Path, value: str) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value.rstrip() + "\n", encoding="utf-8")
    temporary.replace(path)


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


async def status() -> dict:
    release = await release_control.agent_status()
    release_state = str(release.get("state") or "unknown")
    release_phase = str(release.get("phase") or "")
    release_maintenance = release_state in BUSY_STATES or release_state == "failed"
    manual = MANUAL_MAINTENANCE.exists()
    forced_open = FORCE_OPEN.exists()
    maintenance = False if forced_open else (manual or release_maintenance)

    if forced_open:
        source = "manual-open-override"
        detail = "管理员已显式结束维护；发布失败遗留维护门禁被覆盖"
    elif manual:
        source = "manual"
        detail = "管理员手动进入维护"
    elif release_maintenance:
        source = "release"
        target = str(release.get("target_sha") or "")
        detail = f"版本发布 {release_state} / {release_phase or '-'}"
        if target:
            detail += f" · {target[:12]}"
    else:
        source = "open"
        detail = "站点正常开放"

    return {
        "maintenance": maintenance,
        "source": source,
        "detail": detail,
        "manual": manual,
        "force_open": forced_open,
        "release": release,
    }


async def set_maintenance(enabled: bool) -> dict:
    release = await release_control.agent_status()
    release_state = str(release.get("state") or "unknown")
    if enabled:
        _unlink(FORCE_OPEN)
        _touch(MANUAL_MAINTENANCE, "manual")
    else:
        if release_state in BUSY_STATES:
            raise RuntimeError("版本发布正在执行，禁止在容器替换或分发过程中结束维护")
        _unlink(MANUAL_MAINTENANCE)
        if release_state == "failed":
            _touch(FORCE_OPEN, "release-failure-override")
        else:
            _unlink(FORCE_OPEN)
    return await status()


def prepare_release() -> None:
    """Compatibility hook; every updater clears its own open override atomically."""
    return None
