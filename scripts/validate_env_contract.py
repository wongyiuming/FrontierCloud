from __future__ import annotations

import argparse
import re
from pathlib import Path


VARIABLE_PATTERN = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=")
CONTRACT_PATTERN = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=")


def contract_names(example_path: Path) -> set[str]:
    names: set[str] = set()
    for line in example_path.read_text(encoding="utf-8-sig").splitlines():
        match = CONTRACT_PATTERN.match(line)
        if match:
            names.add(match.group(1))
    return names


def configured_names(env_path: Path) -> tuple[set[str], list[int]]:
    if not env_path.exists():
        return set(), []
    names: set[str] = set()
    invalid_lines: list[int] = []
    for line_number, raw_line in enumerate(
        env_path.read_text(encoding="utf-8-sig").splitlines(), start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        match = VARIABLE_PATTERN.match(line)
        if not match:
            invalid_lines.append(line_number)
            continue
        names.add(match.group(1))
    return names, invalid_lines


def validate_contract(env_path: Path, example_path: Path) -> list[str]:
    configured, invalid_lines = configured_names(env_path)
    supported = contract_names(example_path)
    errors = [f"invalid dotenv syntax at line {number}" for number in invalid_lines]
    unsupported = sorted(configured - supported)
    if unsupported:
        errors.append("unsupported environment variables: " + ", ".join(unsupported))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate .env against the formal project contract.")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--example-file", type=Path, default=Path(".env.example"))
    args = parser.parse_args()

    errors = validate_contract(args.env_file, args.example_file)
    if errors:
        for error in errors:
            print(f"Environment contract violation: {error}")
        return 1
    configured, _ = configured_names(args.env_file)
    print(f"Environment contract validation passed ({len(configured)} configured variables).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
