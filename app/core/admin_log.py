from __future__ import annotations

import unicodedata
from collections import deque
from datetime import UTC, datetime
from threading import Lock


def sanitize_log_value(value: object, max_length: int = 4000) -> str:
    """Keep one untrusted value on one printable log line."""
    output = []
    output_length = 0
    for character in str(value):
        if character == "\r":
            rendered = "\\r"
        elif character == "\n":
            rendered = "\\n"
        elif character == "\t":
            rendered = "\\t"
        elif unicodedata.category(character).startswith("C"):
            rendered = f"\\u{{{ord(character):x}}}"
        else:
            rendered = character
        output.append(rendered)
        output_length += len(rendered)
        if output_length >= max_length:
            break
    return "".join(output)[:max_length]


class AdminLogBuffer:
    """Small in-memory mirror of security-relevant web container log lines."""

    def __init__(self, max_lines: int = 500):
        self._lines: deque[dict] = deque(maxlen=max_lines)
        self._lock = Lock()
        self._sequence = 0

    def append(self, line: str) -> None:
        safe_line = sanitize_log_value(line)
        with self._lock:
            self._sequence += 1
            self._lines.append({
                "id": self._sequence,
                "timestamp": datetime.now(UTC).isoformat(),
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
