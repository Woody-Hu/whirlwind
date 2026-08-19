# ADR-0008: Test execution logging — full output to file, terse verdict to console

# ADR-0008：测试执行日志——完整输出落文件，控制台只给简明结论

- Status: Accepted
- Date: 2026-08-19
- Related: [AGENTS.md](../../AGENTS.md) §4（测试规范，本 ADR 落为其 §4.5）、[ADR-0001](0001-agent-runtime-m1.md) §6（禁 mock/伪造铁律）、[ADR-0007](0007-platform-abstraction.md)（日志头记录平台事实）

- 状态：已接受
- 日期：2026-08-19
- 关联：[AGENTS.md](../../AGENTS.md) §4（测试规范，本 ADR 落为其 §4.5）、[ADR-0001](0001-agent-runtime-m1.md) §6（禁 mock/伪造铁律）、[ADR-0007](0007-platform-abstraction.md)（日志头记录平台事实）

---

## Context

## 背景

pytest runs today print everything to the console: progress dots, skip reasons, failure tracebacks, benchmark tables. For long suites and benchmarks the verdict (the only thing you need at a glance) is buried; for agents/ci the useful contract is a **simple result** — did it error, and if so with which exception code and message — with the full log available **on demand**. The same applies to benchmarks (pytest-benchmark tables are detail, not verdict).

今天 pytest 把一切都打到控制台：进度点、skip 理由、失败堆栈、benchmark 表格。长套件与基准下，唯一需要一眼看到的东西——结论——被淹没；对 agent/CI 而言，有用的契约是一个**简单的结果**——是否出错、错了的话异常码与异常信息是什么——完整日志**按需查阅**。基准（pytest-benchmark 表格是细节而非结论）同理。

## Decision — D1: a subprocess runner script, not a pytest plugin

## 决策——D1：子进程 runner 脚本，而非 pytest 插件

`scripts/run_tests.py` wraps `python -m pytest` as a subprocess:

- captures **all** stdout/stderr (pytest + plugins + benchmark tables + subprocess output) without touching pytest's invocation surface;
- direct pytest invocation stays first-class for interactive debugging (`-x`, `-s`, live output);
- a plugin could not redirect *its own* console output without fighting pytest's terminal reporter.

`scripts/run_tests.py` 以子进程方式包装 `python -m pytest`：完整捕获 stdout/stderr（pytest、插件、benchmark 表格、子进程输出）而不改动 pytest 的调用面；直接调用 pytest 仍是一等公民，用于交互式调试（`-x`、`-s`、实时输出）；插件方案无法在不与 pytest 终端报告器搏斗的情况下重定向其自身的控制台输出。

```
uv run python scripts/run_tests.py                    # 默认：tests -q -m "not e2e"
uv run python scripts/run_tests.py tests/unit -q      # 任意 pytest 参数透传
uv run python scripts/run_tests.py tests/benchmark/test_pipeline_bench.py -q
```

## Decision — D2: junitxml is the structured verdict source

## 决策——D2：junitxml 作为结构化结论来源

The runner passes `--junitxml=<tmp>` and parses counts (tests/failures/errors/skipped) and per-case failures (nodeid, exception `type`, first line of `message`) from the XML — a stable schema, no console scraping, no brittle regex over pytest output.

runner 附加 `--junitxml=<tmp>`，从 XML 解析计数（tests/failures/errors/skipped）与逐例失败（nodeid、异常 `type`、`message` 首行）——schema 稳定，不刮控制台，不对 pytest 输出做脆弱正则。

## Decision — D3: log file contract

## 决策——D3：日志文件契约

- Location: `.test-logs/` (repo root, gitignored — dev artifact, not source).
- Name: `<YYYYMMDD-HHMMSS>-<scope>.log`; `scope` derived from the pytest args (e.g. `tests_unit`, `full_not_e2e`).
- Content: header (command, cwd, exit code, timestamp, **platform facts** from ADR-0007 — dogfooding the abstraction) followed by the merged full output.
- Retention is left to the developer (unbounded by default; the directory is disposable).

- 位置：`.test-logs/`（仓库根、gitignore——开发产物，非源码）。
- 命名：`<YYYYMMDD-HHMMSS>-<scope>.log`；`scope` 由 pytest 参数推导（如 `tests_unit`、`full_not_e2e`）。
- 内容：头部（命令、cwd、退出码、时间戳、来自 ADR-0007 的**平台事实**——顺手 dogfood 平台抽象）+ 合并后的完整输出。
- 保留策略交给开发者（默认不清理；该目录可随时丢弃）。

## Decision — D4: console contract & exit code

## 决策——D4：控制台契约与退出码

```
running: pytest tests -q -m "not e2e"          # 一行状态，便于长跑感知存活
PASS · 164 passed · 18 skipped · exit=0
log: .test-logs/20260819-153000-full.log

FAIL · 2 failed · 162 passed · 18 skipped · exit=1
  - tests/unit/test_platform.py::test_x — AssertionError: expected linux, got macos
  - tests/integration/test_y.py::test_z — TimeoutError: timed out after 120s
log: .test-logs/20260819-153010-unit.log
```

- One verdict line (PASS/FAIL + counts + pytest exit code); on failure, **one line per failure: nodeid — 异常类型（码）: 异常信息首行**; then the log path.
- Exit code mirrors pytest's (0 ok / 1 failures / 2 interrupted / 3 internal / 4 usage / 5 nothing collected) — callers (CI, agents) read it as the simple "是否错误" answer.
- No emojis, no color codes: the output is itself a log line for whoever consumes it.

- 一行结论（PASS/FAIL + 计数 + pytest 退出码）；失败时**每个失败一行：nodeid — 异常类型（码）: 异常信息首行**；随后是日志路径。
- 退出码镜像 pytest（0 正常 / 1 失败 / 2 中断 / 3 内部错误 / 4 用法错误 / 5 未收集到用例）——调用方（CI、agent）把它当作简单的"是否错误"答案。
- 不用 emoji、不用颜色码：输出本身就是给消费方的日志行。

## Decision — D5: the convention lands in AGENTS.md §4.5

## 决策——D5：规范落为 AGENTS.md §4.5

Canonical batch runs go through the runner; direct pytest stays for interactive debugging. AGENTS.md §4.4 commands are updated accordingly.

批量跑测试的规范入口是 runner；直接 pytest 用于交互式调试。AGENTS.md §4.4 的命令同步更新。

## Test strategy

## 测试策略

`tests/integration/test_runner.py` — the runner is itself behaviour-verified with **real subprocesses** (no mocks):

- a real passing unit module → exit 0, `PASS` line, log file exists and contains the pytest output;
- a real failing temp test file → exit 1, `FAIL` line, failure line carries the exception type and message;
- the log header records the platform facts.

`tests/integration/test_runner.py`——runner 本身用**真实子进程**做行为验证（无 mock）：真实通过的 unit 模块 → 退出码 0、`PASS` 行、日志文件存在且含 pytest 输出；真实失败的临时测试文件 → 退出码 1、`FAIL` 行、失败行带异常类型与信息；日志头记录平台事实。

## Conflict check

## 冲突检查

No conflict with architecture v0.6 (dev tooling, no runtime surface). §4.2 no-fake rule is untouched: the runner changes *where output goes*, never what the tests assert; skipped tests remain visible in counts + log.

与架构 v0.6 无冲突（开发工具，无运行时表面）。§4.2 禁伪造铁律不受影响：runner 改变的是**输出去向**，从不改变测试断言的内容；skip 的测试在计数与日志中依然可见。

## Risks / open points

## 风险与开放点

- junitxml `classname`/`name` approximate nodeids (dots vs paths); the log file is the authoritative record — the console line is a locator hint, not the source of truth.
- Benchmarks gain nothing from junit (tables live in the log); a future ADR may add machine-readable baseline comparison if needed.
- junitxml 的 `classname`/`name` 只能近似 nodeid（点号 vs 路径）；日志文件才是权威记录——控制台行是定位提示，不是事实源。
- 基准测试从 junit 得不到额外信息（表格在日志里）；如需要机器可读的基线对比，留待未来 ADR。
