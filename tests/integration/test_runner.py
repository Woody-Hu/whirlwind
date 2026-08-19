"""scripts/run_tests.py contract tests (ADR-0008).

The helpers (junit parsing, verdict lines, scope naming) are checked on real
junit XML, and the runner itself is executed end-to-end as a real subprocess
against real pytest targets (one green, one red) — full output must land in
.test-logs/<ts>-<scope>.log while the console sees only the verdict (§4.2: no
mocks; the runner is the subprocess-orchestration SUT).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "scripts" / "run_tests.py"

_JUNIT_XML = """\
<testsuites>
  <testsuite name="suite" tests="4" failures="1" errors="1" skipped="1">
    <testcase classname="tests.unit.test_demo" name="test_ok"/>
    <testcase classname="tests.unit.test_demo" name="test_skipped">
      <skipped/>
    </testcase>
    <testcase classname="tests.unit.test_demo" name="test_fails">
      <failure message="assert 1 == 2">def test_fails():
&gt;       assert 1 == 2
E       assert 1 == 2</failure>
    </testcase>
    <testcase classname="tests.unit.test_demo" name="test_errors">
      <error message="TypeError: boom() takes 0 arguments"/>
    </testcase>
  </testsuite>
</testsuites>
"""


@pytest.fixture(scope="module")
def runner() -> ModuleType:
    """Load scripts/run_tests.py as a module (main() is __main__-guarded)."""
    spec = importlib.util.spec_from_file_location("whirlwind_test_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------- helpers


def test_scope_naming(runner: ModuleType) -> None:
    assert runner._scope(["tests", "-q", "-m", "not e2e"]) == "tests_-q_-m_not_e2e"
    assert runner._scope([]) == "default"
    assert len(runner._scope(["x" * 200])) <= 80
    assert runner._scope(["a b/c:d"]) == "a_b_c_d"


def test_summary_from_junit(runner: ModuleType, tmp_path: Path) -> None:
    xml = tmp_path / "junit.xml"
    xml.write_text(_JUNIT_XML, encoding="utf-8")
    counts, failures = runner._summary_from_junit(xml)
    assert counts == {"tests": 4, "failures": 1, "errors": 1, "skipped": 1}
    assert failures == [
        # assert-failure: no "Type: " prefix -> kind as the type
        ("tests/unit/test_demo.py::test_fails", "failure", "assert 1 == 2"),
        # exception: pytest carries the repr in message -> type split off
        ("tests/unit/test_demo.py::test_errors", "TypeError", "boom() takes 0 arguments"),
    ]


def test_verdict_lines_pass(runner: ModuleType) -> None:
    counts = {"tests": 3, "failures": 0, "errors": 0, "skipped": 1}
    lines = runner._verdict_lines(counts, [], exit_code=0)
    assert lines == ["PASS · 2 passed · 1 skipped · exit=0"]


def test_verdict_lines_fail_lists_each_failure(runner: ModuleType) -> None:
    counts = {"tests": 4, "failures": 1, "errors": 1, "skipped": 0}
    failures = [
        ("tests/unit/test_demo.py::test_fails", "AssertionError", "assert 1 == 2"),
        ("tests/unit/test_demo.py::test_errors", "TypeError", "boom"),
    ]
    lines = runner._verdict_lines(counts, failures, exit_code=1)
    assert lines[0] == "FAIL · 2 failed · 2 passed · exit=1"
    assert lines[1] == "  - tests/unit/test_demo.py::test_fails — AssertionError: assert 1 == 2"
    assert lines[2] == "  - tests/unit/test_demo.py::test_errors — TypeError: boom"


def test_verdict_lines_nothing_collected_is_fail(runner: ModuleType) -> None:
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    assert runner._verdict_lines(counts, [], exit_code=5)[0].startswith("FAIL")


# ---------------------------------------------------------------- end-to-end


def _log_from(out: str) -> Path:
    match = re.search(r"^log: (.+)$", out, flags=re.MULTILINE)
    assert match, out
    return Path(match.group(1))


def test_end_to_end_green_run(runner: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    """Green run: exit 0, PASS verdict, full pytest output in the log file."""
    target = "tests/unit/test_statemachine.py::test_session_legal_transitions"
    rc = runner.main([target, "-q", "--no-header"])
    out = capsys.readouterr().out
    assert rc == 0
    assert re.search(r"^PASS · 1 passed · exit=0$", out, flags=re.MULTILINE), out
    log = _log_from(out)
    assert log.exists()
    content = log.read_text(encoding="utf-8")
    assert content.startswith("# whirlwind test run ")
    assert "# platform: " in content
    assert "1 passed" in content  # pytest's own summary lives in the file


def test_end_to_end_red_run(runner: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Red run: pytest's exit code mirrors through, the console gets nodeid +
    exception type + message, and the full traceback is only in the log file."""
    failing = tmp_path / "test_runner_red.py"
    failing.write_text(
        "def test_boom():\n    raise TypeError('boom message')\n",
        encoding="utf-8",
    )
    rc = runner.main([str(failing), "-q", "--no-header"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL · 1 failed · exit=1" in out
    assert "test_runner_red.py::test_boom — TypeError: boom message" in out
    assert "raise TypeError" not in out  # traceback stays in the log, not the console
    log = _log_from(out)
    content = log.read_text(encoding="utf-8")
    assert "FAILURES" in content  # pytest's full failure section
    assert "raise TypeError('boom message')" in content
