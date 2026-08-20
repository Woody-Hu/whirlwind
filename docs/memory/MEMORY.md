# MEMORY

> 项目记忆快照（精简、只存结论与指针；详细内容见 ADR / session-logs）。每次 session 结束时增量更新。

## 快照

- 系统形态：harness 无感的沙箱化 agent 运行时；单进程 all-in-one（M1 竖切）→ 多 substrate 演进中。
- 里程碑：M1（单进程竖切）、M2（全生命周期）已完成；M3 部分（runsc / 传输 / WAL EventLog）；P0（成熟库 + PG/Redis provider + 统一配置）已完成；P1 除 auth/tenancy 外完成（资源限制、配额、幂等键、k3s、agent env 密钥 ADR-0010）；平台抽象（ADR-0007）与测试运行器（ADR-0008）已落地；顶层文档已拆分为 EN / zh-CN 互链（AGENTS.md §5.5，2026-08-20）。详见 [docs/TODO.md](../TODO.md)。
- 测试基线（2026-08-20，Linux/x86_64 容器 + 本机 PG/Redis，经 runner）：`not e2e` 全量 **280 passed / 12 skipped**（skip = runsc×4、k8s×4、vsock×1、e2e×3，容器无二进制/设备）。冷启动优化后 p50：无限制 183ms / 含 rlimits 197ms（250ms 线内，详见 session-log 2026-08-20）。macOS 基线（2026-08-19）：202 passed / 24 skipped。
- 懒加载原则（用户约定）：只对「该路径确实不需要」的可选/误伤导入惰性化（如沙箱子进程的 pydantic、echo 的 urllib）；业务必需的加载（agent.server 的 asyncio 等）一律保持急切。
- 统一配置（ADR-0009，AGENTS.md §3.5）：默认值只在 `config.py` loader 定义一次；优先级 代码默认 < `whirlwind.toml` < `WHIRLWIND_*` env < CLI（None 哨兵）；`whirlwind config show` 自省；密钥值永不进配置文件（只配置 env 变量名）；k3s 经 ConfigMap 挂载 TOML。新增可调值必须登记 loader schema + env 映射。
- Agent env 密钥（ADR-0010）：名字进 `AgentVersion.env_secrets`、值以 pynacl 信封（`v1:<key_id>:<b64>`）进 `SecretStore`（本地默认后端 0600 JSON）；API 对值只写；供给期 fail-closed 解密注入（优先级 bundle < secrets < prepared）；保留名（`WHIRLWIND_*`、`DEEPSEEK_API_KEY`）拒收。主密钥 `WHIRLWIND_SECRET_KEY` env（开发兜底 `data_dir/secret.key`）；密钥轮转未实现（信封 `v1` 前缀即接缝）。

## 进行中

- P1.1 Gateway API-key 认证（待租户维度）与 P1.6（per-tenant 限流/配额）未启动。
- P2 可观测性（/metrics、结构化日志、OTLP tracing）未启动。
- Microsandbox（ADR-0006，`Isolation.LIGHT_VM`，libkrun/krunkit）：**设计已定稿 @ Accepted，文档已落盘；代码实施推迟到有真机的 session**（TODO P3.5，M0 spike 需真机探测 krunkit CLI）。
- 2026-08-19：生成 AGENTS.md，建立 ADR 先行 / session-log / 项目记忆规范（本文件即首批载体）。

## 环境事实

- 双平台：macOS（M 系列）+ Linux；`runsc` 仅 Linux，无二进制则相关测试 skip（不 stub）。
- 平台判断统一走 `core/platform`（ADR-0007）：`current_facts()` 取事实，`@platform_impl`/`resolve_impl` 分发行为插件；`WHIRLWIND_PLATFORM` 仅模拟身份（探测类事实永不被覆盖），生产不设置。
- 测试一律经 `uv run python scripts/run_tests.py ...`（ADR-0008）：完整输出在 `.test-logs/`（gitignore），控制台仅 verdict + 失败摘要，退出码透传 pytest。
- PostgreSQL / Redis 为 extras（`whirlwind[postgres]` / `whirlwind[redis]`）；集成测试经 conftest 探测，不可达即 skip。
- dsh 两个 Python 包（sdk / sdk-runtime）不在 PyPI；镜像构建从本地 checkout 安装（`refs/deepseek-harness`，`WHIRLWIND_DSH_REPO` 可覆盖）。
- E2E 需 `WHIRLWIND_E2E=1` + 真实 DeepSeek API key。
- **已核实（2026-08，联网）**：runsc 可在 colima 的 Linux Docker VM 内作为 Docker runtime 安装（`runsc install` + daemon.json，宿主内核 ≥ 4.14.77）；macOS 经 `colima start --kubernetes` 支持 k3s（此前"不支持"是 headless sandbox 执行环境的假象）；colima `--vm-type krunkit`（libkrun）是 Apple Silicon 一等的 VM 后端——microsandbox（ADR-0006）选用的正是它；dsh 是公开 MIT 仓库 `github.com/deepseek-ai/deepseek-harness`。

## 坑与注意

- runsc 受限容器（无 `CAP_SYS_ADMIN`）自动降级 rootless + `--network=none`；rootless 模式不支持 restore（runsc 上游限制）。
- 沙箱内 `DEEPSEEK_API_KEY` 只是占位符 `whirlwind-relay`；真实凭证仅在 Hostlet SecretRelay，出网时替换 Authorization 头。
- README / 架构文档已拆分 EN 与 zh-CN 两份（互链切换）；更新内容必须同步两份（AGENTS.md §5.5）。引用性能数字必须注明来源（ADR / 实测）。
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
| 平台抽象：PlatformFacts / WHIRLWIND_PLATFORM / @platform_impl 插件 | 0007 |
| 测试执行日志：runner 落盘 + junit 摘要 + 控制台简要结论 | 0008 |
| 统一配置：whirlwind.toml 分层注入（file < env < CLI）、schema 强校验、config show | 0009 |
| Agent env 密钥：引用/值分离、pynacl 信封、供给期 fail-closed 注入 | 0010 |
