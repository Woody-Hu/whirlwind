# ADR-0014: MCP Gateway completeness — the internal MCP closed loop

# ADR-0014：MCP Gateway 完整性——内部 MCP 闭环

- Status: Accepted
- Date: 2026-08-20
- Related: [ADR-0001 D7](0001-agent-runtime-m1.md)（MCP 兜底 Consumer 的 M1 子集落位）、[ADR-0011](0011-seam-templates-and-harness-bundles.md)（Seam 模板/实例 + Harness 组合，MCP 工具的声明来源）、[ADR-0009](0009-unified-config.md)（统一配置：本次新增 session/超时可调值走该管线）、[ADR-0013](0013-observability.md)（可观测接缝，MCP 请求计入同一请求路径）、架构文档 §8.2（MCP 面向 harness 与外部 IDE 的标准化接入面）

- 状态：已接受
- 日期：2026-08-20
- 关联：[ADR-0001 D7](0001-agent-runtime-m1.md)（MCP 兜底 Consumer 的 M1 子集落位）、[ADR-0011](0011-seam-templates-and-harness-bundles.md)（Seam 模板/实例 + Harness 组合，MCP 工具的声明来源）、[ADR-0009](0009-unified-config.md)（统一配置：本次新增 session/超时可调值走该管线）、[ADR-0013](0013-observability.md)（可观测接缝，MCP 请求计入同一请求路径）、架构文档 §8.2（MCP 面向 harness 与外部 IDE 的标准化接入面）

---

## Context

## 背景

**English.** The host's MCP face (ADR-0001 D7) was shipped as an M1 subset: only
`initialize` / `tools/list` / `tools/call` on a single POST route, a hard-coded
`mcp-session-id: whirlwind-m1` response header, no handshake state, no liveness
probe, no notifications handling, and no resources subsystem. A real MCP client
(such as the official TypeScript/Python SDK, an IDE, or a non-dsh harness) will
send `ping`, `notifications/initialized`, batch arrays, and a proper
per-connection session id it received from `initialize` and expects to echo back;
today those either error or are silently ignored. The roadmap item P4.4 names
exactly this: "MCP gateway completeness: ping, notifications/initialized,
resources subsystem, per-connection session ids (current: M1 subset —
initialize/tools only)". This ADR closes the loop so the MCP face becomes a
usable, production-grade internal gateway that any standards-compliant client —
and our own in-sandbox harness — can drive.

**中文.** 宿主侧 MCP 面（ADR-0001 D7）当初只以 M1 子集交付：单一 POST 路由上只有
`initialize` / `tools/list` / `tools/call`、响应头写死 `mcp-session-id: whirlwind-m1`、无握手状态、
无存活探测、无通知处理、也无 resources 子系统。真实 MCP 客户端（官方 TypeScript/Python SDK、
IDE、非 dsh harness）会发送 `ping`、`notifications/initialized`、批量数组，以及它在
`initialize` 里拿到的、期望后续回显的 per-connection session id——今天这些东西要么报错，
要么被静默忽略。路线图 P4.4 正是此条：「MCP gateway 完整性：ping、notifications/initialized、
resources 子系统、per-connection session ids（当前为 M1 子集——仅 initialize/tools）」。本
ADR 关闭这个闭环，让 MCP 面成为一个可用的、生产级的内部网关，任何规范合规客户端——包括我们
自家沙箱内的 harness——都能驱动它。

## Decision — D1: per-connection MCP session lifecycle

## 决策——D1：per-connection 的 MCP 会话生命周期

**English.** The gateway maintains a real, in-memory session registry keyed by an
opaque per-connection session id it mints (instead of the hard-coded
`whirlwind-m1`). `initialize` creates a session, returns a fresh `Mcp-Session-Id`
response header, and marks the session `initialized`. Every subsequent request
must present the same `Mcp-Session-Id` request header; a request with an unknown
or absent session id is rejected with JSON-RPC error `-32000` (server error) so a
client that never completed the handshake cannot be dropped into a stateful tool
call. Sessions are stateful on the host side only — they track the negotiating
protocol version + client info so the server can adapt, and an idle timeout
(default 10 min, configurable) reaps abandoned sessions so the registry cannot
grown unbounded. A `DELETE` on the MCP route terminates the session.

How this stays honest: session bookkeeping is pure host-side memory (a
`dict[str, McpSession]` guarded by an `asyncio.Lock`), it never touches the
MetadataStore and does not cross into the scheduling hot path — the MCP face
remains a fallback consumer, consistent with ADR-0001 D7.

**中文.** 网关维护一个真实的、内存内的会话注册表，以它自己铸造的不透明 per-connection session id
为键（取代写死的 `whirlwind-m1`）。`initialize` 创建会话、返回全新的 `Mcp-Session-Id` 响应头、
并把会话标记为 `initialized`。其后的每个请求都必须携带同一个 `Mcp-Session-Id` 请求头；session id
未知或缺失的请求以 JSON-RPC 错误 `-32000`（server error）拒绝，从而保证没完成握手的客户端不会
被丢进一个有状态的工具调用。会话仅在宿主侧有状态——记录协商出的协议版本 + 客户端信息，以便服务端
适配；闲置超时（默认 10 分钟，可配）回收遗弃会话，防止注册表无界增长。`DELETE` MCP 路由即终止会话。

诚实性：会话簿记纯粹是宿主机内存（一个由 `asyncio.Lock` 守护的 `dict[str, McpSession]`），
不触碰 MetadataStore、不进入调度热路径——MCP 面仍保持兜底 Consumer 定位，与 ADR-0001 D7 一致。

## Decision — D2: protocol-depth completeness — ping, notifications, batching

## 决策——D2：协议深度完备——ping、通知、批处理

**English.** Beyond the existing request methods, the dispatcher gains:

- `ping` — liveness probe requesting an empty `result: {}`; both directions are
  valid per the spec, the host answers a client `ping`. (If the server later
  needs to ping a long-lived stream, the same empty-result shape is reused.)
- `notifications/initialized` — a JSON-RPC notification (no `id`). The host
  acknowledges it (200 with an empty/resolved body) and records that the client
  considers the handshake complete; notifications are never dispatched to tool
  execution.
- **Batching** — a JSON-RPC array body. Each element that is a proper request is
  dispatched; notifications have no `id` and produce no response entry; invalid
  elements yield a per-element error. The response is a parallel array preserving
  order, so a client can run the standard `initialize` → `notifications/initialized`
  → (`tools/list` | `tools/call` …) handshake in fewer round-trips without hitting
  the "must be an object" parse error that a batch currently triggers.

Notifications with an `id` (i.e. a client wrongly treating a notification as a
request) follow JSON-RPC: ignored for responses — never dispatched — to keep the
request/response semantics unambiguous.

**中文.** 在既有请求方法之上，分发器新增：

- `ping` ——存活探测，要求返回空 `result: {}`；规范里双向都合法，宿主应答客户端的 `ping`
  （将来宿主需要 ping 长连接流时复用同一空-result 形态）。
- `notifications/initialized` ——无 `id` 的 JSON-RPC 通知。宿主以 200 + 空/已解析 body 应答，
  并记录客户端认为握手已完成；通知永不派发给工具执行。
- **批处理** ——JSON-RPC 数组 body。每个是合法请求的元素被分发；通知无 `id`、不产生响应条目；
  非法元素产生逐元素错误。响应是保持顺序的并行数组，让客户端能用更少往返完成标准的
  `initialize` → `notifications/initialized` →（`tools/list` | `tools/call` …）握手，而不会撞上
  当前「必须是对象」的 parse 错误。

带 `id` 的通知（即客户端把通知误当请求）遵循 JSON-RPC：不产生响应、永不派发，保持
请求/响应语义无歧义。

## Decision — D3: resources subsystem

## 决策——D3：resources 子系统

**English.** MCP defines three client-visible primitives — tools, resources,
prompts. Tools are already exposed; this ADR adds the **resources** subsystem as
a read-only window onto a version's state that a client can fetch ahead of a call
(for a code-reading harness, context assembly, or external IDE). `resources/list`
returns URIs of the form `whirlwind://{version_id}/memory` (the session memory
note-store already produced by the builtin `memory.v1`) and
`whirlwind://{version_id}/manifest` (the rendered injection manifest for that
version). `resources/read` resolves a URI back onto real data through the same
confinement rules as tool execution and returns an `mcp.resource` text part.
Capabilities advertised in `initialize` gain `"resources": {"subscribe": false}`,
honest about the read-only, non-subscribing surface. Unknown URIs yield a
JSON-RPC invalid-params error. No new data mutation path is introduced — resources
are a projection of stores the runtime already owns.

**中文.** MCP 定义三类客户端可见原语——tools、resources、prompts。tools 已暴露；本 ADR 新增
**resources** 子系统，作为某版本状态的只读窗口，客户端可在调用前抓取（供读代码的 harness、
上下文组装、外部 IDE）。`resources/list` 返回形如 `whirlwind://{version_id}/memory`
（内建 `memory.v1` 已产生的会话记忆 note-store）与 `whirlwind://{version_id}/manifest`
（该版本渲染出的注入清单）的 URI。`resources/read` 按与工具执行相同的限域规则把 URI 解析回真实
数据，返回一个 `mcp.resource` 文本部分。`initialize` 宣告的能力新增 `"resources": {"subscribe": false}`，
忠实反映只读、不订阅的面。未知 URI 返回 JSON-RPC invalid-params 错误。不引入任何新的数据变更
路径——resources 只是运行时已拥有 store 的投影。

## Decision — D4: honest capability advertisement & protocol negotiation

## 决策——D4：诚实的能力宣告与协议协商

**English.** `initialize` returns a genuine `protocolVersion` that the server
supports, with the spec's major-version negotiation: the server answers with the
highest version it supports when the client asks for a compatible one, and refuses
with a clearly-labelled error when the requested version is outside its supported
set. `capabilities` advertise only what the server truthfully implements:
`"tools"`, `"resources"` (D3). Prompts, sampling, roots, logging, and resource
subscriptions are **not** advertised — honest capability declaration follows the
repo-wide "declare-what-you-can-honestly-do" rule (ADR-0001 D2, the `Caps` ethos in
`drivers/base.py`). `serverInfo` gains a stable `version`.

**中文.** `initialize` 返回一个真实受支持的 `protocolVersion`，并遵循规范的主版本协商：当客户端请求一个
兼容版本时，服务端用它所支持的最高版本应答；当请求版本超出支持集合时，以清晰标注的错误拒绝。
`capabilities` 只宣告服务器确实实现的项：`"tools"`、`"resources"`（D3）。prompts、sampling、roots、
logging、resources 订阅一律**不**宣告——诚实能力申报遵循全仓库「声明即执行」规则（ADR-0001 D2、
`drivers/base.py` 的 `Caps` 精神）。`serverInfo` 携带稳定 `version`。

---

## Detailed design

## 详细设计

### Session registry

### 会话注册表

```python
@dataclass
class McpSession:
    id: str                 # secrets.token_urlsafe(24)
    created_at: float       # time.monotonic()
    last_seen: float        # for idle reaping
    protocol_version: str
    client_name: str = ""
    client_version: str = ""
    initialized_at: float | None = None
```

- Minted in `initialize`; the response body is unchanged in shape, and the minted
  id is returned only via the `Mcp-Session-Id` response header (the spec's channel).
- A per-`version_id` registry means one client can hold a session per version
  independently; the gateway instantiates one `McpSessionManager` per `version_id`
  (or keys the single registry by `(version_id, session_id)`). The single-registry
  shape is chosen for simplicity: `dict[tuple[version_id, session], McpSession]`.
- Idle reaping runs opportunistically on each request under the lock (check expired
  entries while sweeping) plus a background task is unnecessary at this scale —
  an admission-side sweep keeps complexity down and honesty up.

### Request handling pipeline

### 请求处理管线

`handle(version_id, body)` becomes a two-level dispatch:

1. **Transport/shape level**: parse JSON; split single-object vs batch array; extract
   the `Mcp-Session-Id` request header (done by the FastAPI route and passed in).
2. **Session gate**: for batch/dispatchable elements whose method is a *stateful*
   tool call, require a valid session present in the registry; `initialize` is the
   one method that mints. Notifications and `ping` are session-tolerant.
3. **Method dispatch**: `initialize` / `notifications/initialized` / `ping` /
   `tools/list` / `tools/call` / `resources/list` / `resources/read`.

### Files

### 文件

- `src/whirlwind/gateway/mcp.py` — extended: `McpSession`, `McpSessionManager`,
  resources handlers on `WorkspaceToolExecutor`/`McpGateway`, batch + notification
  + ping + session-gate dispatch.
- `src/whirlwind/runtime.py` — construct the manager, wire into `McpGateway`,
  pass idle-timeout from `RuntimeConfig`.
- `src/whirlwind/config.py` — new `[mcp]` section: `session_idle_timeout_s = 600`.
- `src/whirlwind/gateway/app.py` — the `/mcp/{version_id}` route reads the
  `Mcp-Session-Id` request header, calls the widened `handle`, sets the response
  `Mcp-Session-Id` header from the session, and accepts `DELETE` to terminate.

---

## Testing strategy

## 测试策略

- **Integration** (`tests/integration/test_gateway.py`, real uvicorn + httpx):
  - full handshake `initialize` (mint session id in response header) →
    `notifications/initialized` → `ping` → `tools/list` → `tools/call` all succeed
    with the echoed session id;
  - stateful `tools/call` without a session id (or with an unknown one) is rejected
    with JSON-RPC server error, not silently executed;
  - batch array: `[initialize, notifications/initialized, tools/list]` in one POST
    returns three ordered entries, notifications carrying no id emit no response
    entry;
  - `resources/list` returns the memory + manifest URIs; `resources/read` on the
    memory URI returns real persisted notes through the same executor confinement;
    an unknown URI is an invalid-params error;
  - `DELETE` terminates the session; subsequent calls with that id fail;
  - capability advertisement: `initialize` lists only `tools`/`resources`, honest.
- **Benchmark** (`tests/benchmark/test_mcp_bench.py`, real measurement via the
  runner, ADR-0008): `tools/call` happy-path throughput and the session-gate
  overhead have an honest floor (e.g. ≥1k calls/s in this environment); the gate
  does not measurably degrade the existing M1 happy path. Baselines recorded in the
  session-log, not transcribed.

## 中文

- **集成**（`tests/integration/test_gateway.py`，真实 uvicorn + httpx）：
  - 完整握手 `initialize`（响应头铸造 session id）→ `notifications/initialized` →
    `ping` → `tools/list` → `tools/call` 在回显 session id 下全部成功；
  - 无 session id（或未知 id）的有状态 `tools/call` 被 JSON-RPC server error 拒绝而非
    静默执行；
  - 批量数组：一次 POST 内 `[initialize, notifications/initialized, tools/list]` 返回三条
    有序条目，无 id 的通知不产生响应条目；
  - `resources/list` 返回 memory + manifest URI；对 memory URI 的 `resources/read` 经与工具
    相同的 executor 限域返回真实持久化 notes；未知 URI 是 invalid-params 错误；
  - `DELETE` 终止会话；其后携带该 id 的调用失败；
  - 能力宣告：`initialize` 只列 `tools`/`resources`，诚实。
- **基准**（`tests/benchmark/test_mcp_bench.py`，经 runner 真实测量，ADR-0008）：
  `tools/call` 快乐路径吞吐与会话闸门开销有诚实下限（本环境 ≥1k 调用/秒）；闸门不显著劣化
  既有 M1 快乐路径。基线记录在 session-log，不转述。

## Conflict check with the architecture doc

## 与架构文档的冲突检查

No conflict. The MCP face stays an external, fallback Consumer (architecture §8.2,
ADR-0001 D7): it never enters the scheduling hot path and adds no new credential
surface (the executor and relay channel are unchanged). Session state is
host-side, in-memory, per-connection. The added `[mcp]` config follows the unified
pipeline (ADR-0009). No `sys.platform` branches introduced (ADR-0007).

无冲突。MCP 面仍保持外部兜底 Consumer 定位（架构 §8.2、ADR-0001 D7）：不进入调度热路径、
不新增凭证面（executor 与 relay 通道不变）。会话状态是宿主侧、内存内、per-connection。
新增的 `[mcp]` 配置遵循统一管线（ADR-0009）。未引入任何 `sys.platform` 分支（ADR-0007）。

## Implementation order & risks

## 实施顺序与风险

Four self-contained loops, each committed separately:
D1 session registry + lifecycle → D2 ping/notifications/batching + protocol
negotiation → D3 resources subsystem → D4 wiring (config + route + manager
assembly) + tests + benchmark. Open points: stateful results in streaming mode
(when the host later needs SSE responses scoped to a session), a background
reaper (admission-side sweep suffices now), and prompts/sampling — all blocked by
scope, not by this ADR.

四个自闭环、各自单独提交：D1 会话注册表 + 生命周期 → D2 ping/通知/批处理 + 协议协商 →
D3 resources 子系统 → D4 接线（配置 + 路由 + 管理器装配）+ 测试 + 基准。开放点：流式模式下的
有状态结果（将来宿主需要按会话限定 SSE 响应时）、后台回收器（当前准入侧清扫足够）、以及
prompts/sampling——均被范围所限，非本 ADR 阻塞。