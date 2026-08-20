# AGENTS.md — Whirlwind Agent Runtime 工程协作指南

> 本文档面向在本仓库工作的 AI coding agent 与人类工程师，约定架构认知、目录索引、开发/测试/文档规范与长上下文协作（handoff）方式。
> 事实性架构描述以 [agent-runtime-architecture.md](agent-runtime-architecture.md)（v0.6 架构草案）与 [docs/adr/](docs/adr/) 为准；本文档是操作层规范，冲突时以 ADR 为最高事实源。

---

## 1. 项目定位与整体架构

### 1.1 解决的问题

Whirlwind 是一个 **harness 无感的沙箱化智能体运行时**。agent 类负载有三个先天特征，决定了本系统的形态：

1. **负载高度突发**——绝大多数时间在等待输入或工具结果，真正执行时间占比极低；
2. **执行体不可信**——agent 运行模型生成的代码，必须隔离在沙箱中，导致单租户、海量实例；
3. **harness 生态碎片化**——各框架的循环、工具、会话模型互不兼容，绑定任何一家 API 都会被锁死。

**立足点：平台只与「沙箱 + 事件 + 能力契约」打交道，永远不与具体 harness 的内部 API 打交道。** 任意 harness（DeepSeek Harness / dsh、echo、自研 loop）作为黑盒进程装进受管沙箱，平台统一负责会话路由、沙箱调度、快照恢复、池化预热、事件流与能力注入。

### 1.2 五层架构

```
Gateway (REST + SSE + MCP)          接入层：会话 / 事件流 / cron / 镜像管理
  └─ Control (SessionManager / Scheduler / WarmPool / Lifecycle)   控制面
       └─ Hostlet (ensure / bind / turn / pause / destroy + SecretRelay)   节点代理
            └─ SandboxDriver ──→ 沙箱（gVisor / 进程组）
                 └─ SandboxAgent (EventTap / ControlAgent / ResourceInjector / LLM Relay)
                      └─ Harness (dsh / echo / ...)
```

- 上层只认 `AgentSession`（全系统唯一调度单元），下层只认 `Sandbox`（物理执行单元），两者由控制面经 KV 路由表动态绑定翻译。
- 沙箱**无入站请求面**；出站（LLM 调用等）统一经 Hostlet 的 SecretRelay 代理，**密钥永不进沙箱**（沙箱内只有占位符 `DEEPSEEK_API_KEY=whirlwind-relay`）。
- driver 能力位（`Caps`）**如实上报**，调度只认能力位——声明即执行，不强于声明。

### 1.3 演进状态

- **已落地**：M1 单进程竖切（process driver、echo/dsh adapter、Seam renderer、REST+SSE+MCP、CLI）；M2 全生命周期（suspend/resume、warm 池 CAS、时间轮 cron）；M3 部分（runsc driver 真实 gVisor 全生命周期验证、TCP/UDS/vsock 传输、durable WAL EventLog）；P0（croniter/PyYAML 成熟库、PostgreSQL/Redis provider）；P1 除 auth 外（资源限制、会话配额、幂等键、k3s 部署）；平台抽象（ADR-0007：PlatformFacts + platform_impl 行为插件）与测试运行器（ADR-0008：日志落盘 + 控制台简要结论）。
- **已定稿待实施**：microsandbox（libkrun/krunkit）第三 VM 底座（[ADR-0006](docs/adr/0006-microsandbox-driver.md)，`Isolation.LIGHT_VM`）——填补 Apple Silicon 的 VM 级测试覆盖空洞。
- **进行中/待办**：见 [docs/TODO.md](docs/TODO.md)（活文档，随每个自闭环变更增量维护）。

---

## 2. 目录索引

```
whirlwind/
├── AGENTS.md                        # 本文档：工程协作规范
├── README.md                        # 项目简介 / 快速上手（英文版，zh-CN 版互链，见 §5.5）
├── README.zh-CN.md                  # 项目简介 / 快速上手（中文版）
├── agent-runtime-architecture.md    # 架构设计 v0.6（英文版，事实源）
├── agent-runtime-architecture.zh-CN.md  # 架构设计 v0.6（中文版，与英文版互链）
├── pyproject.toml                   # 项目元数据；pytest 配置；extras: [postgres] [redis]
├── docs/
│   ├── adr/                         # 架构决策记录（开发前必读/必写，见 §5）
│   │   ├── 0001-agent-runtime-m1.md     # M1/M2 竖切详设：D1~D10（语言/process driver/dsh 协议/Sidecar/Seam/镜像库/MCP/CLI/时间轮/provider）
│   │   ├── 0002-m3-substrates.md        # M3 底座：runsc driver、vsock 传输、durable WAL EventLog
│   │   ├── 0003-mature-libs.md          # cron→croniter、YAML→PyYAML 成熟库替换
│   │   ├── 0004-production-storage.md   # PostgreSQL MetadataStore、Redis KV/Locks、后端选择
│   │   ├── 0005-edge-hardening.md       # 沙箱资源限制、会话配额、幂等键、k3s 部署
│   │   ├── 0006-microsandbox-driver.md  # libkrun/krunkit 第三 VM 底座（定稿待实施）
│   │   ├── 0007-platform-abstraction.md # 平台抽象：PlatformFacts + WHIRLWIND_PLATFORM + 行为插件
│   │   ├── 0008-test-logging.md         # 测试执行日志：runner 落盘 + junit 摘要 + 简要结论
│   │   ├── 0009-unified-config.md       # 统一配置：单一 TOML + 分层注入（file < env < CLI）
│   │   └── 0010-agent-env-secrets.md    # Agent env 密钥：引用/值分离、pynacl 信封、供给期注入
│   ├── TODO.md                      # 演进路线活文档（P0~P4 优先级分层）
│   ├── session-logs/                # 开发 session 记录（见 §5.3，按日期归档）
│   └── memory/                      # 项目记忆（见 §6，长上下文 handoff 载体）
├── src/whirlwind/
│   ├── core/                        # 领域层：模型 / 状态机 / 事件 / 错误 / id / 平台事实
│   │   ├── model.py                     # AgentDefinition / AgentVersion / AgentSession / Sandbox / Snapshot / CronJob（pydantic）
│   │   ├── statemachine.py              # SESSION/SANDBOX 转移表 + check_transition
│   │   ├── events.py                    # SessionEvent / Surface
│   │   ├── errors.py                    # WhirlwindError 基类（code 字段约定）
│   │   └── platform.py                  # PlatformFacts + WHIRLWIND_PLATFORM 覆盖 + @platform_impl 行为插件（ADR-0007）
│   ├── storage/                     # 存储层：接口与实现分离（开闭原则样板）
│   │   ├── providers.py                 # 六大 Protocol：MetadataStore / KVStore / LockProvider / ObjectStore / EventLog / EventBus
│   │   ├── memory.py / local.py         # 进程内 / 目录实现（默认后端）
│   │   ├── wal_eventlog.py              # durable WAL：fsync 落盘 + 组提交 + 崩溃恢复
│   │   ├── postgres.py / redis.py       # 生产后端（asyncpg / redis，语义对齐共享契约测试）
│   │   └── skills.py                    # skill 归档存储
│   ├── drivers/                     # 沙箱底座：单接口多实现
│   │   ├── base.py                      # SandboxDriver Protocol + Caps / SandboxSpec / Resources / Instance
│   │   ├── process.py                   # 进程组 driver（macOS/Linux，M1 默认）
│   │   ├── runsc.py                     # gVisor driver（Linux，FULL 快照/checkpoint）
│   │   └── microsandbox.py              # libkrun/krunkit driver（LIGHT_VM，mac 本地 VM 底座；ADR-0006，定稿待实施）
│   ├── transport/                   # 传输抽象：endpoints.py 解析 + transports.py connect/serve（tcp/unix/vsock）
│   ├── hostlet/                     # 节点代理：沙箱生命周期编排 + SecretRelay（凭证出网替换）
│   ├── agent/                       # SandboxAgent：沙箱内首进程（stdlib asyncio HTTP，无重框架）
│   ├── harness/                     # HarnessAdapter 接口 + echo 基线 + dsh adapter（stdio JSON-RPC）+ protocol.py
│   ├── seam/                        # Seam 契约模型（SeamDefinition / ProviderSpec / SeamBinding / InjectionManifest）与 Renderer
│   ├── imaging/                     # ImageRegistry 接口 + LocalRegistry（构建即真实安装）
│   ├── control/                     # 控制面：manager（会话）/ scheduler（调度）/ pool（warm 池 CAS）/ lifecycle（ensure_* 幂等步骤链）
│   ├── gateway/                     # FastAPI：REST + SSE + MCP Gateway + cron.py + idempotency.py
│   ├── timer/                       # Kafka 式层次时间轮 wheel.py + cron.py（croniter 委托）
│   ├── bus/                         # 进程内事件总线（主题扇出、seq 游标）
│   ├── secrets.py                   # Agent env 密钥：SecretBox 封存/解密 + 名字校验（ADR-0010，仅宿主侧导入，沙箱冷路径永不加载）
│   ├── runtime.py                   # WhirlwindRuntime：自底向上装配 storage→hostlet→control→gateway，后端选择（metadata_backend/kv_backend）
│   ├── config.py                    # 统一配置加载：TOML + env + CLI 分层注入，schema 强校验（ADR-0009，§3.5）
│   └── cli.py                       # CLI 入口（whirlwind 命令；serve 经 load_settings 装配，config show 自省）
├── scripts/
│   └── run_tests.py                     # 测试运行器：完整输出落 .test-logs/，控制台仅简要结论（ADR-0008，§4.5）
├── tests/
│   ├── unit/                        # 纯逻辑单测（statemachine / model / idempotency / seam / endpoints ...）
│   ├── integration/                 # 真实进程/文件系统/本地 HTTP；conftest.py 提供 PG/Redis 可达性检查与多后端参数化 fixture
│   ├── e2e/                         # WHIRLWIND_E2E=1 + 真实 DeepSeek API 才运行
│   └── benchmark/                   # pytest-benchmark 真实测量（见 §4.3 验收基线）
└── deploy/k3s/                      # k3s 部署形态：Dockerfile / manifest.yaml / build-image.sh / smoke.sh / dev-server.sh
```

---

## 3. 开发规范（开闭原则）

### 3.1 依赖方向与抽象接口

**模块之间只依赖抽象（Protocol），不依赖实现。** 这是本仓库最重要的结构性约束，也是"对扩展开放、对修改关闭"的落地方式：

- 新增一种**沙箱底座**（如 Firecracker microVM）＝ 在 `drivers/` 新增一个实现文件，调度器/Hostlet 零改动（`SandboxDriver` 见 [base.py](src/whirlwind/drivers/base.py)）；
- 新增一种**存储后端**（如 NATS EventBus、对象存储）＝ 在 `storage/` 新增 provider 实现，运行时经 `RuntimeConfig` 注入（接口见 [providers.py](src/whirlwind/storage/providers.py)）；
- 新增一种 **harness** ＝ 在 `harness/` 新增 adapter（`HarnessAdapter.prepare(manifest) -> PreparedHarness`），平台不改内核；
- 新增一种**传输链路** ＝ 在 `transport/transports.py` 注册 connect/serve 分支；
- 新增一种 **Seam provider** ＝ 实现 Seam 契约三元组（Definition / Provider / Consumer）；
- 新增一种**平台特化行为** ＝ 在实现模块用 `@platform_impl(feature, platform)` 注册插件，`resolve_impl(feature)` 按当前平台键分发（`"*"` 兜底），调用方零改动（ADR-0007）。

**判断标准：如果你的改动需要修改既有接口签名或调度核心才能接入新能力，说明抽象放错了位置——先修订 ADR 再动代码。**

关键抽象接口速查：

| 抽象 | 位置 | 形式 | 扩展方式 |
| --- | --- | --- | --- |
| `SandboxDriver` | `drivers/base.py` | `@runtime_checkable Protocol` | 新增驱动文件，如实声明 `Caps` |
| `MetadataStore` / `KVStore` / `LockProvider` / `ObjectStore` / `EventLog` / `EventBus` | `storage/providers.py` | `Protocol` | 新增后端实现 + 共享契约测试 |
| `HarnessAdapter` | `harness/adapter.py` | 接口 + echo 基线 | 新增 adapter（镜像 + 配置模板分发） |
| Seam 契约 | `seam/model.py` | pydantic 模型 + Renderer | 新增 provider spec / binding |
| `ImageRegistry` | `imaging/base.py` | 接口 | 新增 registry 实现 |
| 传输 | `transport/transports.py` | `connect()/serve()` 函数分派 | 新增 scheme 分支 |
| 平台事实与行为插件 | `core/platform.py` | `PlatformFacts` 冻结值对象 + `@platform_impl` 注册表 | 新增 feature 的平台特化实现（导入即接线） |

### 3.2 数据模型定义规范

- **领域实体**（需持久化/跨进程传输）：pydantic `BaseModel`，放 `core/model.py`，字段可序列化、带默认值与毫秒时间戳；新增实体先在这里定义，再在各 provider 接口中暴露存取方法。
- **接口层值对象**（驱动入参/能力声明等不需要序列化的）：`@dataclass(frozen=True, slots=True)`，放对应模块（如 `drivers/base.py` 的 `Caps` / `SandboxSpec` / `Resources`）。
- **枚举**：一律 `StrEnum`（如 `SessionStatus` / `SandboxStatus` / `SnapshotKind` / `Isolation`）。
- **状态迁移**：会话与沙箱的状态只能沿 `core/statemachine.py` 的转移表（`SESSION_TRANSITIONS` / `SANDBOX_TRANSITIONS`）走，经 `check_transition()` 校验；不允许在业务代码里直接赋值 `status` 字段绕过状态机。
- **错误**：继承 `WhirlwindError`，必须带稳定 `code` 字段（形如 `whirlwind/driver/not-found`），错误码是对外契约。
- **ID 生成**：统一走 `core/ids.py`。
- **能力诚实原则**：driver 的 `Caps` 与 `Resources` 声明必须与实际执行机制一致（声明即执行）；已知无法诚实保证的维度要么不声明，要么在 docstring 里写明 caveat（参考 `Resources` 的写法）。

### 3.3 通用编码约定

- Python 3.12+ / asyncio；依赖最小化（`fastapi` / `uvicorn` / `httpx` / `pydantic` / `croniter` / `pyyaml` / `pynacl`），新增依赖必须有 ADR 论证（pynacl 见 ADR-0010：标准库无 AEAD，不手搓密码学）。
- 双平台可运行（macOS M 系列 + Linux）：`pathlib` 路径、`asyncio.subprocess`，不用 Linux-only syscall；平台差异经 `core/platform` 事实 + 各层 `@platform_impl` 插件封装（见下条）。
- **平台分支统一走 [core/platform.py](src/whirlwind/core/platform.py)（ADR-0007）**：禁止在业务代码直接判断 `sys.platform` / `platform.machine()`；事实取 `current_facts()`，行为差异用 `@platform_impl` 注册、`resolve_impl` 分发。`WHIRLWIND_PLATFORM` 环境变量（`auto|macos|linux|windows[/machine]`）仅供开发/测试模拟**身份**（派生语义随之），探测类事实（`/dev/vsock`、`CAP_SYS_ADMIN`）永不被覆盖——模拟平台不能让缺失的底座变绿（§4.2 的诚实边界）。
- 自造轮子前先查成熟库（ADR-0003 的教训与原则）；自研组件（时间轮、WAL）需独立封装、独立测试。
- **性能优化必须合理：懒加载只用于省掉"当前执行路径确实不需要"的导入**（如子进程启动路径不用的 pydantic、无 LLM 配置时 echo 不用的 urllib、未选中的存储后端）；业务必需的加载一律保持急加载（如 agent.server 的 asyncio、gateway 的核心模型），不为 benchmark 数字把必要成本推迟到请求路径上。冷启动优化以 ADR-0008 的基准实测为准，不许为了数字牺牲架构清晰度。
- 包管理用 `uv`（`uv sync`）；测试一律经 runner 执行（§4.4-4.5），交互式调试才直跑 `uv run python -m pytest ...`。

### 3.4 平台能力分支（Platform capabilities）

一口统一"当前跑在什么系统上、该走哪套实现"的跨切能力，载体是 [core/platform.py](src/whirlwind/core/platform.py)，**详细决策见 [ADR-0007](docs/adr/0007-platform-abstraction.md)（D1–D4）**。本分支是横切 core 模块，位于所有消费方（drivers / transport / imaging / tests）之下，不改变架构分层与 harness 可见面。

能力清单与设计决策对应关系：

| 能力 | 形态 | ADR-0007 决策 |
| --- | --- | --- |
| 统一平台事实 | `PlatformFacts` 冻结值对象：`system` (`macos`/`linux`/`windows`/`unknown`) / `machine` (`arm64`/`x86_64`/原样) / `vsock` / `restricted`；派生语义 `rlimit_as_supported` / `uds_path_max` / `overridden` | D1 |
| 当前环境配置 | 环境变量 `WHIRLWIND_PLATFORM=auto\|macos\|linux\|windows[/machine]`；`current_facts()` 在真实探测上叠加身份覆盖；非法值抛 `ValueError` | D2 |
| 平台行为插件 | `@platform_impl(feature, platform)` 注册 + `resolve_impl(feature)` 按当前 system 键分发，`"*"` 为兜底实现；未注册抛稳定错误码 `whirlwind/platform/impl-not-found` | D3 |
| 迁移收编点 | process rlimits、vsock 探测、imaging 平台标签、测试门控（process/edge/runsc）已从裸 `sys.platform` 迁到 facts/插件 | D4 |

**诚实边界（§4.2 的落地，不可绕过）**：`WHIRLWIND_PLATFORM` 只覆盖**身份**与派生语义；探测类事实（`vsock`、`restricted`）永远反映真实宿主——覆盖不能伪造设备/二进制/内核执行，所以模拟 `linux` 无法让缺失的 `/dev/vsock` 或 runsc 底座变绿。真实执行类测试仍按真实探测门控（`shutil.which`、`/dev/vsock`）。

当前已注册的行为插件 feature（生产侧导入即接线）：

- `drivers.process.rlimits`：`"*"` = POSIX 全量（RLIMIT_AS / RLIMIT_CPU / RLIMIT_NPROC）；`"macos"` = 诚实剔除 `RLIMIT_AS`（macOS 内核拒绝 soft=hard，ADR-0005 D1 的透明化）。策略在父进程 (`create`) 解析、`preexec_fn` 只应用预计算计划（fork/exec 间不做探测，异步信号安全）。

平台差异化设计约束见 [ADR-0001](docs/adr/0001-agent-runtime-m1.md)（driver 接缝/能力位如实上报）与 [ADR-0005](docs/adr/0005-edge-hardening.md)（资源上限诚实注记）；VM 级底座 [microsandbox](docs/adr/0006-microsandbox-driver.md)（ADR-0006，`Isolation.LIGHT_VM`，libkrun/krunkit）同样走 `SandboxDriver` 接口而与平台层解耦。

### 3.5 统一配置（Unified configuration）

**禁止写死运维配置**：所有运维可调值（bind 地址、数据目录、后端选择、资源上限、会话配额……）统一经 [config.py](src/whirlwind/config.py) 的单一加载机制注入，**详细决策见 [ADR-0009](docs/adr/0009-unified-config.md)**。要点：

- **优先级阶梯**：代码默认值 < `whirlwind.toml` < `WHIRLWIND_*` 环境变量 < 显式 CLI 参数。默认值只在 loader 定义**一次**；CLI 参数是纯覆盖（argparse 默认值为 `None` 哨兵，显式给出才生效），不得在 argparse / dataclass 里重复写死默认值。
- **文件发现**：`--config PATH` > `$WHIRLWIND_CONFIG` > `./whirlwind.toml`（存在时）> 纯默认值（零配置可启动）。
- **schema 强校验**：未知分区/键是硬错误（`whirlwind/config`）；新增可调值必须同时登记 loader schema、env 映射与（视情况）CLI 参数，并在 ADR-0009 的 schema 块中文档化。
- **不进配置文件**（ADR-0009 D8）：密钥**值**（只配置环境变量名 `api_key_env`，值保持真实环境变量由 Hostlet 读取）、沙箱内部注入变量（`WHIRLWIND_SANDBOX_ID` 等，运行时注入）、`WHIRLWIND_PLATFORM`（ADR-0007，env-only）、`WHIRLWIND_URL`（客户端关注点）。
- 自省：`whirlwind config show` 打印生效配置（来源 + 生效 TOML）。

---

## 4. 测试规范

### 4.1 组织与分层

| 层 | 位置 | 对象 | 运行条件 |
| --- | --- | --- | --- |
| 单元 | `tests/unit/` | 纯逻辑（状态机、模型、解析） | 无条件运行 |
| 集成 | `tests/integration/` | 真实子进程 / 文件系统 / 本地 HTTP / 真实 PG/Redis | 依赖不可达时 skip（conftest 探测），**绝不 stub** |
| E2E | `tests/e2e/` | 真实 dsh + 真实 DeepSeek API | `WHIRLWIND_E2E=1` 且持有 API key |
| 基准 | `tests/benchmark/` | 性能验收与调优对比 | 真实服务实测 |

### 4.2 铁律：禁止 mock / 伪造 / 作弊

来自 ADR-0001 §6，对本仓库具有宪法地位：

1. 集成测试必须跑**真实进程、真实文件系统、真实本地 HTTP**——不允许用 mock 替代被测系统的执行面；
2. 外部二进制/服务不可用（runsc、PostgreSQL、Redis、`/dev/vsock`）时，测试**跳过（skip）**并在 skip reason 里说明，而不是伪造一个假实现让测试变绿；
3. 多后端实现（memory/postgres/redis）之间语义对齐由**共享契约测试套件**钉死，不是各写各的；
4. 测试是行为验收，不是覆盖率表演。

### 4.3 Benchmark：真实测量与验收基线

benchmark 用 `pytest-benchmark`（`tests/benchmark/`），用于验证 ADR 验收线与调优对比（如 memory vs PostgreSQL vs Redis）。**数字必须来自真实运行，禁止写死/伪造/挑帧；调优结论要附测量方法与环境。**

ADR 既定验收基线（改动波及相关路径时必须复测）：

| 指标 | 基线 | 基准文件 |
| --- | --- | --- |
| 沙箱冷启动（process driver，含资源限制） | p50 ≤ 250ms | `test_edge_bench.py` |
| WAL EventLog 组提交突发写入 | ≥ 5k events/s | `test_pipeline_bench.py` |
| 事件总线扇出 | ≥ 20k events/s | `test_pipeline_bench.py` |
| 时间轮调度 | 10k schedules | `test_wheel_bench.py` |
| warm 池 CAS 认领 | p50 ≤ 2ms | `test_pool_bench.py` |
| 存储后端对比 | memory/PG/Redis 实测基线 | `test_storage_bench.py` |

### 4.4 常用命令

测试/benchmark 一律经 runner 执行（§4.5）；pytest 直跑仅用于交互式调试（`-x` / `-s` / 需要实时输出时）：

```bash
uv run python scripts/run_tests.py                                        # 全量（单测+集成+基准，非 e2e）
uv run python scripts/run_tests.py tests/unit -q                          # 仅单测（最快反馈）
uv run python scripts/run_tests.py tests/integration/test_runsc_driver.py -q  # 真实 gVisor（需 runsc）
uv run python scripts/run_tests.py tests/benchmark/test_pipeline_bench.py -q  # 基准复测
WHIRLWIND_E2E=1 uv run python scripts/run_tests.py tests/e2e -q           # 真实 DeepSeek API
```

提交前的最低门槛建议：`run_tests.py` 全量全绿（被 skip 的必须能说出正当理由）。

### 4.5 测试执行日志规范（ADR-0008）

[scripts/run_tests.py](scripts/run_tests.py) 是测试/benchmark 的标准入口，契约：

- **完整输出落盘，不刷控制台**：pytest 全部输出（含基准表格、子进程 chatter）写入 `.test-logs/<时间戳>-<scope>.log`（一次性开发产物，已 gitignore），按需查阅；
- **控制台只要结论**：一行 verdict（`PASS/FAIL · N failed · N passed · N skipped · exit=N`）；失败时逐条输出 `nodeid — 异常类型: 首行异常信息`，完整 traceback 只在日志文件里；
- **退出码即"是否错误"**：原样透传 pytest 退出码（0 成功 / 1 失败 / 2 中断 / 3 内部错误 / 4 用法错误 / 5 未收集到测试——按 FAIL 处理），CI 与 agent 据此判断；
- 结论解析自 **junitxml**（`--junitxml` 结构化数据），不做控制台文本抓取；日志头部记录命令、退出码与平台事实（dogfood ADR-0007）。

---

## 5. 文档体系

### 5.1 ADR 先行原则

**任何非平凡变更（新接口、新数据模型、新模块、新算法、性能权衡、依赖引入、与架构文档的偏差）必须在动手写代码之前先写 ADR 或修订既有 ADR。** ADR 是事实源，代码注释回答"怎么做"，ADR 回答"为什么这么做、还考虑过什么"。

ADR 需覆盖的设计维度（按需取舍，至少明确其一）：

- **接口设计**：抽象接口签名、扩展点、契约语义；
- **数据模型设计**：实体、字段、状态机迁移、序列化形态；
- **架构设计**：模块职责、依赖方向、失败语义、部署形态；
- **算法设计**：核心机制（如时间轮、WAL 组提交、CAS 认领）、复杂度与验收基线。

### 5.2 ADR 写作规范

- 位置 `docs/adr/`，命名 `NNNN-<slug>.md`，编号连续递增（下一个是 0011）。
- 结构对齐既有 ADR（参考 [0001](docs/adr/0001-agent-runtime-m1.md)）：标题（中英）→ Status / Date / Related / Scope → 背景与目标 → **Key Decisions（编号 D1/D2/…）** → 详细设计 → 测试策略 → 与架构文档的冲突检查 → 实施顺序 → 风险与开放点。
- 决策必须**编号**（D1/D2/…），后续变更通过在新 ADR 中引用旧编号来修订（如 `→ Delivered (M3)`、superseded by），不回写抹除历史。
- 与架构文档（v0.6）的偏差必须显式记录并给理由（ADR-0001 D2 是范例）。
- 正文遵循项目双语惯例：中英对照段落。

### 5.3 Session-log 开发记录

每个开发 session（一次自闭环变更的实现过程）在 `docs/session-logs/` 落一份记录，命名 `YYYY-MM-DD-<slug>.md`：

```markdown
# Session: <一句话目标>（YYYY-MM-DD）

## 目标            # 本 session 要解决什么（对应 TODO 项 / ADR 编号）
## 前置            # 阅读了哪些 ADR / 代码 / 上一份 session-log
## 变更清单        # 动了哪些文件、新增了什么（与 ADR 决策编号对应）
## 关键决策与发现  # 实现中的取舍、踩坑、与预期不符的行为
## 验证证据        # 实际运行的测试命令与输出摘要；benchmark 实测数字（禁止转述/编造）
## 遗留与 handoff  # 未完成项、下一步、给下一个 session 的注意事项
```

原则：**写给下一个接手的人（或下一个上下文周期的你自己）**，可复核、有证据、不粉饰。

### 5.4 TODO.md 活文档

每个自闭环变更合入时同步更新 [docs/TODO.md](docs/TODO.md)：勾选项用 `[x]`/`[~]`/`[ ]`/`(!)` 图例，并在 Done 区追加一行带月份与 ADR 指针的记录。

### 5.5 双语文档规范（顶层文档拆分）

顶层文档（README、架构设计文档）**不采用中英混排**，而是拆为独立的两份并互链切换：

- **命名**：英文版用原名（`README.md` / `agent-runtime-architecture.md`），中文版加 `.zh-CN.md` 后缀（`README.zh-CN.md` / `agent-runtime-architecture.zh-CN.md`）；
- **互链**：两份文档顶部第一行放语言切换链接（`**English** | [中文](xxx.zh-CN.md)`），当前语言加粗、另一语言为链接；
- **内容对齐**：两份文档是同一内容的两种语言，结构（章节、表格、图、代码块）必须一一对应；更新时同步更新两份，不允许只改一份；
- **共享元素**：表格、代码块、mermaid 图、图片等构件两份共用同一内容（图内标签/代码注释保持原样即可）；
- **ADR 与 session-log 不拆分**：`docs/adr/` 与 `docs/session-logs/` 保持既有「中英对照段落」风格（历史记录只增不删），只有面向外部读者的顶层文档走拆分形态。

---

## 6. 项目记忆与长上下文 Handoff

整体开发是一个长周期、长上下文的任务。约定用「磁盘化记忆 + 上下文卸载」保证跨 session / 跨上下文压缩的连续性。

### 6.1 记忆分层

| 层 | 载体 | 寿命 | 内容 |
| --- | --- | --- | --- |
| 规范层 | 本文档（AGENTS.md） | 长期 | 协作规范，低频修订 |
| 决策层 | `docs/adr/` | 永久 | 编号化设计决策，只增不删 |
| 状态层 | `docs/memory/MEMORY.md` | 持续更新 | 项目当前快照（见下） |
| 过程层 | `docs/session-logs/` | 追加归档 | 每次 session 的过程与证据 |
| 路线层 | `docs/TODO.md` | 持续更新 | 优先级与完成状态 |

`docs/memory/MEMORY.md` 保持精简（目标一屏内），只存**结论与指针**，详细内容落到 ADR/session-log，避免两处漂移：

```markdown
# MEMORY
- 快照：当前里程碑（M1/M2 已完成，M3 部分，P0/P1 …）｜一句话系统形态
- 进行中：<进行中的任务与所在 session-log 指针>
- 环境事实：<平台差异 / 二进制依赖位置 / 哪些测试在本机 skip 及原因 / dsh 本地 checkout 路径等>
- 坑与注意：<已知的上游限制（如 runsc rootless 不支持 restore）、易错点>
- 决策索引：<主题 → ADR 编号的速查表>
```

### 6.2 Handoff 流程

**Session 启动（或上下文被压缩/接手新任务）时，按序恢复上下文：**

1. 本文档（AGENTS.md）；
2. `docs/memory/MEMORY.md`——当前状态与进行中事项；
3. `docs/TODO.md`——任务队列与优先级；
4. 最近一份 `docs/session-logs/`（尤其"遗留与 handoff"节）；
5. 与本次任务相关的 ADR（按 MEMORY 的决策索引定位）。

**Session 结束（或感知上下文接近上限、即将被压缩）时，先卸载再继续：**

1. 写/更新当日的 session-log（含验证证据与 handoff 注记）；
2. 增量更新 `MEMORY.md`（快照、进行中、坑）与 `TODO.md`（勾选状态）；
3. 若有未落 ADR 的既成决策，补 ADR 或在 session-log 中标记"待 ADR 化"。

**铁律：状态先落盘，再关闭上下文。** 任何只存在于对话上下文里的关键结论（设计取舍、测量数字、环境发现），在压缩后等同于丢失。

---

## 7. 附：环境速查

> **环境先检测，不预设平台。** 开发/执行环境不一定是 macOS（可能是 Linux、受限容器、无头 CI）。凡是 VM 级或容器级工作流（microsandbox/krun/krunkit、Docker、k3s 集群、runsc），**执行前先检测所在环境**——平台、可用二进制（`shutil.which`）、虚拟化支持（`/dev/vsock`、`CAP_SYS_ADMIN`）——再决定路径或如实降级/skip。集成测试已按此约定经 `conftest.py` 探测，后端不可达即 skip（禁 mock 铁律 §4.2）。不要把"当前沙箱可不可用"误当成"平台不支持"（参考 k3s 曾经的假性失败）。

- **启动**：`uv sync` → `whirlwind image build echo` → `whirlwind serve`（配置经 `whirlwind.toml` / `WHIRLWIND_*` env / CLI 分层注入，§3.5；`whirlwind config show` 自省生效配置；示例见 `deploy/whirlwind.example.toml`）
- **后端切换**：`whirlwind serve --metadata-backend postgres --kv-backend redis` 或经 `[storage]` 配置分区（需 `whirlwind[postgres]` / `whirlwind[redis]` extras）
- **生产后端依赖**：PostgreSQL / Redis 需本地可达；`tests/integration/conftest.py` 探测，不可达即 skip
- **gVisor**：`runsc` 仅 Linux；无二进制或受限容器（无 `CAP_SYS_ADMIN`）自动降级 rootless + `--network=none`（rootless 不支持 restore，上游限制）；也验证过可装在 colima 的 Linux Docker VM 内作为 Docker runtime
- **microsandbox**：libkrun/krunkit（`Isolation.LIGHT_VM`），需 `Virtualization.framework`/真机；colima `--vm-type krunkit` 是其一等后端（ADR-0006）
- **k3s / macOS**：`colima start --kubernetes`（底层 k3s）即为本地集群；无头沙箱里"不支持"多为执行环境假象，应先在真机/CLI 验证再下结论
- **dsh**：公开仓库 `github.com/deepseek-ai/deepseek-harness`（MIT）；镜像构建需本地 checkout（`refs/deepseek-harness`，可用 `WHIRLWIND_DSH_REPO` 覆盖）；两个 Python 包均不在 PyPI
- **平台事实/模拟**：平台判断统一走 `core/platform`（ADR-0007）；`WHIRLWIND_PLATFORM=macos|linux|windows[/machine]` 仅模拟身份与派生语义，探测类事实（`/dev/vsock`、`CAP_SYS_ADMIN`）永不被覆盖，生产环境不设置
- **测试日志**：测试/benchmark 经 `scripts/run_tests.py` 执行；完整输出在 `.test-logs/`（gitignore），控制台仅 verdict + 失败摘要（ADR-0008，§4.5）
- **部署**：`deploy/k3s/`（镜像构建 + manifest + 可重跑 smoke 脚本）
