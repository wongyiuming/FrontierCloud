"""Keep hosted Python checks bounded and native acceptance on the development host."""
from pathlib import Path
import re
import shlex


ROOT = Path(__file__).resolve().parents[1]
HEAVY = re.compile(
    r"test-python-(?:final|operator|s3|migration-hour|mysql|certificate)[\w-]*\.sh"
    r"|test_python_units\.py|unittest\s+(?:-\w+\s+)*discover"
    r"|\bdocker(?:-compose)?\s"
    r"|\b(?:pip\s+install|playwright|certutil|google-chrome|apt-get|nohup)\b"
    r"|runs-on:\s*.*self-hosted|\btests[/.](?!compose_layout_native\b)[\w]*_native\b"
    r"|\b(?:federation_stack|browser_ui_regression)\b"
)
BACKGROUND = re.compile(r"(?<!&)&(?!&)")
BOUNDED_RUNS = {
    "source": "timeout --signal=TERM --kill-after=5s 150s bash scripts/test-ci-source.sh",
    "test-compose": "timeout --signal=TERM --kill-after=5s 150s python -m tests.compose_layout_native",
}
SOURCE_UNITS = (
    "tests.test_repository_policy", "tests.test_deployment_contract", "tests.test_federation_contract",
    "tests.test_operator_release", "tests.test_operator_source", "tests.test_native_acceptance_cleanup",
)


def inspect_workflow(text, name):
    findings = []
    sections = text.split("\njobs:\n", 1)
    if len(sections) != 2:
        return [f"{name}: jobs block missing"]
    if re.search(r"(?:contents|actions|packages|id-token):\s*write|permissions:\s*write-all", text):
        findings.append(f"{name}: hosted checks require read-only permissions")
    if HEAVY.search(sections[1]) or BACKGROUND.search(sections[1]):
        findings.append(f"{name}: heavy or background acceptance belongs on the development host")
    blocks = re.split(r"(?m)^  ([\w-]+):\s*$", sections[1])
    jobs = dict(zip(blocks[1::2], blocks[2::2]))
    if len(jobs) != len(blocks[1::2]):
        findings.append(f"{name}: duplicate job names are prohibited")
    for job, block in jobs.items():
        limits = re.findall(r"(?m)^    timeout-minutes:\s*(\d+)\s*$", block)
        if len(limits) != 1 or not 1 <= int(limits[0]) <= 3:
            findings.append(f"{name}/{job}: explicit timeout from one through three minutes required")
        if re.search(r"(?m)^    needs:", block):
            findings.append(f"{name}/{job}: serial hosted test chains are prohibited")
        if re.search(r"continue-on-error:\s*true", block):
            findings.append(f"{name}/{job}: check failures cannot be ignored")
    if name == "docker.yml":
        if set(jobs) != {"source", "test-compose", "topology", "promote-main"}:
            findings.append(f"{name}: exact compatible source and promotion jobs required")
        if re.search(r"(?m)^  (?:pull_request|pull_request_target):", sections[0]):
            findings.append(f"{name}: PR topology has its own workflow; do not duplicate source runs")
        for job, command in BOUNDED_RUNS.items():
            runs = re.findall(r"(?m)^\s+(?:- )?run:\s*(.*)$", jobs.get(job, ""))
            if runs != [command]:
                findings.append(f"{name}/{job}: one exact foreground 150-second command required")
        if "concurrency:" in sections[0] or "concurrency:" in jobs.get("promote-main", ""):
            findings.append(f"{name}: main promotion must not be cancelled by source concurrency")
    return findings


def check_workflows(directory):
    return [finding for path in sorted(directory.glob("*.y*ml"))
            for finding in inspect_workflow(path.read_text(encoding="utf-8"), path.name)]


def inspect_source(text):
    findings = []
    if HEAVY.search(text) or BACKGROUND.search(text):
        findings.append("test-ci-source.sh: heavy or background acceptance is prohibited")
    if "set -euo pipefail" not in text:
        findings.append("test-ci-source.sh: foreground failures must stop the check")
    if re.search(r"\|\|\s*(?:true|:)|\bexit\s+0\b", text):
        findings.append("test-ci-source.sh: check failures cannot be ignored")
    units = [line for line in text.replace("\\\n", " ").splitlines()
             if re.match(r"^\s*python\s+-m\s+unittest\b", line)]
    if len(units) != 1 or shlex.split(units[0]) != ["python", "-m", "unittest", "-v", *SOURCE_UNITS]:
        findings.append("test-ci-source.sh: only the explicit fast source-contract unit roster is permitted")
    return findings


if __name__ == "__main__":
    issues = check_workflows(ROOT / ".github/workflows")
    issues.extend(inspect_source((ROOT / "scripts/test-ci-source.sh").read_text(encoding="utf-8")))
    if issues:
        raise SystemExit("\n".join(issues))
    print("Hosted checks passed: parallel jobs <=3 minutes, foreground checks <=150 seconds, no native acceptance")
