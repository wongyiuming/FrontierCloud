"""Manual availability cannot override an operator's hard maintenance fence."""
from __future__ import annotations

import os
from pathlib import Path

DATA_ROOT = Path(__file__).resolve().parents[2] / 'data'
MANUAL_MAINTENANCE = DATA_ROOT / '.frontiercloud-maintenance'
NATIVE_MAINTENANCE = DATA_ROOT / '.frontiercloud-native-maintenance'


def hard_fence_active() -> bool:
    return NATIVE_MAINTENANCE.exists()


def _touch(path: Path, value: str) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as out:
        out.write(value.rstrip() + '\n')
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)
    descriptor = os.open(DATA_ROOT, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


async def status() -> dict:
    manual = MANUAL_MAINTENANCE.exists()
    hard = hard_fence_active()
    return {'maintenance': manual or hard, 'source': 'operator' if hard else 'manual' if manual else 'open',
            'detail': '操作者维护围栏生效；控制台不能解除' if hard else '管理员手动进入维护' if manual else '站点正常开放',
            'manual': manual, 'operator_fence': hard, 'force_open': False}


async def set_maintenance(enabled: bool) -> dict:
    if not enabled and hard_fence_active():
        raise RuntimeError('操作者正在执行维护，控制台不能解除围栏')
    if enabled:
        _touch(MANUAL_MAINTENANCE, 'manual')
    else:
        MANUAL_MAINTENANCE.unlink(missing_ok=True)
    return await status()
