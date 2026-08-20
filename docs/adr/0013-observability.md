# ADR-0013: Observability — metrics, structured logging, W3C trace context

# ADR-0013：可观测性——指标、结构化日志、W3C 追踪上下文

- Status: Accepted
- Date: 2026-08-20
- Related: [ADR-0008](0008-test-logging.md)（runner 日志落盘/结论解耦的先例）、[ADR-0009](0009-unified-config.md)（统一配置 schema/分层注入，`[logging]` 分区在此扩展）、[ADR-0007](0007-platform-abstraction.md)（探测类事实不被覆盖）、[ADR-0010](0010-agent-env-secrets.md)（宿主侧组装范式）、架构文档 5.x（Gateway 接入层）

- 状态：已接受
- 日期：2026-08-20
- 关联：[ADR-0008](0008-test-logging.md)（runner 落盘/结论解耦先例）、[ADR-0009](0009-unified-config.md)（统一配置，`[logging]` 分区在此扩展）、[ADR-0007](0007-platform-abstraction.md)（探测事实不被覆盖）、[ADR-0010](0010-agent-env-secrets.md)（宿主侧组装范式）、架构文档 5.x（Gateway 接入层）

---

## Context

## 背景

**English.** A production runtime can only be operated if you can see what it is
doing. Today whirlwind logs to the console as plain text (one opaque line per
record, no correlation), exposes no metrics, and has no way to trace a request
from the gateway call through control plane to a WAL commit. The observability
closure the roadmap asks for is the *minimal honest set* that does not lock us
into a vendor: (a) a first-class metrics surface a Prometheus scrape can read,
(b) machine-parseable structured logging keyed by a request/correlation id, and
(c) W3C Trace Context propagation so a gateway call and the work it triggers can
be joined. Each piece must stay dependency-light, honest about what it measures
(no fabricated numbers), and wired at one composition root.

**中文.** 生产级运行时必须能「看得见」自己在做什么，才谈得上运维。今天 whirlwind 以纯文本打控制台日志（每条记录一行，无关联信息）、不暴露任何指标、也没有办法把一次网关调用经控制面一直追踪到一次 WAL 落盘。路线图要求的可观测性闭环是*最小且诚实*的一组能力，且不得把我们锁进某个厂商：(a) 一块能被 Prometheus scrape 读到的首要指标面；(b) 以 request/correlation id 关联、机器可解析的结构化日志；以及 (c) W3C Trace Context 传播，让一次网关调用与其触发的内部工作能串起来。每一块都必须依赖轻量、对测得内容诚实（不编造数字）、并在唯一一处组合根完成接线。

## Decision — D1: dependency-free Prometheus-text metrics registry

## 决策——D1：零依赖的 Prometheus 文本指标注册表

**English.** `observability/metrics.py` hand-rolls the Prometheus text exposition
format (`text/plain; version=0.0.4`) instead of pulling in `prometheus_client`:
the format is a small, well-specified one-line-per-series text format, and
hand-rolling keeps the hot `/metrics` path cheap and the dependency surface flat.
Semantics follow the Prometheus data model:

- `Counter` — monotonic, non-decreasing (`inc` only).
- `Gauge` — arbitrary numeric value (`set`/`inc`/`dec`).
- `Histogram` — cumulative `_bucket{le=...}` series (+Inf sentinel) plus `_sum`
  and `_count`, with conventional web-latency default buckets.

All mutation happens under one `threading.Lock` so a scrape can read while
workers write (asyncio single loop or a small worker pool). Label values are
escaped per the exposition grammar (backslash, newline, double-quote). The `le`
bucket bound keeps its configured float spelling (`1.0` stays `1.0`, matching
the Prometheus client convention) while other values drop a trailing zero.
Rationale matches ADR-0003: before hand-rolling, check the mature library; the
client was *considered* and rejected for the exposition surface alone, not as a
blanket no-deps stance.

**中文.** `observability/metrics.py` 手写 Prometheus 文本暴露格式
（`text/plain; version=0.0.4`）而不引入 `prometheus_client`：该格式很小、规范明确
（每序列一行文本），手写能让热的 `/metrics` 路径更廉价、依赖面更平。语义遵循
Prometheus 数据模型：

- `Counter` — 单调不减（仅 `inc`）。
- `Gauge` — 任意数值（`set`/`inc`/`dec`）。
- `Histogram` — 累计 `_bucket{le=...}` 序列（+Inf 哨兵）外加 `_sum`/`_count`，
  使用惯用的 web 延迟默认桶。

所有变更在同一个 `threading.Lock` 下进行，scrape 时也能并发写（asyncio 单循环或小
worker 池）。label 值按暴露语法转义（反斜杠、换行、双引号）。`le` 桶上界保留配置的
float 拼写（`1.0` 仍是 `1.0`，与 Prometheus client 惯例一致），其它数值丢弃末尾 `0`。
理由与 ADR-0003 一脉相承：自造轮子前先查成熟库；`prometheus_client` 被*权衡过*，
仅就暴露面本身被否决，不是一刀切的「零依赖」立场。

## Decision — D2: structured JSON logging keyed by correlation scope

## 决策——D2：以关联作用域为键的结构化 JSON 日志

**English.** The gateway and control plane keep using the stdlib `logging`
interface, but the default plain-text formatter is replaced with a single-line
JSON formatter so every record is machine-parseable and every log line carries
stable fields (`ts`, `level`, `logger`, `msg`, plus a frozen `environment`
tag). A `contextvar` (`observability/logconfig.py`) carries the
request/correlation scope (`request_id`, `trace_id`/`span_id`, `session_id`,
...); a logging `Filter` splices those fields into every record emitted while
the scope is active. The scope is set once per request by the trace middleware
and reset in a `finally`, so correlation never leaks across requests on the same
worker. Configuration rides the unified pipeline (ADR-0009): a `[logging]`
section (`level`, `format` ∈ {json,text}, `environment`) + env overloads
`WHIRLWIND_LOG_LEVEL` / `WHIRLWIND_LOG_FORMAT` / `WHIRLWIND_LOG_ENV`. Unknown
keys are hard errors under the shared schema; new knobs are documented in the
ADR-0009 schema block.

**中文.** Gateway 与控制面继续使用 stdlib `logging` 接口，但把默认纯文本 formatter 换成
单行 JSON formatter，使每条记录机器可解析、并带有稳定字段（`ts`/`level`/`logger`/`msg`
外加冻结的 `environment` 标签）。一个 `contextvar`（`observability/logconfig.py`）携带
request/correlation 作用域（`request_id`、`trace_id`/`span_id`、`session_id`…）；一个
logging `Filter` 把那些字段拼进作用域活跃期间发出的每一条记录。作用域由 trace 中间件
每个请求设置一次、在 `finally` 中重置，保证同 worker 上先后请求不会互相污染关联信息。
配置走统一注入管线（ADR-0009）：`[logging]` 分区（`level`、`format` ∈ {json,text}、
`environment`）+ 环境变量覆盖 `WHIRLWIND_LOG_LEVEL`/`WHIRLWIND_LOG_FORMAT`/
`WHIRLWIND_LOG_ENV`。共享 schema 下未知键是硬错误；新增可调值在 ADR-0009 schema 块
中登记。

## Decision — D3: one collector, sampled gauges + event counters, single /metrics

## 决策——D3：单一 collector，采样型 gauge + 事件型 counter，单一 /metrics

**English.** `observability/collector.py` exposes one process-wide
`MetricsCollector`, created at the composition root (`WhirlwindRuntime`) and
handed to the gateway, the manager, and the WAL. It separates two kinds of
state honestly:

- **Sampled gauges** — pulled from the source of truth at render time so they
  heal across restarts instead of trusting incremental deltas. Sessions-by-status
  are sampled asynchronously from the `MetadataStore` (`render_async` runs the
  async samplers first), and sandbox population from `hostlet.population`.
- **Event counters/histograms** — incremented at the call site: turn latency
  (manager), WAL appends (`on_append` callback), and HTTP request
  method/route/status + latency via an ASGI `MetricsMiddleware`.

`/metrics` is exposed by the gateway as a `PlainTextResponse` with media type
`text/plain; version=0.0.4`. HTTP route labels are resolved against the router
so cardinality stays bounded (`/sessions/{id}/turns`, not `/sessions/s_123/...`);
unknown paths fall back to their raw path. The middleware is a plain ASGI wrapper
(not starlette's `BaseHTTPMiddleware`) so streaming responses (SSE) pass through
unbuffered, and the status code is captured from the `http.response.start` ASGI
message so a raising handler still records the 500 it ultimately returns.

**中文.** `observability/collector.py` 暴露一个进程级 `MetricsCollector`，在组合根
（`WhirlwindRuntime`）创建并交给 gateway、manager 与 WAL。它诚实地把两类状态分开：

- **采样型 gauge** ——在渲染时从事实源拉取，跨重启自愈，不依赖增量累加。
  sessions-by-status 从 `MetadataStore` 异步采样（`render_async` 先跑异步 sampler），
  sandbox 数量来自 `hostlet.population`。
- **事件型 counter/histogram** ——在调用点递增：turn 延迟（manager）、WAL 追加
  （`on_append` 回调）、以及经 ASGI `MetricsMiddleware` 记录的 HTTP
  method/route/status + 延迟。

`/metrics` 由 gateway 以 `PlainTextResponse`、媒体类型 `text/plain; version=0.0.4`
暴露。HTTP route 标签按 router 解析以约束基数（`/sessions/{id}/turns` 而非
`/sessions/s_123/...`）；未知路径回退到原始路径。中间件是纯 ASGI 包装（非 starlette
的 `BaseHTTPMiddleware`），流式响应（SSE）可无缓冲直通，状态码取自
`http.response.start` ASGI 消息，因此抛异常的 handler 仍能记录它最终返回的 500。

## Decision — D4: W3C Trace Context — always-on, cheap, propagated

## 决策——D4：W3C Trace Context——常开、廉价、可传播

**English.** The runtime adopts the W3C Trace Context v1 wire format. `trace.py`
provides parse/format/child/root primitives over `traceparent`
(`00-{trace-id}-{span-id}-{flags}`, 32-hex trace id / 16-hex span id, sampled
flag) plus `new_root`/`child` span derivation. A `TraceMiddleware` in the gateway
parses an inbound `traceparent`, or starts a new sampled root when absent, then
opens a child span and installs it into the correlation scope (D2) so every log
line during the request carries the trace/span ids. The span id written back is
the gateway's own (there is no parent-span field on the wire); a
`traceresponse` header echoes the derived context to the caller. `outbound_traceparent()`
hands an outbound caller the active span so a downstream hop stays in the same
trace. Tracing is always-on and cheap (parse + two contextvar writes per
request); sampling policy is deferred — the flag is default `01`, and turning on
probabilistic sampling later is a config knob, not a protocol change.

**中文.** 运行时采用 W3C Trace Context v1 线格式。`trace.py` 提供基于 `traceparent`
（`00-{trace-id}-{span-id}-{flags}`，32 位十六进制 trace id / 16 位 span id，采样
标志）的 parse/format/child/root 原语，以及 `new_root`/`child` span 派生。gateway 的
`TraceMiddleware` 解析入站 `traceparent`（缺失则新建采样根），开一个子 span 并装入关联
作用域（D2），使请求期间的每条日志都携带 trace/span id。回写的 span id 是 gateway 自身的
（线格式没有 parent-span 字段）；`traceresponse` 头把派生上下文回显给调用方。
`outbound_traceparent()` 把当前活动 span 交给出站调用方，使下游一跳留在同一 trace。
追踪常开且廉价（每请求一次 parse + 两次 contextvar 写）；采样策略推迟——标志默认 `01`，
将来开启概率采样只是一个配置旋钮，不涉及协议变更。

---

## Testing strategy

## 测试策略

- **Unit**: `tests/unit/test_metrics.py` (registry semantics, label escaping,
  histogram/`le` spelling, counter/gauge 不变量), `tests/unit/test_trace.py`
  (parse/reject/round-trip/child propagation).
- **Integration**: `tests/integration/test_observability.py` — real `httpx`
  against a running app asserts `/metrics` returns valid `version=0.0.4` text
  with labelled families, and that a caller-supplied `traceparent` is echoed via
  `traceresponse` with a fresh child span id.
- **Benchmark** (`tests/benchmark/test_observability_bench.py`, real
  measurement via the runner, ADR-0008): `/metrics` render throughput has an
  honest floor (≥1k renders/s in this environment; measured well above).
  Baseline numbers are recorded in the session-log, not transcribed/vended.

## 中文

- **单元**：`tests/unit/test_metrics.py`（注册表语义、label 转义、histogram/`le`
  拼写、counter/gauge 不变量）、`tests/unit/test_trace.py`
  （parse/reject/round-trip/child 传播）。
- **集成**：`tests/integration/test_observability.py` ——用真实 `httpx` 打一个运行中
  app，断言 `/metrics` 返回合法 `version=0.0.4` 文本且含带标签的 metric 族；断言调用方
  给的 `traceparent` 经 `traceresponse` 回显且带全新子 span id。
- **基准**（`tests/benchmark/test_observability_bench.py`，经 runner 真实测量，
  ADR-0008）：`/metrics` 渲染吞吐有一个诚实下限（本环境 ≥1k 渲染/秒；实测显著超过）。
  基线数字记录在 session-log，不转述/编造。

## Conflict check with the architecture doc

## 与架构文档的冲突检查

No conflict. Observability is a horizontal seam beneath all five layers: the
collector is wired at the composition root and passed down, `/metrics` lives on
the existing Gateway face, and tracing rides the same ASGI request path already
described in §5. New files live under `src/whirlwind/observability/`, mirroring
the DRY/interface conventions of the rest of the tree and the platform
abstraction rules — no `sys.platform` branches were introduced.

无冲突。可观测性是横切五层之下的水平接缝：collector 在组合根装配并下传，`/metrics`
落在既有 Gateway 接入面，追踪复用 §5 已描述的同一 ASGI 请求路径。新文件放在
`src/whirlwind/observability/`，遵循全树一致的接口/DRY 约定与平台抽象规则——未引入
任何 `sys.platform` 分支。

## Implementation order & risks

## 实施顺序与风险

Implemented in four self-contained loops (each committed separately):
D1 metrics core → D2 structured logging → D3 wiring seam (middleware + collection
points + `/metrics`) → D4 trace context. Open points: probabilistic sampling
policy (config knob), a richer metric set (scheduler/cron/pool), and an exporter
tail to ship /metrics off-host — none blocked by this ADR.

按四个自闭环分步实施（各自单独提交）：D1 指标核心 → D2 结构化日志 → D3 接线接缝
（中间件 + 采集点 + `/metrics`）→ D4 追踪上下文。开放点：概率采样策略（配置旋钮）、更
丰富的指标集（scheduler/cron/pool）、以及把 /metrics 送出主机的 exporter tail——均不
被本 ADR 阻碍。