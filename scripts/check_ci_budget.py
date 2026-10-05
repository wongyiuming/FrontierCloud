"""Enforce the operator's three-minute hosted-CI boundary without dependencies."""
from pathlib import Path
import re


HEAVY = re.compile(
    r"test-(?:mixed-runtime|mixed-release|go-business|go-updater|native-default|store-interop|go-deployment)\.sh"
    r"|docker\s+(?:build|buildx|compose\s+up)|federation_stack\.py|browser_ui_regression\.py"
    r"|runs-on:\s*.*self-hosted|\bnohup\b"
)


def inspect_workflow(text: str, name: str) -> list[str]:
    findings = []
    jobs = text.split("\njobs:\n", 1)
    if len(jobs) != 2:
        return findings + [f"{name}: jobs block missing"]
    if HEAVY.search(jobs[1]):
        findings.append(f"{name}: heavyweight/background acceptance is prohibited in hosted CI")
    blocks = re.split(r"(?m)^  ([a-zA-Z0-9_-]+):\s*$", jobs[1])
    for index in range(1, len(blocks), 2):
        job, block = blocks[index:index + 2]
        limits = re.findall(r"(?m)^    timeout-minutes:\s*(\d+)\s*$", block)
        if len(limits) != 1 or not 1 <= int(limits[0]) <= 3:
            findings.append(f"{name}/{job}: explicit timeout-minutes from 1 through 3 required")
        if re.search(r"(?m)^    needs:", block):
            findings.append(f"{name}/{job}: serial CI job chains can exceed the workflow budget")
    return findings


def check_workflows(directory: Path) -> list[str]:
    return [finding for path in sorted(directory.glob("*.y*ml"))
            for finding in inspect_workflow(path.read_text(encoding="utf-8"), path.name)]


if __name__ == "__main__":
    issues = check_workflows(Path(__file__).resolve().parents[1] / ".github/workflows")
    if issues:
        raise SystemExit("\n".join(issues))
    print("CI budget passed: at most three minutes; no heavyweight acceptance or serial chains")
