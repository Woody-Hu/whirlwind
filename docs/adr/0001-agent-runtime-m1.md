# ADR-0001: Whirlwind Agent Runtime — M1 Vertical Slice Detailed Design & Implementation Plan
# ADR-0001: Whirlwind 智能体运行时 — M1 竖切详细设计与实施计划

- Status: Accepted
- 状态：已接受
- Date: 2026-08-18
- 日期：2026-08-18
- Related: [agent-runtime-architecture.md](../../agent-runtime-architecture.md) (v0.6 architecture draft)
- 关联：[agent-runtime-architecture.md](../../agent-runtime-architecture.md)（v0.6 架构草案）
- Scope: M1 (single-node vertical slice) + M2 (full lifecycle), plus MCP Gateway / CLI / image-registry designs not covered by the architecture doc
- 范围：M1（单节点竖切）+ M2（生命周期完整），含架构文档未覆盖的 MCP Gateway / CLI / 镜像库设计

---

## 1. Background & Goals
## 1. 背景与目标

The architecture doc defines the five-layer structure and module responsibilities. This ADR refines it into actionable engineering decisions: package structure, interface signatures,
the Python shape of the domain model, test and benchmark acceptance criteria, and explicitly records deviations from the architecture doc along with their rationale.
架构文档已给出五层结构与模块职责。本 ADR 将其细化为可实施的工程决策：包结构、接口签名、
领域模型的 Python 形态、测试与 benchmark 验收标准，并显式记录与架构文档的偏差与理由。

**M1 acceptance goal**: in single-process all-in-one mode, a native DeepSeek Harness (dsh) image mounts into the Whirlwind runtime;
agents / sessions / turns are created via the REST API, events stream back over SSE, LLM calls egress through the relay without credentials entering the sandbox,
and skill / seam (tool) configuration can be injected into an agent and take effect.
**M1 验收目标**：单进程 all-in-one 模式下，原生 DeepSeek Harness（dsh）镜像挂载进 Whirlwind runtime，
通过 REST API 创建 agent / 会话 / 发起 turn，事件经 SSE 流式返回，LLM 调用经 relay 出网且凭证不进沙箱，
skill / seam（tool）配置可注入 agent 并生效。

**Verified feasibility** (2026-08-18, in this repo's dev sandbox; acceptance e2e re-verified the same day):
**已验证的可行性**（2026-08-18，本仓库开发沙箱内；验收 e2e 复验于同日）：

- The dsh Python side ships as two source packages: `python/sdk` (`deepseek-harness-sdk`) and `python/sdk-runtime`
  (`deepseek-harness-runtime-bin`, carrying the platform single-file exe). **Neither is on PyPI** — image builds install from the local
  checkout (`refs/deepseek-harness`, overridable with `WHIRLWIND_DSH_REPO`); the exe is built by the repo script
  `pnpm exec tsx scripts/build-exe-for-python-sdk.ts` (macOS arm64 / linux x64+arm64).
- dsh Python 侧分发为两个源码包：`python/sdk`（`deepseek-harness-sdk`）与 `python/sdk-runtime`
  （`deepseek-harness-runtime-bin`，承载平台单文件 exe）。**两者均不在 PyPI**——镜像构建从本地
  checkout 安装（`refs/deepseek-harness`，可用 `WHIRLWIND_DSH_REPO` 覆盖）；exe 由仓库脚本
  `pnpm exec tsx scripts/build-exe-for-python-sdk.ts` 构建（macOS arm64 / linux x64+arm64）。
- dsh runtime + real DeepSeek API end-to-end works (`h.run("Reply with exactly: OK")` → `OK` / `completed`).
- dsh 运行时 + 真实 DeepSeek API 端到端跑通（`h.run("Reply with exactly: OK")` → `OK` / `completed`）。
- dsh depends on the env vars `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY`, which fits the sidecar LLM Relay design naturally.
  Note: the llm-deepseek adapter requires an API key in the environment before it will send requests — in the sandbox a non-secret placeholder
  `DEEPSEEK_API_KEY=whirlwind-relay` is injected; real credentials exist only in the Hostlet, and SecretRelay replaces the
  Authorization header at egress (the placeholder never reaches upstream).
- dsh 依赖环境变量 `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY`，与 sidecar LLM Relay 设计天然契合。
  注意：llm-deepseek 适配器要求环境里存在 API key 才发请求——沙箱内注入非密钥占位符
  `DEEPSEEK_API_KEY=whirlwind-relay`，真实凭证仅存在于 Hostlet，由 SecretRelay 在出网时替换
  Authorization 头（占位符永不到达上游）。
- LLM traffic is SSE (`text/event-stream`): both hops of the agent ↔ Hostlet relay are unbuffered byte-stream pass-through.
- LLM 流量为 SSE（`text/event-stream`）：agent 与 Hostlet 两跳 relay 均为未缓冲字节流透传。

## 2. Key Decisions
## 2. 关键决策

### D1 Language & Runtime: Python 3.12+ / asyncio / FastAPI
### D1 语言与运行时：Python 3.12+ / asyncio / FastAPI

Consistent with architecture §8.2 "control-plane tech stack". Constraints:
与架构 8.2「控制面技术栈」一致。约束：

- Minimal dependencies: `fastapi`, `uvicorn`, `httpx`, `pydantic>=2`. Testing: `pytest`, `pytest-asyncio`. Benchmark: `pytest-benchmark`.
- 依赖最小化：`fastapi`、`uvicorn`、`httpx`、`pydantic>=2`。测试：`pytest`、`pytest-asyncio`。benchmark：`pytest-benchmark`。
- No Redis/Postgres/NATS clients as M1 dependencies — M1 uses in-memory provider implementations throughout.
- 不引入 Redis/Postgres/NATS 客户端作为 M1 依赖——M1 全部走 provider 内存实现。
- Runs on both macOS (M-series) and Linux: paths use `pathlib`, processes use `asyncio.subprocess`,
  no Linux-only syscalls; platform differences are encapsulated in the driver layer.
- macOS（M 芯片）与 Linux 双平台可运行：路径用 `pathlib`，进程用 `asyncio.subprocess`，
  不使用 Linux-only 系统调用；平台差异封装在 driver 层。

### D2 Sandbox Base: M1 defaults to the `process` driver, not gVisor
### D2 沙箱底座：M1 默认 `process` driver，而非 gVisor

**Deviation from the architecture doc** (the architecture's M1 wrote "single SandboxClass (start with gVisor)"). Rationale:
**与架构文档的偏差**（架构 M1 写「单 SandboxClass（gVisor 起步）」）。理由：

1. gVisor (runsc) is Linux-only, which directly conflicts with "M-series Mac local dev/test friendliness";
2. Architecture §4.4 already established the principle "capability bits are the only scheduling basis, class is just a label"; `Process` is itself a
   legal value of the `Isolation` enum (dsh's `sandbox-local`, and Substrate's in-process actor are both process-level isolation precedents);
3. The `SandboxDriver` interface is fully implemented per architecture §4.4 (caps declaration, create/exec/pause/checkpoint/restore/destroy),
   and the `process` driver honestly declares `isolation=Process, snapshot_full=False, snapshot_data=True, density=High, net_policy=False`.
   The runsc/firecracker driver is added to the same interface in M3 (Linux cluster form) with zero scheduler changes.
   **→ Delivered (M3)**: the runsc driver is landed and validated against real gVisor for the full lifecycle, see
   [ADR-0002 D1](0002-m3-substrates.md).
1. gVisor（runsc）仅 Linux 可用，与「M 芯片 Mac 本地开发测试亲和」直接冲突；
2. 架构 4.4 已确立「能力位是调度唯一依据、class 只是标签」的原则，`Process` 本就是
   `Isolation` 枚举的合法取值（dsh 的 `sandbox-local`、substrate 的进程内 actor 同为进程级隔离先例）；
3. `SandboxDriver` 接口按架构 4.4 完整实现（caps 声明、create/exec/pause/checkpoint/restore/destroy），
   `process` driver 如实声明 `isolation=Process, snapshot_full=False, snapshot_data=True, density=High, net_policy=False`。
   runsc/firecracker driver 按同一接口在 M3（Linux 集群形态）加入，调度器零改动。
   **→ 已兑现（M3）**：runsc driver 已落地并对真实 gVisor 全生命周期验证，见
   [ADR-0002 D1](0002-m3-substrates.md)。

**Isolation boundary (process driver, honestly declared)**: independent process group + independent workspace directory (cwd confined to the workspace root) +
an environment-variable allowlist (does not inherit arbitrary host env) + LLM egress only via the injected relay URL. This is configuration-level isolation, not kernel-level isolation;
caps are reported honestly and are visible to the tenant (never stronger than declared).
**隔离边界（process driver，如实声明）**：独立进程组 + 独立工作区目录（cwd 限定 workspace root）+
环境变量白名单（不继承宿主任意 env）+ LLM 出站仅经 relay URL 注入。这是配置级隔离而非内核级隔离，
caps 如实上报，租户侧可见（不强于声明）。

### D3 dsh Integration Form: JSON-RPC stdio protocol + session JSONL event surface
### D3 dsh 集成形态：JSON-RPC stdio 协议 + 会话 JSONL 事件面

After studying the dsh source (`packages/core/session`, `python/sdk`), two integration surfaces were determined:
研究 dsh 源码（`packages/core/session`、`python/sdk`）后确定两个集成面：

1. **Control plane**: the dsh runtime exposes stdio JSON-RPC (`session_prompt` / `session_end` / notifications).
   SandboxAgent (the first process in the sandbox) owns this stdio and exposes it as HTTP.
2. **Event surface**: dsh's session-persistence appends `SessionEvent{type, seq, time, data, surface}` as
   JSONL. EventTap tails that file and normalizes it into platform events by the dsh event vocabulary (`turn/start`, `assistant/chunk`,
   `tool/call`, `tool/result`, `turn/end`), aligned with the §4.5 event schema.
1. **控制面**：dsh 运行时暴露 stdio JSON-RPC（`session_prompt` / `session_end` / notifications）。
   SandboxAgent（沙箱内首进程）持有该 stdio，对外暴露 HTTP。
2. **事件面**：dsh 的 session-persistence 将 `SessionEvent{type, seq, time, data, surface}` 追加写为
   JSONL。EventTap tail 该文件，按 dsh 事件词表归一化为平台事件（`turn/start`、`assistant/chunk`、
   `tool/call`、`tool/result`、`turn/end`），与架构 4.5 的事件 schema 对齐。

"Image" definition (see D6 image registry): a dsh image = a self-contained runtime directory (venv + `deepseek-harness-sdk`
+ launcher); to the runtime it is a bundle resolved from an `image ref`, interface-equivalent to an OCI image.
「镜像」定义（见 D6 镜像库）：dsh 镜像 = 一个自包含的运行时目录（venv + `deepseek-harness-sdk`
+ 启动器），对 runtime 而言是 `image ref` 解析出的 bundle，与 OCI 镜像在接口上等价。

### D4 Sidecar Placement: SandboxAgent (in-sandbox) + SecretRelay in the Hostlet (host side)
### D4 Sidecar 落位：SandboxAgent（沙箱内）+ Hostlet 内的 SecretRelay（宿主侧）

Mapping of the architecture §4.5 five-piece set under the process driver:
架构 4.5 的五件套在 process driver 下的映射：

| Architecture responsibility | M1 implementation | Location |
| --- | --- | --- |
| EventTap | tail dsh session JSONL → normalize → POST to Hostlet | SandboxAgent (in-sandbox process) |
| ControlAgent | HTTP `/health` `/turn` `/prepare_pause` `/stop` | SandboxAgent |
| ResourceInjector | read injection manifest, materialize workspace, assemble harness launch config | SandboxAgent startup phase |
| LLM Relay | `/relay/llm/*` keyless forward → Hostlet SecretRelay attaches key at egress | SandboxAgent (jump) + Hostlet (holds keys) |
| TraceRelay | structured spans carried out via event channel (M1 simplified to attribution fields) | SandboxAgent |

| 架构职责 | M1 实现 | 位置 |
| --- | --- | --- |
| EventTap | tail dsh 会话 JSONL → 归一化 → POST 到 Hostlet | SandboxAgent（沙箱内进程） |
| ControlAgent | HTTP `/health` `/turn` `/prepare_pause` `/stop` | SandboxAgent |
| ResourceInjector | 读取注入清单，落盘 workspace、装配 harness 启动配置 | SandboxAgent 启动阶段 |
| LLM Relay | `/relay/llm/*` 无密钥转发 → Hostlet SecretRelay 附加密钥出网 | SandboxAgent（跳板）+ Hostlet（持密钥） |
| TraceRelay | 结构化 span 经事件通道带出（M1 简化为归因字段） | SandboxAgent |

Keys never enter the sandbox: the dsh process environment only has `DEEPSEEK_BASE_URL=http://127.0.0.1:{agent_port}/relay/llm`;
the real `DEEPSEEK_API_KEY` exists only in the Hostlet process, outside the sandbox boundary (process group). **This is real isolation, not declarative**:
the key is neither in the sandbox env, nor the workspace, nor snapshots.
密钥不进沙箱：dsh 进程环境只有 `DEEPSEEK_BASE_URL=http://127.0.0.1:{agent_port}/relay/llm`，
真实 `DEEPSEEK_API_KEY` 仅存在于 Hostlet 进程。沙箱边界（进程组）之外。**这是真实隔离而非声明式**：
密钥既不在沙箱 env、也不在 workspace、也不在快照中。

### D5 Seam Three-Layer Abstraction (for users with different capability levels)
### D5 Seam 三层抽象（面向不同能力用户）

```
高级抽象（用户可见）      SkillPackage / ToolDecl / MemoryBinding   —— 平台概念，版本化、可审计
中间格式（Renderer 产物） SeamBinding{seam, provider, policy, consumers[]}  —— 平台中立契约
低层实现（harness 侧）    dsh cordis 配置 / MCP Server / 进程内 provider —— harness 原生形态
```

> Caption: The three seam layers — high-level user-visible abstraction, Renderer-produced intermediate format, and low-level harness-side implementation.
> 图注：seam 三层抽象——用户可见的高级抽象、Renderer 产出的中间格式、harness 侧的低层实现。

The seam IDL aligns with dsh's three roles: `Definition` (e.g. `fs.v1`, `shell.v1`, `web.v1`, `memory.v1`) is registered in the
Resource Registry; `Provider` declares the implementation (`sandbox-fs`, etc.) and policy (`mode: read-only|workspace-write|full`);
`Consumer` declares the exposure (`harness: dsh` native mapping / `harness: "*"` catch-all MCP).
The Renderer takes an AgentVersion's `seamBindings[]` + `skillRefs[]` and outputs an injection manifest (JSON file that lands in the sandbox with Bind).
Seam IDL 对齐 dsh 三角色：`Definition`（如 `fs.v1`、`shell.v1`、`web.v1`、`memory.v1`）注册于
Resource Registry；`Provider` 声明实现（`sandbox-fs` 等）与策略（`mode: read-only|workspace-write|full`）；
`Consumer` 声明暴露方式（`harness: dsh` 原生映射 / `harness: "*"` 兜底 MCP）。
Renderer 输入 AgentVersion 的 `seamBindings[]` + `skillRefs[]`，输出注入清单（JSON 文件，随 Bind 落入沙箱）。

M1 built-in seam set (aligned with dsh capabilities, all genuinely effective): `fs.v1`, `shell.v1`, `web.v1` (dsh native consumer),
`memory.v1` (persistent directory in workspace). Skill injection: the skill package is unpacked into the workspace `.whirlwind/skills/`,
loaded on the dsh side via the filesystem skill provider (dsh `skill-filesystem` mechanism), callable by the model.
M1 内置 seam 集（对齐 dsh 能力，全部真实生效）：`fs.v1`、`shell.v1`、`web.v1`（dsh 原生 consumer）、
`memory.v1`（workspace 内持久目录）。Skill 注入：skill 包解包至 workspace `.whirlwind/skills/`，
dsh 侧通过 filesystem skill provider 装载（dsh `skill-filesystem` 机制），模型可调用。

**Snapshot resume (suspend/resume on the harness side) — adopt-or-create shim**: dsh's native
sdk-jsonrpc-server always creates a fresh live session for `session/prompt`; a restored-sandbox persistent JSONL log with the same id conflicts with a fresh create
(that turn ends in an id collision, reproduced in practice). whirlwind injects
`whirlwind-resume-shim.mjs` next to the rendered cordis.yml (loaded via relative path; pkg-SEA exe verified to support runtime
dynamic import): it wraps `ctx.agents.create`, first checking `sessionPersistence.list()` — a already-materialized id goes through the official
`ctx.agents.resume` (same semantics as dsh apiproxy `api-proxy.ts`), otherwise create as-is. The resume decision is determined entirely by
disk state (whether a session log exists), decoupled from control-plane flags: InjectionManifest carries no resume semantics,
and fresh boot and snapshot boot go through the same render path.
**快照续播（suspend/resume 的 harness 侧）—— adopt-or-create shim**：dsh 原生
sdk-jsonrpc-server 对 `session/prompt` 一律新建 live session；快照恢复的沙箱里同 id 的
持久化 JSONL log 与 fresh create 冲突（该 turn 以 id collision 告终，实测复现）。whirlwind 在
渲染的 cordis.yml 旁注入 `whirlwind-resume-shim.mjs`（相对路径加载；pkg-SEA exe 实测支持运行时
动态 import）：包装 `ctx.agents.create`，先查 `sessionPersistence.list()`——已物化的 id 走官方
`ctx.agents.resume`（与 dsh apiproxy `api-proxy.ts` 同款语义），否则原样 create。续播决策完全由
磁盘状态（会话日志存在性）决定，与控制面标记解耦：InjectionManifest 不携带 resume 语义，
fresh boot 与 snapshot boot 走同一条渲染路径。

### D6 Image Registry (not covered by the architecture; new design)
### D6 镜像库（架构未覆盖，新增设计）

```python
class ImageRegistry(Protocol):
    async def resolve(self, ref: str) -> ImageBundle: ...   # ref: "dsh@0.3" / "local/echo"
    async def register(self, name: str, build: ImageBuild) -> str: ...  # 构建并登记

@dataclass
class ImageBundle:
    ref: str; root: Path            # 自包含运行时目录
    launcher: list[str]             # 绝对启动命令（argv[0] 在 root 内）
    env: dict[str, str]             # 镜像级默认 env（不含密钥）
    harness: str                    # "dsh" | "echo" | ... → 选择 HarnessAdapter
```

> Caption: The `ImageRegistry` protocol and the `ImageBundle` data structure (code unchanged except product tokens).
> 图注：`ImageRegistry` 协议与 `ImageBundle` 数据结构（代码除产品 token 外不变）。

M1 implements `LocalRegistry` (directory-based): `images/{name}/{version}/` + `manifest.json`.
`pip` builder: creates a venv in the image directory and `pip install`s (a dsh image = `deepseek-harness-sdk`) —
building is a real install, not a cached fake. The cluster-form OCI implementation leaves an interface slot (`pull` + unpack), not implemented or declared in M1.
M1 实现 `LocalRegistry`（目录型）：`images/{name}/{version}/` + `manifest.json`。
`pip` 构建器：在镜像目录创建 venv 并 `pip install`（dsh 镜像 = `deepseek-harness-sdk`）——
构建即真实安装，非缓存伪造。集群形态的 OCI 实现留接口位（`pull` + unpack），M1 不实现不声明。

### D7 MCP Gateway (not covered by the architecture; new design)
### D7 MCP Gateway（架构未覆盖，新增设计）

Architecture §8.2 positions MCP as the fallback Consumer. M1 delivery: Gateway mounts the `/mcp/{agent_version}/` endpoint,
implements a subset of MCP Streamable HTTP (`initialize` / `tools/list` / `tools/call`),
exposing the seams with `consumuers: ["*"]` in the SeamBindings rendered for that AgentVersion as MCP tools.
Non-dsh harnesses (and external IDEs) connect through this, **not on the scheduling hot path**.
架构 8.2 定位 MCP 为兜底 Consumer。M1 落地：Gateway 挂载 `/mcp/{agent_version}/` 端点，
实现 MCP Streamable HTTP 子集（`initialize` / `tools/list` / `tools/call`），
把该 AgentVersion 渲染出的 SeamBindings 中 `consumuers: ["*"]` 的 seam 暴露为 MCP tools。
非 dsh harness（及外部 IDE）经此接入，**不进入**调度热路径。

### D8 CLI (not covered by the architecture; new design)
### D8 CLI（架构未覆盖，新增设计）

`whirlwind` command (`python -m whirlwind.cli` / console_script), a pure httpx client:
`whirlwind` 命令（`python -m whirlwind.cli` / console_script），纯 httpx 客户端：

```
whirlwind serve [--port] [--data-dir]          # 启动 all-in-one
whirlwind image build dsh                       # 构建并登记 dsh 镜像
whirlwind agent create ./agent.yaml / list
whirlwind session create <agent> / send <sid> <text> --stream / events <sid>
whirlwind cron add <agent> --schedule '*/5 * * * *' --input '...'
```

> Caption: The `whirlwind` CLI subcommands (executables renamed; CLI comment lines unchanged).
> 图注：`whirlwind` CLI 子命令（可执行命令改名；注释行不变）。

### D9 Timing Wheel (Kafka-style, separately encapsulated)
### D9 时间轮（Kafka 式，独立封装）

`whirlwind/timer/`: hierarchical timing wheel. Single layer of 512 slots, tick 20ms; overflow tasks go to the upper wheel; `advance()` is driven by a
monotonic clock (self-driving inside the event loop, no signal dependency). Interface:
`whirlwind/timer/`：层次时间轮。单层 512 槽、tick 20ms；溢出任务入上层轮；`advance()` 由
单调时钟驱动（事件循环内自驱，不依赖信号）。接口：

```python
class TimingWheel:
    def schedule(self, delay: float, cb: Callable[[], Awaitable]) -> TimerHandle
    def schedule_at(self, deadline: float, cb) -> TimerHandle   # 绝对期限（keepalive 用）
```

> Caption: The `TimingWheel` interface.
> 图注：`TimingWheel` 接口。

The Cron Scheduler (G3) and Keepalive Manager (G4) share the TimingWheel and the in-process lock, unaware of each other.
Cron expression parsing is self-implemented (5-field standard semantics), with no added dependency.
Cron Scheduler（G3）与 Keepalive Manager（G4）共用 TimingWheel 与进程内锁，业务互不感知。
Cron 表达式解析自实现（5 字段标准语义），不引入依赖。

### D10 Storage & Communication Providers (aligned with architecture §10.3)
### D10 存储与通信 provider（对齐架构 10.3）

Five provider interfaces (`MetadataStore / KVStore / EventBus / ObjectStore / EventLog`) + M1 implementations:
in-memory dict / asyncio.Queue / local directory / JSONL. SQLite persistence option is added in M2. All modules depend only on interfaces.
**→ Evolution (M3)**: EventLog has been upgraded to a durable WAL implementation (group-commit fsync + crash recovery), see
[ADR-0002 D3](0002-m3-substrates.md).
五个 provider 接口（`MetadataStore / KVStore / EventBus / ObjectStore / EventLog`）+ M1 实现：
内存 dict / asyncio.Queue / 本地目录 / JSONL。SQLite 持久化选项 M2 加。全部模块只依赖接口。
**→ 演进（M3）**：EventLog 已升级为 durable WAL 实现（组提交 fsync + 崩溃恢复），见
[ADR-0002 D3](0002-m3-substrates.md)。

## 3. Package Structure
## 3. 包结构

```
src/whirlwind/
  core/        # 领域模型：ids、errors、AgentDefinition/Version、AgentSession、SessionEvent、
               #   Sandbox、Snapshot、CronJob、状态机枚举
  storage/     # 五 provider 接口 + memory/jsonl/目录实现
  bus/         # EventBus 内存实现（主题扇出、seq 游标）
  timer/       # 层次时间轮 + cron 表达式
  seam/        # SeamDefinition 注册表、SeamBinding、Renderer（AgentVersion → 注入清单）
  imaging/     # ImageRegistry + LocalRegistry + pip 构建器
  harness/     # HarnessAdapter 接口 + echo（测试基线）+ dsh adapter
  drivers/     # SandboxDriver 接口 + process driver
  hostlet/     # Hostlet（ensure/bind/turn/pause/destroy）+ SecretRelay
  agent/       # SandboxAgent（沙箱内首进程：tap/control/inject/relay 跳板）
  control/     # SessionManager、Scheduler、Lifecycle、PoolManager、Registry
  gateway/     # FastAPI app：REST + SSE + MCP gateway；CronScheduler、Keepalive
  cli/         # whirlwind 命令
  runtime.py   # all-in-one 装配（provider 注入、模块接线）
tests/
  unit/        # 纯逻辑（时间轮、cron、seam renderer、状态机、调度打分）
  integration/ # 真实子进程 / 真实文件 / 真实本地 HTTP
  e2e/         # 真实 dsh + 真实 DeepSeek API（env WHIRLWIND_E2E=1 开关，需密钥）
  benchmark/   # pytest-benchmark
```

> Caption: Whirlwind runtime package layout and test suite layout (package paths renamed `argus` → `whirlwind`, CLI renamed).
> 图注：Whirlwind runtime 包结构与测试布局（包路径 `argus` → `whirlwind`，CLI 改名）。

## 4. Domain Model (core)
## 4. 领域模型（core）

```python
AgentDefinition: name, display_name, default_version
AgentVersion:    agent_id, version, harness, image_ref, entrypoint, seam_bindings: list[SeamBindingDecl],
                 skill_refs: list[str], immutable after create
AgentSession:    id, agent_id, agent_version, status(SessionStatus), bound_sandbox, route_epoch,
                 idle_deadline, max_duration, created_at
SessionEvent:    session_id, seq(单调连续), type, ts, data, surface{op: append|replace, span}
Sandbox:         id, pool_id, status(SandboxStatus), bound_session, last_snapshot
Snapshot:        kind(GOLDEN|FULL|DATA), subject, manifest, location, size, merkle
CronJob:         agent_id, schedule, input_template, session_policy
```

> Caption: M1 domain model (no product tokens; kept unchanged).
> 图注：M1 领域模型（无产品 token，保持不变）。

The state machine is implemented per architecture diagrams 5/6: `Created → Dispatching → Running → Idle → Suspending → Suspended →
Resuming → Running → Closed`; illegal transitions raise `InvalidTransition` (unit-tested across the full matrix).
状态机照架构图 5/6 实现：`Created → Dispatching → Running → Idle → Suspending → Suspended →
Resuming → Running → Closed`；转换非法即抛 `InvalidTransition`（单测覆盖全矩阵）。

## 5. Data Flow of a Single Turn (M1 delivered form)
## 5. 一次 turn 的数据流（M1 落地形态）

```
POST /v1/sessions/{sid}/turns
 → SessionManager: 路由表查绑定（无 → Scheduler 选路：warm 池 CAS 认领 / 冷启动）
 → Hostlet.ensure(image→bundle, workspace 创建) + bind(seam 清单注入)
 → Hostlet.turn(input) → SandboxAgent /turn → dsh session_prompt (stdio JSON-RPC)
 dsh 事件 → workspace session.jsonl → EventTap tail → 归一化 → Hostlet → EventLog + EventBus
 → Gateway SSE（Last-Event-ID 以 seq 续传）
 LLM: dsh → 127.0.0.1:agent_port/relay/llm → Hostlet SecretRelay(+key) → api.deepseek.com
```

> Caption: End-to-end data flow for a single turn (code comments unchanged).
> 图注：单次 turn 的端到端数据流（代码注释不变）。

## 6. Testing Strategy (no mocks / fakes / cheating)
## 6. 测试策略（禁止 mock / 伪造 / 作弊）

Principle: **the object under test must really execute** — real subprocesses, real filesystem, real local sockets/HTTP.
Allowed stand-ins are limited to "second implementations for test purposes" (e.g. the echo harness is a real minimal harness used to drive real process paths;
llm-replay is the same idea: dsh ships an official replay adapter that replays recorded traffic, used for keyless dsh integration tests).
原则：**测试对象必须真实执行**——真实子进程、真实文件系统、真实本地 socket/HTTP。
允许的替身仅限「测试用途的第二实现」（如 echo harness 是真实的极简 harness，用于驱动真实进程路径；
llm-replay 同思路：dsh 官方自带 replay 适配器，回放录制流量，用于无密钥的 dsh 集成测试）。

| Layer | Content | Authenticity |
| --- | --- | --- |
| unit | timing wheel (ordering/cancel/overflow/monotonicity), cron parsing, seam renderer, state-machine matrix, scheduling scoring, seq allocation | pure logic, no stand-ins needed |
| integration | providers (real files/dirs), process driver + echo harness (real subprocesses), SandboxAgent HTTP, Hostlet lifecycle, Scheduler CAS, Gateway REST/SSE (real uvicorn port), MCP gateway, CLI (real local service) | all real local resources |
| e2e | real dsh image build (pip install) → mount → turn → SSE event assertions → tools genuinely invoked → snapshot-resume continuation | real DeepSeek API |
| benchmark | see §7 | real executed timings |

| 层 | 内容 | 真实性 |
| --- | --- | --- |
| unit | 时间轮（顺序/取消/溢出/单调性）、cron 解析、seam renderer、状态机矩阵、调度打分、seq 分配 | 纯逻辑，无需替身 |
| integration | provider（真实文件/目录）、process driver + echo harness（真实子进程）、SandboxAgent HTTP、Hostlet 生命周期、Scheduler CAS、Gateway REST/SSE（真实 uvicorn 端口）、MCP gateway、CLI（真实本地服务） | 全真实本地资源 |
| e2e | 真实 dsh 镜像构建（pip install）→ 挂载 → turn → SSE 事件断言 → 工具调用真实发生 → 快照恢复续话 | 真实 DeepSeek API |
| benchmark | 见 §7 | 真实执行计时 |

Keyless CI path: e2e only runs when `WHIRLWIND_E2E=1`; the integration layer does not touch the network (the LLM relay forwards to a real locally-run
replay HTTP service — the service itself really runs; the replay data is recorded samples in dsh's official llm-replay format).
无密钥 CI 路径：`WHIRLWIND_E2E=1` 才跑 e2e；integration 层不触网（LLM relay 转发到本地起的真实
回放 HTTP 服务——服务本身真实运行，回放数据是 dsh 官方 llm-replay 格式的录制样本）。

## 7. Benchmark & Performance Acceptance
## 7. Benchmark 与性能验收

`pytest tests/benchmark/` (pytest-benchmark, real timings):
`pytest tests/benchmark/`（pytest-benchmark，真实计时）：

| Benchmark | Metric | M1 acceptance line | Measured (M2-d, dev sandbox) |
| --- | --- | --- | --- |
| timing wheel vs heapq baseline | 10k task schedule+cancel throughput | wheel ≥ 80% of heapq (same-machine comparison), and O(1)-cancel advantage scenario ≥ 3x | see `test_wheel_bench.py` (passing) |
| EventBus fan-out | 10k events / 10 subscribers | ≥ 20k events/s | 1.2M deliveries/s (10k×10, zero drops) |
| EventLog append + replay | JSONL write and read-by-seq | ≥ 5k events/s writes | 24.8k events/s (incl. replay assertion) |
| Scheduler cold-start decision | no sandbox → ensure completes | echo harness ≤ 250ms (real process boot) | p50 237ms (after SandboxAgent rewritten in pure stdlib; original fastapi/uvicorn version ~669ms) |
| warm claim | CAS bind decision | p50 ≤ 2ms | p50 0.021ms (4 sandboxes really pre-started) |
| E2E turn (dsh + DeepSeek) | POST → first event arrival | record value, no hard line (network-dominated), for regression comparison | 3 cases 37.6s all passing (mount + tool/skill + snapshot resume); first event not separately timed |
| suspend/resume (dsh + DeepSeek e2e) | control-plane latency (excluding LLM turn) | record value | suspend 0.05s / resume 0.59s (incl. new sandbox ensure + snapshot restore + shim resume) |

| 基准 | 指标 | M1 验收线 | 实测（M2-d，开发沙箱） |
| --- | --- | --- | --- |
| 时间轮 vs heapq 基线 | 10k 任务调度+取消吞吐 | 时间轮 ≥ heapq 的 80%（同机对比），且取消 O(1) 优势场景 ≥ 3x | 见 `test_wheel_bench.py`（通过） |
| EventBus 扇出 | 10k 事件 / 10 订阅者 | ≥ 20k events/s | 1.2M deliveries/s（10k×10，零丢弃） |
| EventLog append + 回放 | JSONL 写入与按 seq 读取 | ≥ 5k events/s 写入 | 24.8k events/s（含回放断言） |
| Scheduler 冷启动决策 | 无沙箱 → ensure 完成 | echo harness ≤ 250ms（进程真实启动） | p50 237ms（SandboxAgent 重写为纯 stdlib 后；原 fastapi/uvicorn 版 ~669ms） |
| warm 认领 | CAS bind 决策 | p50 ≤ 2ms | p50 0.021ms（4 沙箱真实预启动） |
| E2E turn（dsh + DeepSeek） | POST → 首事件到达 | 记录值，无硬线（受网络支配），回归对比用 | 3 用例 37.6s 全通过（挂载 + 工具/技能 + 快照续播）；首事件未单独计时 |
| suspend/resume（dsh + DeepSeek e2e） | 控制面时延（不含 LLM turn） | 记录值 | suspend 0.05s / resume 0.59s（含新沙箱 ensure + 快照恢复 + shim 续播） |

Performance notes (measurement-driven):
性能注记（实测驱动）：

- EventLog fsync-per-append measured ~7x slower than the acceptance line; M1 treats flush + process lifecycle as the persistence boundary,
  and crash persistence is left for the durable-log provider replacement (architecture §10.3), with the interface unchanged.
- EventLog 每 append fsync 实测比验收线慢 ~7x，M1 以 flush + 进程生命周期为持久性边界，
  崩溃持久化留给 durable-log provider 替换（架构 10.3），接口不变。
- Synchronous burst-sending on EventBus starves subscribers (drop-oldest + EventLog backfill is the designed behavior); the benchmark
  measured zero drops at a real publisher cadence (yielding once per 64 events).
- EventBus 同步连发会饿死订阅者（drop-oldest + EventLog 补读是设计行为）；基准按真实
  publisher 节奏（每 64 事件让步一次）测得零丢弃。
- SandboxAgent cold start is dominated by import cost: fastapi/uvicorn import ~630ms vs pure stdlib ~120ms;
  the control plane switched to asyncio-native HTTP (Connection: close, close-delimited streaming pass-through, SSE-safe),
  directly benefiting every image cold start.
- SandboxAgent 冷启动由 import 成本支配：fastapi/uvicorn 导入 ~630ms vs 纯 stdlib ~120ms，
  控制面改为 asyncio 原生 HTTP（Connection: close、close-delimited 流式透传，SSE 安全），
  每个镜像冷启动直接受益。
- Snapshot-resume correctness is proven by e2e (`test_dsh_snapshot_resume_continuity`): turn1 plants a random key →
  suspend (data snapshot + sandbox workspace torn down) → resume (brand-new sandbox) → turn2 has the model recall the key purely from dsh's persisted session
  log (`.whirlwind/sessions` JSONL restored with the snapshot); the whirlwind in-process state between the two turns is zero.
- 快照续播正确性由 e2e 证明（`test_dsh_snapshot_resume_continuity`）：turn1 种入随机密钥 →
  suspend（数据快照 + 沙箱工作区拆除）→ resume（全新沙箱）→ turn2 模型仅凭 dsh 持久化会话
  日志（`.whirlwind/sessions` JSONL 随快照恢复）回忆出密钥；两次 turn 间的 whirlwind 进程内状态为零。

Benchmark results are archived in CI output (`benchmark/` result JSON) so performance regressions do not go unnoticed.
基准结果在 CI 输出存档（`benchmark/` 结果 JSON），防止性能劣化无感知。

## 8. Conflict Check Against the Architecture Doc (conclusion)
## 8. 与架构文档的冲突检查（结论）

| # | Conflict point | Handling |
| --- | --- | --- |
| 1 | architecture M1 wrote gVisor-first vs this ADR process driver | **deviation accepted** (D2), interface fully implemented per §4.4, runsc left for M3; capability bits declared honestly |
| 2 | architecture sidecar via vsock/UDS vs this ADR localhost HTTP | transport carrier differs; Sidecar HTTP contract unchanged (4.5); vsock enabled under the VM driver |
| 3 | architecture kept credentials in the sidecar's LLM Relay vs the keys-never-enter-sandbox principle | split into in-sandbox jump + host SecretRelay (D4); the principle (keys never enter sandbox) takes precedence, consistent with 6.2 |
| 4 | architecture event log in a dedicated logging system vs M1 JSONL | per §10.3 provider shape: M1 EventLog = local JSONL, interface unchanged |
| 5 | MCP Gateway / CLI / image registry not covered by the architecture | covered by ADR D6–D8; MCP keeps its fallback position (§8.2) |
| 6 | architecture's snapshot_full expectation (firecracker/runsc) | process driver only declares snapshot_data; suspend/resume uses data snapshot + dsh session-log resume (isomorphic to substrate `onResume: ColdBoot`), interface not degraded |

| # | 冲突点 | 处理 |
| --- | --- | --- |
| 1 | 架构 M1 写 gVisor 起步 vs 本 ADR process driver | **偏差已接受**（D2），接口按 4.4 全量实现，runsc 留 M3；能力位如实声明 |
| 2 | 架构 sidecar 经 vsock/UDS vs 本 ADR localhost HTTP | 传输载体差异，Sidecar HTTP 契约不变（4.5）；vsock 在 VM driver 下启用 |
| 3 | 架构 LLM Relay 在 sidecar 内持凭证 vs 密钥不进沙箱原则 | 拆为沙箱跳板 + 宿主 SecretRelay（D4），原则（密钥不进沙箱）优先，符合 6.2 |
| 4 | 架构事件日志入专门日志系统 vs M1 JSONL | 按 10.3 provider 形态：M1 EventLog = 本地 JSONL，接口不变 |
| 5 | 架构未覆盖 MCP Gateway / CLI / 镜像库 | 本 ADR D6–D8 补齐，MCP 保持兜底定位（8.2） |
| 6 | 架构 snapshot_full 期望（firecracker/runsc） | process driver 仅声明 snapshot_data；suspend/resume 走 data 快照 + dsh 会话日志续播（substrate `onResume: ColdBoot` 同构），接口不降级 |

Everything else (AgentSession as the sole scheduling unit, five-layer contract stays within bounds, ensure step chain, atomic claim, seq idempotency,
event schema, lease/epoch to prevent split-brain semantics) is implemented consistently with the architecture.
其余（AgentSession 唯一调度单元、五层契约不越界、ensure 步骤链、原子认领、seq 幂等、
事件 schema、租约/epoch 防脑裂语义）均与架构一致实现。

## 9. Implementation Order (minimal closed-loop commit sequence)
## 9. 实施顺序（最小闭环提交序列）

Each commit is independently testable and reviewable; unit tests ship with the code, and benchmarks follow the corresponding module commit:
每个提交独立可测、可 review；单测随代码同提交，benchmark 在对应模块提交后紧跟：

1. the ADR itself + repo scaffolding (pyproject / package skeleton / pytest config)
1. ADR 本身 + 仓库脚手架（pyproject / 包骨架 / pytest 配置）
2. core domain model + state machine + ids/errors (unit)
2. core 领域模型 + 状态机 + ids/errors（unit）
3. storage five providers + in-memory/file implementations (integration)
3. storage 五 provider + 内存/文件实现（integration）
4. bus + EventLog JSONL (integration)
4. bus + EventLog JSONL（integration）
5. timer hierarchical wheel + cron parsing (unit + benchmark)
5. timer 层次时间轮 + cron 解析（unit + benchmark）
6. seam model + Renderer (unit)
6. seam 模型 + Renderer（unit）
7. drivers process driver + echo harness + hostlet base lifecycle (integration)
7. drivers process driver + echo harness + hostlet 基础生命周期（integration）
8. imaging LocalRegistry + pip builder (integration, real install)
8. imaging LocalRegistry + pip 构建器（integration，真实安装）
9. agent SandboxAgent (tap/control/relay jump) + hostlet SecretRelay (integration, echo harness)
9. agent SandboxAgent（tap/control/relay 跳板）+ hostlet SecretRelay（integration，echo harness）
10. dsh harness adapter + dsh image build (integration: llm-replay recorded replay, no network)
10. dsh harness adapter + dsh 镜像构建（integration：llm-replay 录制回放，不触网）
11. control: SessionManager / Scheduler / Pool / Lifecycle ensure chain (integration)
11. control：SessionManager / Scheduler / Pool / Lifecycle ensure 链（integration）
12. gateway REST + SSE + MCP gateway + CLI (integration)
12. gateway REST + SSE + MCP gateway + CLI（integration）
13. cron + keepalive wired into the timing wheel (integration)
13. cron + keepalive 接入时间轮（integration）
14. snapshot data + suspend/resume (dsh session resume) (integration)
14. snapshot data + suspend/resume（dsh 会话续播）（integration）
15. e2e: full acceptance against real DeepSeek + benchmark archive + README quick start
15. e2e：真实 DeepSeek 的完整验收 + benchmark 存档 + README 快速上手

## 10. Risks & Open Points
## 10. 风险与开放点

- The stdio protocol details of dsh's `dsh-jsonrpc-agent` follow the SDK source (`python/sdk`); if the protocol offers no version commitment,
  the adapter pins the verified SDK version range, and upgrades go through image rebuilds (versioned images are naturally isolated).
- dsh `dsh-jsonrpc-agent` 的 stdio 协议细节以 SDK 源码为准（`python/sdk`）；如协议无版本承诺，
  adapter 内钉住已验证的 SDK 版本区间，升级走镜像重建（版本化镜像天然隔离）。
- The process driver's isolation strength is limited (honestly declared); the multi-tenant production form must be runsc/firecracker (M3).
- process driver 隔离强度有限（如实声明）；多租户生产形态必须 runsc/firecracker（M3）。
- DeepSeek API network latency dominates the e2e benchmarks; use only for regression comparison, no hard acceptance line.
- DeepSeek API 网络延迟支配 e2e 基准，只做回归对比、不设硬验收线。