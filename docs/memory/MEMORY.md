# MEMORY

> 项目记忆快照（精简、只存结论与指针；详细内容见 ADR / session-logs）。每次 session 结束时增量更新。

## 快照

- 系统形态：harness 无感的沙箱化 agent 运行时；单进程 all-in-one（M1 竖切）→ 多 substrate 演进中。
- 里程碑：M1（单进程竖切）、M2（全生命周期）已完成；M3 部分（runsc / 传输 / WAL EventLog）；P0（成熟库 + PG/Redis provider + 统一配置）已完成；P1 除 auth/tenancy 外完成（资源限制、配额、幂等键、k3s、agent env 密钥 ADR-0010、seam 模板/实例 + harness 组合 ADR-0011、delta 快照 + 底座钉选 ADR-0012）；平台抽象（ADR-0007）与测试运行器（ADR-0008）已落地；**microsandbox 第三底座已落地并在 Apple Silicon 上完成真实 VM 生命周期验证（ADR-0006 Implemented + M4 verified，2026-08-20：msb CLI 封装 + 装配 + 门控测试 + benchmark，HVF 真跑 19/19）**；顶层文档已拆分为 EN / zh-CN 互链（AGENTS.md §5.5，2026-08-20）。详见 [docs/TODO.md](../TODO.md)。
- 测试基线（2026-08-20，Linux/x86_64 容器 + 本机 PG/Redis + runsc/msb 实装，经 runner）：`not e2e` 全量 **347 passed / 11 skipped**（runsc 真跑 ×8；microsandbox 渲染/装配 ×17 真跑；剩余 skip = k8s×4、vsock×1、rootless restore×1、e2e×3、msb VM×2（KVM ENODEV），均为环境事实）。冷启动优化后 p50：无限制 183ms / 含 rlimits 197ms（250ms 线内，详见 session-log 2026-08-20）。**macOS 基线（2026-08-20，Apple Silicon + msb 0.6.8 HVF 真跑）：336 passed / 26 skipped**（skip = 无本地 PG×9、k3s×4、runsc×4、vsock×1、RLIMIT_AS macOS 诚实事实×1、e2e×3 等；microsandbox 生命周期×3 + benchmark×3 全部真跑通过）。
- 懒加载原则（用户约定）：只对「该路径确实不需要」的可选/误伤导入惰性化（如沙箱子进程的 pydantic、echo 的 urllib）；业务必需的加载（agent.server 的 asyncio 等）一律保持急切。
- 统一配置（ADR-0009，AGENTS.md §3.5）：默认值只在 `config.py` loader 定义一次；优先级 代码默认 < `whirlwind.toml` < `WHIRLWIND_*` env < CLI（None 哨兵）；`whirlwind config show` 自省；密钥值永不进配置文件（只配置 env 变量名）；k3s 经 ConfigMap 挂载 TOML。新增可调值必须登记 loader schema + env 映射。
- Agent env 密钥（ADR-0010）：名字进 `AgentVersion.env_secrets`、值以 pynacl 信封（`v1:<key_id>:<b64>`）进 `SecretStore`（本地默认后端 0600 JSON）；API 对值只写；供给期 fail-closed 解密注入（优先级 bundle < secrets < prepared）；保留名（`WHIRLWIND_*`、`DEEPSEEK_API_KEY`）拒收。主密钥 `WHIRLWIND_SECRET_KEY` env（开发兜底 `data_dir/secret.key`）；密钥轮转未实现（信封 `v1` 前缀即接缝）。
- Seam 模板/实例 + Harness 组合（ADR-0011，2026-08-20 落地）：`SeamTemplate`（`${param}` 占位符，注册期对 renderer registry 校验）经命名 `SeamInstance` 物化为具体 decl（ConfigMap 活性：供给期解析，非创建期冻结）；`HarnessBundle` 一等镜像组合（内建 echo/dsh 兜底，store 文档可遮蔽）；`AgentVersion` 绑 0..1 bundle + 0..N 实例，内联声明兼容；网关急切准入（未知引用 4xx、显式值不一致 422、拒绝不留孤儿 agent）+ Hostlet 供给期二次解析（fail-closed）；env 优先级 image < harness-bundle < secrets < prepared；通用 catalog 接缝（MetadataStore 四元组，memory/PG 对齐）。
- Delta 快照 + 底座钉选（ADR-0012，2026-08-20 落地）：`Caps.delta_snapshots` 能力位（process=True，runsc 诚实 False，`checkpoint(base=...)` 前置拒绝）；overlay 工件（`WHIRLWIND_DELTA.json` 索引；merkle=物化端态，全量/delta 同态同根；逐跳链校验 fail-closed）；hostlet 血缘策略 `sandbox.snapshot_mode`（默认 full，逐字节兼容）+ `snapshot_chain_max` 压实；播种统一走 `driver.materialize`（链重建只在产出 driver 一处）；`sandbox.driver` 配置钉选（指名不可用即拒绝启动，绝不静默回退）。实测：稀疏负载 delta 载荷 ~50x 缩小；链深同时抬高 checkpoint/物化成本（delta 需先物化 base 链再 diff）——`chain_max` 一次界定三端成本；块索引优化是已知后续路径（ADR-0012 风险区）。

## 进行中

- P1.1 Gateway API-key 认证（待租户维度）与 P1.6（per-tenant 限流/配额）未启动。
- P2 可观测性（/metrics、结构化日志、OTLP tracing）未启动。
- P3.5 microsandbox：驱动/装配/门控测试已落地，**真实 VM 生命周期 + benchmark 已在 Apple Silicon mac（HVF）上验证（2026-08-20，19/19）**；`snapshot_full` 在真实 restore 通过前保持 False；Linux-KVM 路径待有 KVM 的宿主执行（本 mac/容器均无）。
- 2026-08-19：生成 AGENTS.md，建立 ADR 先行 / session-log / 项目记忆规范（本文件即首批载体）。

## 环境事实

- 双平台：macOS（M 系列）+ Linux；Linux 容器（Ubuntu 24.04，无 CAP_SYS_ADMIN）已实装 runsc `release-20260817.0` + 静态 busybox 1.35.0（`scripts/setup/install-runsc.sh`，分段并行下载 + sha512 校验）→ rootless + `--network=none` 模式，restore 用例按上游限制 skip。
- microsandbox：Linux 容器（msb 0.6.12）`/dev/kvm` 节点存在但 open(2) 返回 ENODEV（宿主 kvm 模块未加载，mknod 无解；`msb doctor` 诚实报告 "KVM access unavailable"；libkrun 无 TCG 回退）→ VM 生命周期测试在该容器诚实 skip。**macOS（Apple Silicon）msb 0.6.8（`~/.local/bin/msb` → `~/.microsandbox/bin/msb`）HVF 后端正常，2026-08-20 已真跑通过生命周期套件 + benchmark**。实测要点（详见 ADR-0006 验证记录 / session-log 2026-08-20-microsandbox-mac-verification.md）：(1) macOS `/tmp` 是指向 `/private/tmp` 的符号链接，msb VM 以 follow_root_symlinks=false 打开镜像 → bundle_root 必须 resolve()（ENOTDIR 陷阱）；(2) msb 二进制的符号链接会影响 libkrunfw 的 binary-relative 查找 → 驱动把二进制 resolve()；(3) **`MSB_HOME` 不得重定向到空目录**（libkrunfw 也在 `MSB_HOME/lib` 查找）；TRAE 沙箱拦截 `~/.microsandbox` store 写入，故本地验证用 `MSB_HOME=/tmp/whirlwind-msb-home` + 解析后的二进制路径；(4) `msb run --detach` 返回即 guest 就绪（create 后首次 exec 即稳态延迟），无需就绪等待；(5) 不带 `--replace` 的 `msb run` 对已存在名称静默复用旧 VM（创建标志被忽略）——驱动现在恒传 `--replace` 防 spec 欺骗。benchmark（HVF 真跑）：冷启动 p50=116ms、exec p50=11ms、DATA checkpoint p50=1ms。guest 载荷用本地 docker 导出的 busybox rootfs（`rancher/mirrored-library-busybox:1.36.1`，tar extractall 需 filter="fully_trusted"——默认 "data" filter 会拒绝 /etc/mtab 等绝对符号链接）。本容器亦无 `/dev/vsock`。
- 平台判断统一走 `core/platform`（ADR-0007）：`current_facts()` 取事实，`@platform_impl`/`resolve_impl` 分发行为插件；`WHIRLWIND_PLATFORM` 仅模拟身份（探测类事实永不被覆盖），生产不设置。
- 测试一律经 `uv run python scripts/run_tests.py ...`（ADR-0008）：完整输出在 `.test-logs/`（gitignore），控制台仅 verdict + 失败摘要，退出码透传 pytest。
- PostgreSQL / Redis 为 extras（`whirlwind[postgres]` / `whirlwind[redis]`）；集成测试经 conftest 探测，不可达即 skip。
- dsh 两个 Python 包（sdk / sdk-runtime）不在 PyPI；镜像构建从本地 checkout 安装（`refs/deepseek-harness`，`WHIRLWIND_DSH_REPO` 可覆盖）。
- E2E 需 `WHIRLWIND_E2E=1` + 真实 DeepSeek API key。
- **已核实（2026-08，联网）**：runsc 可在 colima 的 Linux Docker VM 内作为 Docker runtime 安装（`runsc install` + daemon.json，宿主内核 ≥ 4.14.77）；macOS 经 `colima start --kubernetes` 支持 k3s（此前"不支持"是 headless sandbox 执行环境的假象）；colima `--vm-type krunkit`（libkrun）是 Apple Silicon 一等的 VM 后端——microsandbox（ADR-0006）选用的正是它；dsh 是公开 MIT 仓库 `github.com/deepseek-ai/deepseek-harness`。

## 坑与注意

- runsc 受限容器（无 `CAP_SYS_ADMIN`）自动降级 rootless + `--network=none`；rootless 模式不支持 restore（runsc 上游限制）。
- **busybox applet 陷阱**：bundle rootfs 只有 busybox 多调用二进制、无 applet 符号链接——`busybox sh -c "sleep 300"` 会因找不到独立 `sleep` 而 exit 127、容器 stopped；OCI init 必须用直接 applet 调用（`busybox sleep 300`）。长期 skip 的测试在装上真二进制后会暴露这类假设（2026-08-20 实录）。
- 沙箱内 `DEEPSEEK_API_KEY` 只是占位符 `whirlwind-relay`；真实凭证仅在 Hostlet SecretRelay，出网时替换 Authorization 头。
- README / 架构文档已拆分 EN 与 zh-CN 两份（互链切换）；更新内容必须同步两份（AGENTS.md §5.5）。引用性能数字必须注明来源（ADR / 实测）。
- **环境重置坑（2026-08-20 实录）**：容器重建后 runsc/busybox/PG 角色全丢；PG 的 `whirlwind_test` 库为镜像预置（owner=postgres），存在性检查会跳过 `createdb -O` → PG15+ public schema 权限拒绝（恢复命令：`ALTER DATABASE whirlwind_test OWNER TO whirlwind` + `GRANT ALL ON SCHEMA public TO whirlwind`）。zsh 无 `/dev/tcp` 重定向——TCP 探测用 `pg_isready`/`redis-cli ping`。
- **GitHub token 无 `workflow` scope（2026-08-20 实录）**：当前 `github_token` 对 `Woody-Hu/whirlwind` 有 admin/push 权限，但**不能推送含 `.github/workflows/**` 的提交**（"PAT requires workflow scope"，GitHub 硬限制）。所以 CI workflow 暂不可落库。M4 真机验证改走 `scripts/setup/setup-kvm-linux.sh` 本地路径；用户若想恢复 CI，需要提供含 workflow scope 的新 token。
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
| Seam 模板/实例 + Harness 组合 + Agent 绑定模型 + 通用 catalog 接缝 | 0011 |
| Delta 快照（overlay 工件 + 链压实）+ `sandbox.driver` 底座钉选 | 0012 |
