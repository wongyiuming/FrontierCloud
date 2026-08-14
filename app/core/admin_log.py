from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from threading import Lock


class AdminLogBuffer:
    """Small in-memory mirror of security-relevant web container log lines."""

    def __init__(self, max_lines: int = 500):
        self._lines: deque[dict] = deque(maxlen=max_lines)
        self._lock = Lock()
        self._sequence = 0

    def append(self, line: str) -> None:
        safe_line = str(line).replace("\x00", "")[:4000]
        with self._lock:
            self._sequence += 1
            self._lines.append({
                "id": self._sequence,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "line": safe_line,
            })

    def read(self, after: int = 0, limit: int = 200) -> list[dict]:
        bounded_limit = max(1, min(limit, 500))
        with self._lock:
            matches = [entry.copy() for entry in self._lines if entry["id"] > after]
        return matches[-bounded_limit:]


admin_log_buffer = AdminLogBuffer()


def append_admin_log(line: str) -> None:
    admin_log_buffer.append(line)
