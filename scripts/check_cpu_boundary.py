#!/usr/bin/env python3
"""Reject product-runtime constructs that can introduce CPU-heavy computation."""
from __future__ import annotations

import ast
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
PROHIBITED_IMPORTS = {
    "bz2",
    "concurrent.futures",
    "ctypes",
    "gzip",
    "lzma",
    "multiprocessing",
    "numba",
    "numpy",
    "pandas",
    "scipy",
    "subprocess",
    "tensorflow",
    "torch",
    "zlib",
}
PROHIBITED_CALLS = {
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "os.popen",
    "os.system",
}
PROHIBITED_CALL_PREFIXES = ("multiprocessing.", "os.spawn", "subprocess.")


def dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def scan_source(source: str, name: str = "<source>") -> list[str]:
    tree = ast.parse(source, filename=name)
    violations: list[str] = []
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        for module in modules:
            if any(module == denied or module.startswith(denied + ".") for denied in PROHIBITED_IMPORTS):
                violations.append(f"{name}:{node.lineno}: prohibited runtime import {module}")
        if isinstance(node, ast.Call):
            call = dotted_name(node.func)
            if call in PROHIBITED_CALLS or call.startswith(PROHIBITED_CALL_PREFIXES):
                violations.append(f"{name}:{node.lineno}: prohibited runtime execution {call}")
            if call.endswith("run_in_executor"):
                violations.append(f"{name}:{node.lineno}: executor work is prohibited in product runtime")
    return violations


def main() -> int:
    paths = [ROOT / "main.py", *(ROOT / "app").rglob("*.py")]
    violations = [
        finding
        for path in paths
        for finding in scan_source(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))
    ]
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for dependency in project.get("project", {}).get("dependencies", []):
        package = dependency.split("[", 1)[0].split("=", 1)[0].strip().lower().replace("-", "_")
        if package in {name.replace(".", "_") for name in PROHIBITED_IMPORTS}:
            violations.append(f"pyproject.toml: prohibited runtime dependency {dependency}")
    if violations:
        print("The one-core runtime boundary rejects CPU-heavy computation:")
        print("\n".join(violations))
        return 1
    print("One-core product-runtime boundary passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
