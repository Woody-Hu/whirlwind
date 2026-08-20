# Session: 可观测性闭环——metrics / 结构化日志 / W3C trace（2026-08-20）

## 目标

用户任务清单第 1 项：「根据 todo 文档寻找接下来建议开发的事情」，按此前沟通选定 **P2 可观测性闭环**。目标是把生产级可观测性的最小诚实集落地：面向 Prometheus 的 `/metrics` 指标面、机器可解析的关联结构化日志、以及 W3C Trace Context 传播；全程不引入厂商锁死、不伪造数字。同时按用户清单第 4/6/7 项，把真实后端依赖（Redis/PostgreSQL）装上并纳入契约测试与 benchmark，且同步维护 ADR / session-log / TODO / MEMORY。

## 前置

- 阅读 ADR-0009（统一配置，`[logging]` 分区在此扩展）、ADR-0008（测试 runner 日志落盘先例）、ADR-0007（平台抽象）、架构文档 Gateway 接入层；
- 盘点现有可观测状态：纯文本日志、无指标、无追踪；
- 确认新增依赖最小化原则（§3.3）：指标注册表自研、追踪自研，零新增 runtime 依赖。

## 变更清单

按四个自闭环 loop 分步提交（各一次 commit）：
1. **Loop 1 — D1 指标核心**：`src/whirlwind/observability/metrics.py`（stdlib-only Counter/Gauge/Histogram + prometheus text v0.0.4 渲染，`threading.Lock` 保护 + label 转义 + `le` 桶浮点拼写保留）+ `collector.py` 骨架 + `tests/unit/test_metrics.py`。
2. **Loop 2 — D2 结构化日志**：`src/whirlwind/observability/logconfig.py`（单行 JSON formatter + `contextvar` 关联作用域 `request_id`/`trace_id`/`span_id` + Filter 拼接）+ `config.py` 增 `[logging]` 分区（`level`/`format`∈{json,text}/`environment`）+ env 覆盖 `WHIRLWIND_LOG_*` + ADR-0009 schema 块补登记 + 测试。
3. **Loop 3 — D3 接线接缝**：`MetricsCollector`（采样型 gauge 走异步事实源 sampler：sessions-by-status 从 `MetadataStore`、sandbox 从 `hostlet.population`；事件型 counter：manager turn 延迟、WAL `on_append`、HTTP 延迟）+ `MetricsMiddleware`（纯 ASGI，流式 SSE 无缓冲直通，route 标签按 router 约束基数）+ 路由 `GET /metrics` 返回 `PlainTextResponse(text/plain; version=0.0.4)` + `tests/integration/test_observability.py` + `tests/benchmark/test_observability_bench.py`。
4. **Loop 4 — D4 W3C Trace Context**：`src/whirlwind/observability/trace.py`（`traceparent` parse/format/child/root 原语）+ `TraceMiddleware`（解析入站 traceparent/无则新采样根 → 开子 span → 装入关联作用域 → `traceresponse` 回显）+ `outbound_traceparent()` 出站传播 + `tests/unit/test_trace.py` + 集成断言。
5. **Loop 5 — 真实后端依赖**：Ubuntu 24.04 上 apt 安装 redis-server（启动）+ PostgreSQL 16（启动）；`scripts/setup/provision-test-db.sh`（新增，幂等装配 conftest 默认 DSN 需要的 `whirlwind` 角色 + `whirlwind_test` 库 + redis ping 自验，修掉 install-postgres.sh 只建库不建角色导致默认 DSN 认证失败的缺口）+ `scripts/setup/README.md` env 名修正（`WHIRLWIND_POSTGRES_DSN` → 补充 `WHIRLWIND_TEST_POSTGRES_DSN`）。
6. **Loop 6 — 文档**：ADR-0013（新增，Accepted）+ ADR-0009 schema 补 `[logging]` + TODO P2 勾选 + MEMORY + AGENTS 索引。

## 关键决策与发现

1. **指标注册表自研 vs `prometheus_client`**：暴露格式是「每序列一行」的小而明确的文本格式，手写让热 `/metrics` 路径廉价、依赖面持平（ADR-0003 先查成熟库的立场，客户端被权衡后仅就暴露面否决，不是一刀切零依赖）。`le` 桶保留浮点拼写（`1.0` 仍是 `1.0` 匹配官方客户端惯例），其它数值去尾零——单测钉死。
2. **采样型 gauge vs 事件型 counter 的诚实分界**：gauge 是"跨重启必须自愈"的状态，从事实源**拉**（sessions 由 `MetadataStore`，异步 sampler 在 `render_async` 先跑）；counter/histogram 是"事件数"在调用点**推**。这避免增量 delta 在重启后漂移。
3. **指标渲染下限**：初版断言 20k renders/s 拍高，实测约 2.8k——把下限调到诚实的 1k renders/s（`MIN_RENDERS_PER_SEC`），数字写进 ADR 测试策略，不挑帧不编造。
4. **纯 ASGI 中间件而非 `BaseHTTPMiddleware`**：流式响应（SSE `/sessions/{id}/stream`）不被 head-of-stream 缓冲；状态码从 `http.response.start` ASGI 消息捕获，抛异常的 handler 也记录它最终返回的 500。route 标签按 router 匹配约束基数（`/sessions/{id}/turns` 而非具体 id），未知路径回退原始路径。
5. **traceparent 无线格式无 parent-span 字段**：回写给调用方的是 gateway 自身的 span id；`outbound_traceparent()` 携带当前活动 span，让下游一跳停留同一 trace。测试最初误断言父 span 存在，改为断言同 trace、新子 span 后再修正。
6. **真实后端缺口**：conftest 默认 DSN 是 `postgresql://whirlwind:whirlwind@.../whirlwind_test`，但 install-postgres.sh 只 `createdb`（owner=postgres）→ 按脚本安装后默认 DSN 认证失败、测试如实 skip。`provision-test-db.sh` 补齐这条 wiring（幂等，跑两遍无副作用）。

## 验证证据

- `tests/unit/test_metrics.py` + `tests/unit/test_trace.py` 全绿（registry 语义、`le` 拼写、label 转义、trace parse/reject/round-trip/child）。
- `tests/integration/test_observability.py` 全绿：真实 `httpx` 打 running app，`/metrics` 返回合法 `version=0.0.4` 文本带标签族；调用方 `traceparent` 经 `traceresponse` 回显且带全新子 span。
- `tests/benchmark/test_observability_bench.py`（真实测量）：**`/metrics` render：2,870 renders/s（134 样本/次）；`render_async`（含 sampler）：2,839 renders/s**，达到诚实下限 1k。
- 后端实装后：`tests/integration/test_storage.py` **33 passed**（memory + postgres 双后端契约一致）；`tests/benchmark/test_storage_bench.py` **8 passed**——实测 memory ~113k ops/s、Redis CAS ~9.3k ops/s、PG session-update ~1.8k ops/s（对比基线存 log，数字来自真实运行）。
- 全量回归（经 runner，Linux/x86_64 容器 + 本机 PG/Redis）：`PASS · 390 passed · 18 skipped · exit=0`。18 skip 均为环境事实：microsandbox VM×（KVM ENODEV/rootfs）、runsc rootless restore×、`/dev/vsock`×、live k3s×、e2e×——真实依赖不可达，诚实跳过（禁 mock 铁律，不伪造）。
- `scripts/setup/provision-test-db.sh` 实跑两遍：`pg-conn-ok` + `PONG`，幂等确认。

## 遗留与 handoff

- P2.3 的 **OTLP 导出**（架构 6.1 M3）仍未做——需目标环境有 collector 才值得；采样策略已预留为配置旋钮（当前 traceparent 标志默认 `01`）。
- 开放点：更丰富的指标集（scheduler/cron/pool）、把 /metrics 送出主机的 exporter tail、概率采样策略。
- ADR-0009 schema 的 `[logging]` 已登记（level/format/environment），后续可调值按同一规则追加。
- 本容器环境：redis + PostgreSQL 16 已启动并接入测试；若容器重建，PG 角色/库会丢，可重跑 `provision-test-db.sh` 恢复（坑见 MEMORY 环境事实）。