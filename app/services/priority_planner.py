"""Bounded integer drag planning with the public catalog's canonical ties."""
from __future__ import annotations

import hashlib
import json

MINIMUM = -7
MAXIMUM = 500_000


class PriorityMoveError(ValueError):
    pass


class PriorityRangeError(ValueError):
    pass


def tie(entry: dict, kind: str) -> tuple:
    path = entry["path"]
    return ((path.casefold(), path, entry.get("media_id", "")) if kind == "directory"
            else (path, entry.get("media_id", "")))


def ordered(items: list[dict], kind: str) -> list[dict]:
    return sorted((dict(v) for v in items), key=lambda v: (-v["preference"], tie(v, kind)))


def revision(scope: str, kind: str, items: list[dict]) -> str:
    value = {"scope": scope, "kind": kind, "items": ordered(items, kind)}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                                     sort_keys=True).encode()).hexdigest()


def plan(items: list[dict], at: int, kind: str, *, minimum=MINIMUM, maximum=MAXIMUM) -> list[dict]:
    """Change a contiguous collision window; leave all outside values untouched."""
    if kind not in {"media", "directory"} or not 0 <= at < len(items) or minimum > maximum:
        raise PriorityMoveError("Invalid priority scope or insertion")
    result = [dict(v) for v in items]
    if any(isinstance(v["preference"], bool) or not isinstance(v["preference"], int)
           or not minimum <= v["preference"] <= maximum for v in result):
        raise PriorityRangeError("Existing preference is outside the supported range")
    if len({v["path"] for v in result}) != len(result):
        raise PriorityMoveError("Duplicate scope identity")
    gaps = [0] * len(result)
    for index in range(1, len(result)):
        gaps[index] = gaps[index - 1] + int(tie(result[index - 1], kind) >= tie(result[index], kind))
    left = right = at

    def limits(start, end):
        upper = maximum if start == 0 else result[start - 1]["preference"] - gaps[start] + gaps[start - 1]
        lower = minimum if end == len(result) - 1 else result[end + 1]["preference"] + gaps[end + 1] - gaps[end]
        return upper, lower

    while True:
        upper, lower = limits(left, right)
        if upper - lower >= gaps[right] - gaps[left]:
            for index in range(left, right + 1):
                floor = lower + gaps[right] - gaps[index]
                result[index]["preference"] = max(floor, min(upper, result[index]["preference"]))
                if index < right:
                    upper = result[index]["preference"] - gaps[index + 1] + gaps[index]
            return result
        if left == 0 and right == len(result) - 1:
            raise PriorityRangeError("The bounded integer range cannot represent this order")
        left_gain = right_gain = -1
        if left > 0:
            left_gain = limits(left - 1, right)[0] - upper - gaps[left] + gaps[left - 1]
        if right + 1 < len(result):
            right_gain = lower - limits(left, right + 1)[1] - gaps[right + 1] + gaps[right]
        if left > 0 and (right == len(result) - 1 or left_gain > right_gain
                         or left_gain == right_gain and at - left <= right - at):
            left -= 1
        else:
            right += 1


def move(items: list[dict], path: str, before: str, after: str, kind: str) -> list[dict]:
    if not path or path in {before, after} or not (before or after):
        raise PriorityMoveError("Invalid move")
    selected = [v for v in items if v["path"] == path]
    if len(selected) != 1:
        raise PriorityMoveError("Moved item is missing or ambiguous")
    remaining = [dict(v) for v in items if v["path"] != path]
    for at in range(len(remaining) + 1):
        next_path = remaining[at]["path"] if at < len(remaining) else ""
        previous_path = remaining[at - 1]["path"] if at else ""
        if next_path == before and previous_path == after:
            return plan([*remaining[:at], dict(selected[0]), *remaining[at:]], at, kind)
    raise PriorityMoveError("Adjacent scope identities have changed")
