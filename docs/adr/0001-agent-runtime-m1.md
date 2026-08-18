# ADR-0001: Argus Agent Runtime — M1 竖切详细设计与实施计划

- 状态：已接受
- 日期：2026-08-18
- 关联：[agent-runtime-architecture.md](../../agent-runtime-architecture.md)（v0.6 架构草案）
- 范围：M1（单节点竖切）+ M2（生命周期完整），含架构文档未覆盖的 MCP Gateway / CLI / 镜像库设计

---

## 1. 背景与目标

架构文档已给出五层结构与模块职责。本 ADR 将其细化为可实施的工程决策：包结构、接口签名、
领域模型的 Python 形态、测试与 benchmark 验收标准，并显式记录与架构文档的偏差与理由。

**M1 验收目标**：单进程 all-in-one 模式下，原生 DeepSeek Harness（dsh）镜像挂载进 agent runtime，
通过 REST API 创建 agent / 会话 / 发起 turn，事件经 SSE 流式返回，LLM 调用经 relay 出网且凭证不进沙箱，
skill / seam（tool）配置可注入 agent 并生效。

**已验证的可行性**（2026-08-18，本仓库开发沙箱内；验收 e2e 复验于同日）：

- dsh Python 侧分发为两个源码包：`python/sdk`（`deepseek-harness-sdk`）与 `python/sdk-runtime`
  （`deepseek-harness-runtime-bin`，承载平台单文件 exe）。**两者均不在 PyPI**——镜像构建从本地
  checkout 安装（`refs/deepseek-harness`，可用 `ARGUS_DSH_REPO` 覆盖）；exe 由仓库脚本
  `pnpm exec tsx scripts/build-exe-for-python-sdk.ts` 构建（macOS arm64 / linux x64+arm64）。
- dsh 运行时 + 真实 DeepSeek API 端到端跑通（`h.run("Reply with exactly: OK")` → `OK` / `completed`）。
- dsh 依赖环境变量 `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY`，与 sidecar LLM Relay 设计天然契合。
  注意：llm-deepseek 适配器要求环境里存在 API key 才发请求——沙箱内注入非密钥占位符
  `DEEPSEEK_API_KEY=argus-relay`，真实凭证仅存在于 Hostlet，由 SecretRelay 在出网时替换
  Authorization 头（占位符永不到达上游）。
- LLM 流量为 SSE（`text/event-stream`）：agent 与 Hostlet 两跳 relay 均为未缓冲字节流透传。

## 2. 关键决策

### D1 语言与运行时：Python 3.12+ / asyncio / FastAPI

与架构 8.2「控制面技术栈」一致。约束：

- 依赖最小化：`fastapi`、`uvicorn`、`httpx`、`pydantic>=2`。测试：`pytest`、`pytest-asyncio`。benchmark：`pytest-benchmark`。
- 不引入 Redis/Postgres/NATS 客户端作为 M1 依赖——M1 全部走 provider 内存实现。
- macOS（M 芯片）与 Linux 双平台可运行：路径用 `pathlib`，进程用 `asyncio.subprocess`，
  不使用 Linux-only 系统调用；平台差异封装在 driver 层。

### D2 沙箱底座：M1 默认 `process` driver，而非 gVisor

**与架构文档的偏差**（架构 M1 写「单 SandboxClass（gVisor 起步）」）。理由：

1. gVisor（runsc）仅 Linux 可用，与「M 芯片 Mac 本地开发测试亲和」直接冲突；
2. 架构 4.4 已确立「能力位是调度唯一依据、class 只是标签」的原则，`Process` 本就是
   `Isolation` 枚举的合法取值（dsh 的 `sandbox-local`、substrate 的进程内 actor 同为进程级隔离先例）；
3. `SandboxDriver` 接口按架构 4.4 完整实现（caps 声明、create/exec/pause/checkpoint/restore/destroy），
   `process` driver 如实声明 `isolation=Process, snapshot_full=False, snapshot_data=True, density=High, net_policy=False`。
   runsc/firecracker driver 按同一接口在 M3（Linux 集群形态）加入，调度器零改动。

**隔离边界（process driver，如实声明）**：独立进程组 + 独立工作区目录（cwd 限定 workspace root）+
环境变量白名单（不继承宿主任意 env）+ LLM 出站仅经 relay URL 注入。这是配置级隔离而非内核级隔离，
caps 如实上报，租户侧可见（不强于声明）。

### D3 dsh 集成形态：JSON-RPC stdio 协议 + 会话 JSONL 事件面

研究 dsh 源码（`packages/core/session`、`python/sdk`）后确定两个集成面：

1. **控制面**：dsh 运行时暴露 stdio JSON-RPC（`session_prompt` / `session_end` / notifications）。
   SandboxAgent（沙箱内首进程）持有该 stdio，对外暴露 HTTP。
2. **事件面**：dsh 的 session-persistence 将 `SessionEvent{type, seq, time, data, surface}` 追加写为
   JSONL。EventTap tail 该文件，按 dsh 事件词表归一化为平台事件（`turn/start`、`assistant/chunk`、
   `tool/call`、`tool/result`、`turn/end`），与架构 4.5 的事件 schema 对齐。

「镜像」定义（见 D6 镜像库）：dsh 镜像 = 一个自包含的运行时目录（venv + `deepseek-harness-sdk`
+ 启动器），对 runtime 而言是 `image ref` 解析出的 bundle，与 OCI 镜像在接口上等价。

### D4 Sidecar 落位：SandboxAgent（沙箱内）+ Hostlet 内的 SecretRelay（宿主侧）

架构 4.5 的五件套在 process driver 下的映射：

| 架构职责 | M1 实现 | 位置 |
| --- | --- | --- |
| EventTap | tail dsh 会话 JSONL → 归一化 → POST 到 Hostlet | SandboxAgent（沙箱内进程） |
| ControlAgent | HTTP `/health` `/turn` `/prepare_pause` `/stop` | SandboxAgent |
| ResourceInjector | 读取注入清单，落盘 workspace、装配 harness 启动配置 | SandboxAgent 启动阶段 |
| LLM Relay | `/relay/llm/*` 无密钥转发 → Hostlet SecretRelay 附加密钥出网 | SandboxAgent（跳板）+ Hostlet（持密钥） |
| TraceRelay | 结构化 span 经事件通道带出（M1 简化为归因字段） | SandboxAgent |

密钥不进沙箱：dsh 进程环境只有 `DEEPSEEK_BASE_URL=http://127.0.0.1:{agent_port}/relay/llm`，
真实 `DEEPSEEK_API_KEY` 仅存在于 Hostlet 进程。沙箱边界（进程组）之外。**这是真实隔离而非声明式**：
密钥既不在沙箱 env、也不在 workspace、也不在快照中。

### D5 Seam 三层抽象（面向不同能力用户）

```
高级抽象（用户可见）      SkillPackage / ToolDecl / MemoryBinding   —— 平台概念，版本化、可审计
中间格式（Renderer 产物） SeamBinding{seam, provider, policy, consumers[]}  —— 平台中立契约
低层实现（harness 侧）    dsh cordis 配置 / MCP Server / 进程内 provider —— harness 原生形态
```

Seam IDL 对齐 dsh 三角色：`Definition`（如 `fs.v1`、`shell.v1`、`web.v1`、`memory.v1`）注册于
Resource Registry；`Provider` 声明实现（`sandbox-fs` 等）与策略（`mode: read-only|workspace-write|full`）；
`Consumer` 声明暴露方式（`harness: dsh` 原生映射 / `harness: "*"` 兜底 MCP）。
Renderer 输入 AgentVersion 的 `seamBindings[]` + `skillRefs[]`，输出注入清单（JSON 文件，随 Bind 落入沙箱）。

M1 内置 seam 集（对齐 dsh 能力，全部真实生效）：`fs.v1`、`shell.v1`、`web.v1`（dsh 原生 consumer）、
`memory.v1`（workspace 内持久目录）。Skill 注入：skill 包解包至 workspace `.argus/skills/`，
dsh 侧通过 filesystem skill provider 装载（dsh `skill-filesystem` 机制），模型可调用。

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

M1 实现 `LocalRegistry`（目录型）：`images/{name}/{version}/` + `manifest.json`。
`pip` 构建器：在镜像目录创建 venv 并 `pip install`（dsh 镜像 = `deepseek-harness-sdk`）——
构建即真实安装，非缓存伪造。集群形态的 OCI 实现留接口位（`pull` + unpack），M1 不实现不声明。

### D7 MCP Gateway（架构未覆盖，新增设计）

架构 8.2 定位 MCP 为兜底 Consumer。M1 落地：Gateway 挂载 `/mcp/{agent_version}/` 端点，
实现 MCP Streamable HTTP 子集（`initialize` / `tools/list` / `tools/call`），
把该 AgentVersion 渲染出的 SeamBindings 中 `consumuers: ["*"]` 的 seam 暴露为 MCP tools。
非 dsh harness（及外部 IDE）经此接入，**不进入**调度热路径。

### D8 CLI（架构未覆盖，新增设计）

`argus` 命令（`python -m argus.cli` / console_script），纯 httpx 客户端：

```
argus serve [--port] [--data-dir]          # 启动 all-in-one
argus image build dsh                       # 构建并登记 dsh 镜像
argus agent create ./agent.yaml / list
argus session create <agent> / send <sid> <text> --stream / events <sid>
argus cron add <agent> --schedule '*/5 * * * *' --input '...'
```

### D9 时间轮（Kafka 式，独立封装）

`argus/timer/`：层次时间轮。单层 512 槽、tick 20ms；溢出任务入上层轮；`advance()` 由
单调时钟驱动（事件循环内自驱，不依赖信号）。接口：

```python
class TimingWheel:
    def schedule(self, delay: float, cb: Callable[[], Awaitable]) -> TimerHandle
    def schedule_at(self, deadline: float, cb) -> TimerHandle   # 绝对期限（keepalive 用）
```

Cron Scheduler（G3）与 Keepalive Manager（G4）共用 TimingWheel 与进程内锁，业务互不感知。
Cron 表达式解析自实现（5 字段标准语义），不引入依赖。

### D10 存储与通信 provider（对齐架构 10.3）

五个 provider 接口（`MetadataStore / KVStore / EventBus / ObjectStore / EventLog`）+ M1 实现：
内存 dict / asyncio.Queue / 本地目录 / JSONL。SQLite 持久化选项 M2 加。全部模块只依赖接口。

## 3. 包结构

```
src/argus/
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
  cli/         # argus 命令
  runtime.py   # all-in-one 装配（provider 注入、模块接线）
tests/
  unit/        # 纯逻辑（时间轮、cron、seam renderer、状态机、调度打分）
  integration/ # 真实子进程 / 真实文件 / 真实本地 HTTP
  e2e/         # 真实 dsh + 真实 DeepSeek API（env ARGUS_E2E=1 开关，需密钥）
  benchmark/   # pytest-benchmark
```

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

状态机照架构图 5/6 实现：`Created → Dispatching → Running → Idle → Suspending → Suspended →
Resuming → Running → Closed`；转换非法即抛 `InvalidTransition`（单测覆盖全矩阵）。

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

## 6. 测试策略（禁止 mock / 伪造 / 作弊）

原则：**测试对象必须真实执行**——真实子进程、真实文件系统、真实本地 socket/HTTP。
允许的替身仅限「测试用途的第二实现」（如 echo harness 是真实的极简 harness，用于驱动真实进程路径；
llm-replay 同思路：dsh 官方自带 replay 适配器，回放录制流量，用于无密钥的 dsh 集成测试）。

| 层 | 内容 | 真实性 |
| --- | --- | --- |
| unit | 时间轮（顺序/取消/溢出/单调性）、cron 解析、seam renderer、状态机矩阵、调度打分、seq 分配 | 纯逻辑，无需替身 |
| integration | provider（真实文件/目录）、process driver + echo harness（真实子进程）、SandboxAgent HTTP、Hostlet 生命周期、Scheduler CAS、Gateway REST/SSE（真实 uvicorn 端口）、MCP gateway、CLI（真实本地服务） | 全真实本地资源 |
| e2e | 真实 dsh 镜像构建（pip install）→ 挂载 → turn → SSE 事件断言 → 工具调用真实发生 → 快照恢复续话 | 真实 DeepSeek API |
| benchmark | 见 §7 | 真实执行计时 |

无密钥 CI 路径：`ARGUS_E2E=1` 才跑 e2e；integration 层不触网（LLM relay 转发到本地起的真实
回放 HTTP 服务——服务本身真实运行，回放数据是 dsh 官方 llm-replay 格式的录制样本）。

## 7. Benchmark 与性能验收

`pytest tests/benchmark/`（pytest-benchmark，真实计时）：

| 基准 | 指标 | M1 验收线 |
| --- | --- | --- |
| 时间轮 vs heapq 基线 | 10k 任务调度+取消吞吐 | 时间轮 ≥ heapq 的 80%（同机对比），且取消 O(1) 优势场景 ≥ 3x |
| EventBus 扇出 | 10k 事件 / 10 订阅者 | ≥ 20k events/s |
| EventLog append + 回放 | JSONL 写入与按 seq 读取 | ≥ 5k events/s 写入 |
| Scheduler 冷启动决策 | 无沙箱 → ensure 完成 | echo harness ≤ 250ms（进程真实启动） |
| warm 认领 | CAS bind 决策 | p50 ≤ 2ms |
| E2E turn（dsh + DeepSeek） | POST → 首事件到达 | 记录值，无硬线（受网络支配），回归对比用 |

基准结果在 CI 输出存档（`benchmark/` 结果 JSON），防止性能劣化无感知。

## 8. 与架构文档的冲突检查（结论）

| # | 冲突点 | 处理 |
| --- | --- | --- |
| 1 | 架构 M1 写 gVisor 起步 vs 本 ADR process driver | **偏差已接受**（D2），接口按 4.4 全量实现，runsc 留 M3；能力位如实声明 |
| 2 | 架构 sidecar 经 vsock/UDS vs 本 ADR localhost HTTP | 传输载体差异，Sidecar HTTP 契约不变（4.5）；vsock 在 VM driver 下启用 |
| 3 | 架构 LLM Relay 在 sidecar 内持凭证 vs 密钥不进沙箱原则 | 拆为沙箱跳板 + 宿主 SecretRelay（D4），原则（密钥不进沙箱）优先，符合 6.2 |
| 4 | 架构事件日志入专门日志系统 vs M1 JSONL | 按 10.3 provider 形态：M1 EventLog = 本地 JSONL，接口不变 |
| 5 | 架构未覆盖 MCP Gateway / CLI / 镜像库 | 本 ADR D6–D8 补齐，MCP 保持兜底定位（8.2） |
| 6 | 架构 snapshot_full 期望（firecracker/runsc） | process driver 仅声明 snapshot_data；suspend/resume 走 data 快照 + dsh 会话日志续播（substrate `onResume: ColdBoot` 同构），接口不降级 |

其余（AgentSession 唯一调度单元、五层契约不越界、ensure 步骤链、原子认领、seq 幂等、
事件 schema、租约/epoch 防脑裂语义）均与架构一致实现。

## 9. 实施顺序（最小闭环提交序列）

每个提交独立可测、可 review；单测随代码同提交，benchmark 在对应模块提交后紧跟：

1. ADR 本身 + 仓库脚手架（pyproject / 包骨架 / pytest 配置）
2. core 领域模型 + 状态机 + ids/errors（unit）
3. storage 五 provider + 内存/文件实现（integration）
4. bus + EventLog JSONL（integration）
5. timer 层次时间轮 + cron 解析（unit + benchmark）
6. seam 模型 + Renderer（unit）
7. drivers process driver + echo harness + hostlet 基础生命周期（integration）
8. imaging LocalRegistry + pip 构建器（integration，真实安装）
9. agent SandboxAgent（tap/control/relay 跳板）+ hostlet SecretRelay（integration，echo harness）
10. dsh harness adapter + dsh 镜像构建（integration：llm-replay 录制回放，不触网）
11. control：SessionManager / Scheduler / Pool / Lifecycle ensure 链（integration）
12. gateway REST + SSE + MCP gateway + CLI（integration）
13. cron + keepalive 接入时间轮（integration）
14. snapshot data + suspend/resume（dsh 会话续播）（integration）
15. e2e：真实 DeepSeek 的完整验收 + benchmark 存档 + README 快速上手

## 10. 风险与开放点

- dsh `dsh-jsonrpc-agent` 的 stdio 协议细节以 SDK 源码为准（`python/sdk`）；如协议无版本承诺，
  adapter 内钉住已验证的 SDK 版本区间，升级走镜像重建（版本化镜像天然隔离）。
- process driver 隔离强度有限（如实声明）；多租户生产形态必须 runsc/firecracker（M3）。
- DeepSeek API 网络延迟支配 e2e 基准，只做回归对比、不设硬验收线。
