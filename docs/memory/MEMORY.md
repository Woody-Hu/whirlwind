# MEMORY

> 项目记忆快照（精简、只存结论与指针；详细内容见 ADR / session-logs）。每次 session 结束时增量更新。

## 快照

- 系统形态：harness 无感的沙箱化 agent 运行时；单进程 all-in-one（M1 竖切）→ 多 substrate 演进中。
- 里程碑：M1（单进程竖切）、M2（全生命周期）已完成；M3 部分（runsc / 传输 / WAL EventLog）；P0（成熟库 + PG/Redis provider）已完成；P1 除 auth 外完成（资源限制、配额、幂等键、k3s）。详见 [docs/TODO.md](../TODO.md)。
- 测试基线参考（历史计数）：`not e2e` 全量曾达 139 passed / 5 skipped（Argus 时代，原样保留）；以实际运行为准。

## 进行中

- P1.1 Gateway API-key 认证（待租户维度）与 P1.6（per-tenant 限流/配额）未启动。
- P2 可观测性（/metrics、结构化日志、OTLP tracing）未启动。
- Microsandbox（ADR-0006，`Isolation.LIGHT_VM`，libkrun/krunkit）：**设计已定稿 @ Accepted，文档已落盘；代码实施推迟到有真机的 session**（TODO P3.5，M0 spike 需真机探测 krunkit CLI）。
- 2026-08-19：生成 AGENTS.md，建立 ADR 先行 / session-log / 项目记忆规范（本文件即首批载体）。

## 环境事实

- 双平台：macOS（M 系列）+ Linux；`runsc` 仅 Linux，无二进制则相关测试 skip（不 stub）。
- PostgreSQL / Redis 为 extras（`whirlwind[postgres]` / `whirlwind[redis]`）；集成测试经 conftest 探测，不可达即 skip。
- dsh 两个 Python 包（sdk / sdk-runtime）不在 PyPI；镜像构建从本地 checkout 安装（`refs/deepseek-harness`，`WHIRLWIND_DSH_REPO` 可覆盖）。
- E2E 需 `WHIRLWIND_E2E=1` + 真实 DeepSeek API key。
- **已核实（2026-08，联网）**：runsc 可在 colima 的 Linux Docker VM 内作为 Docker runtime 安装（`runsc install` + daemon.json，宿主内核 ≥ 4.14.77）；macOS 经 `colima start --kubernetes` 支持 k3s（此前"不支持"是 headless sandbox 执行环境的假象）；colima `--vm-type krunkit`（libkrun）是 Apple Silicon 一等的 VM 后端——microsandbox（ADR-0006）选用的正是它；dsh 是公开 MIT 仓库 `github.com/deepseek-ai/deepseek-harness`。

## 坑与注意

- runsc 受限容器（无 `CAP_SYS_ADMIN`）自动降级 rootless + `--network=none`；rootless 模式不支持 restore（runsc 上游限制）。
- 沙箱内 `DEEPSEEK_API_KEY` 只是占位符 `whirlwind-relay`；真实凭证仅在 Hostlet SecretRelay，出网时替换 Authorization 头。
- README 中的测试计数是历史数据；引用性能数字必须注明来源（ADR / 实测）。
- driver 的 Caps/Resources 必须如实上报（声明即执行）；已知无法诚实保证的维度不声明或在 docstring 写明 caveat。

## 决策索引

| 主题 | ADR |
| --- | --- |
| M1/M2 竖切、process driver 默认、dsh stdio JSON-RPC、Sidecar 落位、Seam 三层、镜像库、MCP、CLI、时间轮、provider | 0001 |
| runsc driver、vsock 传输、durable WAL EventLog | 0002 |
| cron→croniter、YAML→PyYAML | 0003 |
| PostgreSQL MetadataStore、Redis KV/Locks、后端选择 | 0004 |
| 沙箱资源限制、会话配额、幂等键、k3s 部署 | 0005 |
| microsandbox（libkrun/krunkit）第三 VM 底座、mac 本地可测 | 0006 |
