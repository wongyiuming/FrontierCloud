from __future__ import annotations

import ast
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
HASH_COMMENT_SUFFIXES = {
    ".conf",
    ".hcl",
    ".http",
    ".sh",
    ".toml",
    ".yaml",
    ".yml",
}
SLASH_COMMENT_SUFFIXES = {".css", ".html", ".htm", ".js", ".mjs", ".ts"}


def tracked_files() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
    )
    return [ROOT / value.decode() for value in output.rstrip(b"\0").split(b"\0") if value]


def python_violations(path: Path) -> list[tuple[int, str]]:
    with tokenize.open(path) as stream:
        text = stream.read()
    violations: list[tuple[int, str]] = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT and CJK.search(token.string):
            violations.append((token.start[0], token.string.strip()))

    tree = ast.parse(text, filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        docstring = ast.get_docstring(node, clean=False)
        if docstring and CJK.search(docstring):
            violations.append((node.body[0].lineno, "non-English docstring"))
    return violations


def comment_markers(path: Path) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    if path.suffix in SLASH_COMMENT_SUFFIXES:
        line_markers = ("//",)
        block_markers = (("/*", "*/"), ("<!--", "-->"))
        return line_markers, block_markers
    if (
        path.suffix in HASH_COMMENT_SUFFIXES
        or path.name in {"Dockerfile", ".dockerignore", ".gitignore"}
        or path.name.endswith(".env.example")
    ):
        return ("#",), ()
    return (), ()


def text_violations(path: Path) -> list[tuple[int, str]]:
    line_markers, block_markers = comment_markers(path)
    if not line_markers and not block_markers:
        return []

    violations: list[tuple[int, str]] = []
    active_block_end: str | None = None
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        index = 0
        quote: str | None = None
        escaped = False
        while index < len(line):
            if active_block_end is not None:
                end = line.find(active_block_end, index)
                comment = line[index:] if end < 0 else line[index:end]
                if CJK.search(comment):
                    violations.append((line_number, comment.strip()))
                    break
                if end < 0:
                    break
                index = end + len(active_block_end)
                active_block_end = None
                continue

            character = line[index]
            if escaped:
                escaped = False
                index += 1
                continue
            if quote is not None:
                if character == "\\":
                    escaped = True
                elif character == quote:
                    quote = None
                index += 1
                continue
            if character in {"'", '"', "`"}:
                quote = character
                index += 1
                continue

            line_marker = next((marker for marker in line_markers if line.startswith(marker, index)), None)
            if line_marker is not None:
                comment = line[index + len(line_marker):]
                if CJK.search(comment):
                    violations.append((line_number, comment.strip()))
                break

            block_marker = next(
                ((start, end) for start, end in block_markers if line.startswith(start, index)),
                None,
            )
            if block_marker is not None:
                start, active_block_end = block_marker
                index += len(start)
                continue
            index += 1
    return violations


def main() -> int:
    violations: list[str] = []
    for path in tracked_files():
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        findings = python_violations(path) if path.suffix == ".py" else text_violations(path)
        for line_number, comment in findings:
            violations.append(f"{relative}:{line_number}: {comment}")

    if violations:
        print("Comments and docstrings must be written in English:", file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("English comment check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
