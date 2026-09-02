from __future__ import annotations

import logging
import unicodedata


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


def append_admin_log(line: str) -> None:
    """Compatibility helper that writes to stdout without retaining logs in memory."""
    logging.getLogger("frontiercloud.application").info(
        "application_event",
        extra={"context": {"detail": sanitize_log_value(line)}},
    )
