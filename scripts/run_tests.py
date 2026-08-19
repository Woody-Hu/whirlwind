#!/usr/bin/env python3
"""Whirlwind test runner: full output to a log file, terse verdict to console (ADR-0008).

Canonical way to run tests/benchmarks:

    uv run python scripts/run_tests.py                        # tests -q -m "not e2e"
    uv run python scripts/run_tests.py tests/unit -q          # any pytest args pass through
    uv run python scripts/run_tests.py tests/benchmark/test_pipeline_bench.py -q

Behaviour:
- pytest runs as a subprocess with ALL output (pytest, plugins, benchmark
  tables, subprocess chatter) captured to .test-logs/<ts>-<scope>.log —
  inspect on demand; the directory is a disposable dev artifact;
- the console gets one verdict line (+ one line per failure: nodeid,
  exception type, first line of the message) and the log path;
- the exit code mirrors pytest's (0 ok / 1 failures / 2 interrupted /
  3 internal / 4 usage / 5 nothing collected) — that is the simple
  "did it error" answer for CI and agents.

For interactive debugging (-x, -s, live output) invoke pytest directly.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = REPO_ROOT / ".test-logs"
DEFAULT_ARGS = ["tests", "-q", "-m", "not e2e"]

# counts read from <testsuite> attributes; failures/errors read per <testcase>
_COUNT_ATTRS = ("tests", "failures", "errors", "skipped")


def _scope(args: list[str]) -> str:
    """A filesystem-friendly scope name derived from the pytest args."""
    joined = "_".join(args).strip() or "default"
    scope = re.sub(r"[^A-Za-z0-9._-]+", "_", joined)
    return scope[:80] or "default"


def _platform_header() -> str:
    """Platform facts in the log header (ADR-0007 dogfood). Plain python (no
    venv guarantees when invoked directly) — degrade to n/a instead of dying."""
    try:
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from whirlwind.core.platform import current_facts  # noqa: PLC0415

        facts = current_facts()
        return (
            f"platform: {facts.system}/{facts.machine} "
            f"vsock={facts.vsock} restricted={facts.restricted} "
            f"rlimit_as={facts.rlimit_as_supported} overridden={facts.overridden}"
        )
    except Exception as exc:  # pragma: no cover - header nicety only
        return f"platform: n/a ({exc})"


def _nodeid(case: ET.Element) -> str:
    """Approximate the pytest nodeid from junit classname/name (dots -> path).

    The log file is the authoritative record; this is a console locator hint.
    """
    classname = (case.get("classname") or "").replace(".", "/")
    name = case.get("name") or ""
    if classname:
        return f"{classname}.py::{name}"
    return name


def _summary_from_junit(xml_path: Path) -> tuple[dict[str, int], list[tuple[str, str, str]]]:
    """Parse junitxml into (counts, failures[(nodeid, type, message-first-line)]).

    junitxml is the structured verdict source — no console scraping (ADR-0008 D2).
    pytest's xunit2 failure elements carry no ``type`` attribute: the exception
    repr sits in ``message`` ("TypeError: boom"); split that leading type so the
    console line reads ``nodeid — TypeError: boom``. The log file stays the
    authoritative record.
    """
    counts = {attr: 0 for attr in _COUNT_ATTRS}
    failures: list[tuple[str, str, str]] = []
    root = ET.parse(xml_path).getroot()  # <testsuites> or a bare <testsuite>
    for suite in root.iter("testsuite"):
        for attr in _COUNT_ATTRS:
            counts[attr] += int(suite.get(attr) or 0)
        for case in suite.iter("testcase"):
            for kind in ("failure", "error"):
                el = case.find(kind)
                if el is None:
                    continue
                message = (el.get("message") or el.text or "").strip()
                first_line = message.splitlines()[0] if message else ""
                etype = el.get("type")
                if not etype:
                    head, sep, tail = first_line.partition(": ")
                    if sep and tail:
                        etype, first_line = head, tail
                    else:
                        etype = kind
                failures.append((_nodeid(case), etype, first_line[:200]))
    return counts, failures


def _verdict_lines(counts: dict[str, int], failures: list[tuple[str, str, str]], exit_code: int) -> list[str]:
    passed = counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"]
    parts = [
        f"{counts['failures'] + counts['errors']} failed" if counts["failures"] + counts["errors"] else None,
        f"{passed} passed" if passed else None,
        f"{counts['skipped']} skipped" if counts["skipped"] else None,
    ]
    status = "FAIL" if (failures or exit_code not in (0, 5)) else "PASS"
    if exit_code == 5 and not failures:
        status = "FAIL"  # nothing collected is an error for the caller
    line = f"{status} · " + " · ".join(p for p in parts if p) + f" · exit={exit_code}"
    lines = [line]
    for nodeid, etype, message in failures:
        suffix = f": {message}" if message else ""
        lines.append(f"  - {nodeid} — {etype}{suffix}")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv) or DEFAULT_ARGS
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"{stamp}-{_scope(args)}.log"

    cmd = [sys.executable, "-m", "pytest", *args]
    print(f"running: {' '.join(cmd)}")
    with tempfile.TemporaryDirectory() as tmp:
        xml_path = Path(tmp) / "junit.xml"
        proc = subprocess.run(
            [*cmd, f"--junitxml={xml_path}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        header = (
            f"# whirlwind test run {stamp}\n"
            f"# command: {' '.join(cmd)} (cwd: {REPO_ROOT})\n"
            f"# exit: {proc.returncode}\n"
            f"# {_platform_header()}\n"
        )
        log_path.write_text(
            header + proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else ""),
            encoding="utf-8",
        )
        try:
            counts, failures = _summary_from_junit(xml_path)
        except (ET.ParseError, OSError):
            counts = {attr: 0 for attr in _COUNT_ATTRS}
            failures = []
            stderr_tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            for line in stderr_tail:
                print(f"  - {line}")

    for line in _verdict_lines(counts, failures, proc.returncode):
        print(line)
    print(f"log: {log_path}")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
