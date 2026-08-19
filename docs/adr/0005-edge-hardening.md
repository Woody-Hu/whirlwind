# ADR-0005: Edge hardening — sandbox resource limits, session quotas, idempotency keys, k3s deployment

# ADR-0005：边缘加固——沙箱资源限制、会话配额、幂等键、k3s 部署

- Status: Accepted
- Date: 2026-08-19
- Related: [ADR-0001](0001-agent-runtime-m1.md) (seam architecture), [ADR-0004](0004-production-storage.md) (KV/locks as shared primitives), Architecture 3.1 G1, 6.2, [TODO](../TODO.md) P1.2–P1.4

- 状态：已接受
- 日期：2026-08-19
- 关联：[ADR-0001](0001-agent-runtime-m1.md)（接缝架构）、[ADR-0004](0004-production-storage.md)（KV/锁作为共享原语）、架构 3.1 G1、6.2、[TODO](../TODO.md) P1.2–P1.4

---

## Context

## 背景

The gateway has no resource gates: any client can create unbounded live sessions, every sandbox runs with host-unlimited memory/CPU/pids, and a retried POST (timeout → retry) executes twice. Direction stays production grade; authentication is out of scope this round (P1.1, needs a tenant dimension). This ADR lands the three non-auth gates of P1 plus a first k3s deployment form.

网关没有任何资源闸门：客户端可以无限创建活跃会话；每个沙箱以宿主机无限制的内存/CPU/pids 运行；重试的 POST（超时→重试）会执行两次。方向仍是生产级；认证不在本轮范围（P1.1，需要租户维度）。本 ADR 落地 P1 的三个非认证闸门与首个 k3s 部署形态。

## D1 Sandbox resource limits — `Resources` on `SandboxSpec`

## D1 沙箱资源限制——`SandboxSpec` 上的 `Resources`

```python
# drivers/base.py
@dataclass(frozen=True, slots=True)
class Resources:
    mem_limit_mb: int | None = None   # hard memory ceiling
    cpu_seconds: int | None = None    # total CPU-time budget (SIGXCPU at soft, SIGKILL at hard)
    pids_max: int | None = None       # fork-time process-count ceiling

@dataclass(slots=True)
class SandboxSpec:
    ...
    resources: Resources = Resources()   # default: no limits (behavior unchanged)
```

Fields chosen for **truthful cross-driver enforcement** — each maps to a mechanism both drivers really implement:

字段的选取标准是**可被两个 driver 真实兑现**——每个字段都映射到两个 driver 都真正实现的机制：

| Field / 字段 | ProcessDriver | RunscDriver |
|---|---|---|
| `mem_limit_mb` | `RLIMIT_AS` via `preexec_fn` (`resource.setrlimit`) | OCI `process.rlimits` `RLIMIT_AS` + `linux.resources.memory.limit` (cgroup-backed, the primary path) |
| `cpu_seconds` | `RLIMIT_CPU` (soft=hard) | OCI `process.rlimits` `RLIMIT_CPU` (gVisor implements rlimit CPU accounting) |
| `pids_max` | `RLIMIT_NPROC` | OCI `process.rlimits` `RLIMIT_NPROC` + `linux.resources.pids.limit` |

Known honesty caveats, documented rather than hidden: `RLIMIT_AS` caps *virtual* memory (CPython reserves more VA than RSS); `RLIMIT_NPROC` is a fork-time check against the *uid's* total process count, not the sandbox's alone; CPU *rate* limiting (quota/period, %-of-core) needs the cgroup cpu controller and is deferred to the tenant-quota round. Nothing is claimed beyond what the tests prove.

已知的诚实性注意事项（文档化而非隐藏）：`RLIMIT_AS` 限制的是*虚拟*内存（CPython 预留的 VA 大于 RSS）；`RLIMIT_NPROC` 是 fork 时对*该 uid* 总进程数的检查，并非仅沙箱自身；CPU *速率*限制（quota/period、核百分比）需要 cgroup cpu 控制器，推迟到租户配额轮。任何声称不超出测试能证明的范围。

**Wiring**: `HostletConfig.sandbox_resources: Resources | None = None` → copied into every `SandboxSpec` it builds. `RuntimeConfig.sandbox_resources` passes through. Default `None` = unchanged behavior; the CLI does not expose it yet (deployment-level setting).

**装配**：`HostletConfig.sandbox_resources: Resources | None = None` → 复制进它构建的每个 `SandboxSpec`。`RuntimeConfig.sandbox_resources` 透传。默认 `None` = 行为不变；CLI 暂不暴露（部署级设置）。

## D2 Concurrent session cap — backpressure at the control plane

## D2 并发会话上限——控制面的背压

```python
# core/errors.py
class QuotaExceeded(WhirlwindError):      # code "whirlwind/quota-exceeded" → HTTP 429
    ...

# control/manager.py
class SessionManager:
    def __init__(..., max_live_sessions: int | None = None) -> None: ...
```

`create_session` counts live sessions (`status != CLOSED`; suspended sessions hold snapshots and count) from the **MetadataStore — the existing source of truth** — under an in-process lock, and raises `QuotaExceeded` at the cap. Store-derived counting is self-healing (no counter drift after crashes); no new primitive is invented. Multi-process over-cap-by-race is accepted and documented (hardening belongs with the Redis counter + tenant dimension, P1.2 follow-up).

`create_session` 从 **MetadataStore（既有事实源）** 统计活跃会话数（`status != CLOSED`；挂起会话持有快照，计入），在进程内锁保护下达到上限即抛 `QuotaExceeded`。基于存储的计数自愈（崩溃后无计数漂移）；不发明新原语。多进程竞态导致的短暂超限被接受并文档化（加固属于 Redis 计数器 + 租户维度，P1.2 后续）。

`RuntimeConfig.max_live_sessions: int | None = None` + `whirlwind serve --max-live-sessions`. `None` = uncapped (unchanged).

`RuntimeConfig.max_live_sessions: int | None = None` + `whirlwind serve --max-live-sessions`。`None` = 不设限（不变）。

## D3 Idempotency keys — HTTP middleware over the KVStore seam

## D3 幂等键——基于 KVStore 接缝的 HTTP 中间件

```python
# gateway/idempotency.py
class IdempotencyMiddleware:              # pure ASGI middleware
    def __init__(self, app, kv: KVStore, *, ttl_s: float = 86400.0) -> None: ...
```

- Applies to `POST`/`DELETE` requests carrying an `Idempotency-Key` header; all others pass through untouched.
- Key scope: `idem:{method}:{path}:{key}` — a key reused on a different route is *not* replayed.
- Flow: read KV → replay stored response (status + body) on hit; `CAS(None → pending)` to claim → raced claims see `pending` and get `409`; handler result is stored with TTL; `5xx` deletes the key so the client can retry cleanly.
- The claim records a sha256 of the request body; replay with a different body → `422` (Stripe semantics: same key assumes same request).
- Lives behind the existing `KVStore` seam (ADR-0004): memory backend for single-process, Redis for multi-process, no new storage.
- `GatewayDeps.kv: KVStore | None = None`; the runtime passes its KV. Header sent while no KV is wired → `400` (honest, not silently ignored).

- 仅作用于携带 `Idempotency-Key` 头的 `POST`/`DELETE` 请求；其余一律直通。
- 键作用域：`idem:{method}:{path}:{key}`——同一键换路由*不会*被重放。
- 流程：读 KV → 命中则重放已存响应（状态码+体）；`CAS(None → pending)` 认领 → 竞争认领者看到 `pending` 得到 `409`；handler 结果带 TTL 存储；`5xx` 删除键以便客户端干净重试。
- 认领时记录请求体的 sha256；同键不同体重放 → `422`（Stripe 语义：同键视为同请求）。
- 落在既有 `KVStore` 接缝之后（ADR-0004）：单进程用 memory、多进程用 Redis，不新增存储。
- `GatewayDeps.kv: KVStore | None = None`；运行时传入自己的 KV。未接 KV 却发头 → `400`（诚实报错，不静默忽略）。

SSE streams are GETs and unaffected. Replay of a turn response returns the original `message_id` — the exact retry-safety a timed-out client needs.

SSE 流是 GET，不受影响。重放 turn 响应返回原 `message_id`——正是超时客户端所需的重试安全。

## D4 k3s deployment form

## D4 k3s 部署形态

Single-node k3s on the dev sandbox (no systemd → `k3s server` as a plain process; containerd via `k3s ctr`). The image is a normal OCI image (`python:3.12-slim` + `pip install /app` + the repo copied to `/app` — the echo image build needs `repo_root` on disk for `pip install whirlwind @ file://…`). Deployment: 1 replica, `--metadata-backend memory` (single-node form; PG/Redis URLs would cross into cluster services, P3 concern), PVC-backed data dir via k3s local-path, NodePort service. Sandboxes spawn as child processes inside the pod — the process driver needs no privileges.

开发沙箱上的单节点 k3s（无 systemd → `k3s server` 作为普通进程；containerd 走 `k3s ctr`）。镜像是普通 OCI 镜像（`python:3.12-slim` + `pip install /app` + 仓库拷到 `/app`——echo 镜像构建需要磁盘上的 `repo_root` 以 `pip install whirlwind @ file://…`）。部署：1 副本、`--metadata-backend memory`（单节点形态；PG/Redis URL 涉及集群服务，属 P3）、k3s local-path 的 PVC 数据目录、NodePort 服务。沙箱作为 pod 内子进程拉起——process driver 无需特权。

Artifacts live in `deploy/k3s/` (image build script, manifest, smoke script); the smoke script drives the same REST surface the integration suite uses — build echo image, create agent, run a turn to completion.

产物放在 `deploy/k3s/`（镜像构建脚本、清单、冒烟脚本）；冒烟脚本驱动与集成套件相同的 REST 面——构建 echo 镜像、创建 agent、跑完一个 turn。

**Measured reality on the dev sandbox** (2026-08-19): the sandbox container has no `CAP_SYS_ADMIN`, a read-only cgroup2 mount and read-only `/proc/sys`, so kubelet/containerd cannot run and full agent mode is impossible — this is a container limit, not a k3s defect. What shipped instead, all verified for real:

**开发沙箱上的实测结论**（2026-08-19）：沙箱容器无 `CAP_SYS_ADMIN`、cgroup2 只读挂载、`/proc/sys` 只读，kubelet/containerd 无法运行，完整 agent 模式不可行——这是容器限制，不是 k3s 缺陷。实际交付并真实验证的是：

- `k3s server --disable-agent` (v1.36.3+k3s1, `--egress-selector-mode=disabled`): a real control plane — kube-apiserver + controller-manager + scheduler over sqlite. `deploy/k3s/dev-server.sh` boots it.
- A fake node kept Ready the KWOK way: a 10s loop renewing the `kube-node-lease` Lease + patching the node's Ready condition; without it the node-lifecycle-controller taints the node `unreachable` within ~40s and scheduling dies.
- A pre-bound static PV (`local-path` StorageClass label + `claimRef`) standing in for the disabled local-path provisioner, so the PVC binds.
- `deploy/k3s/manifest.yaml` applied for real: Deployment→ReplicaSet→Pod chain created, pod scheduled onto the node (`Successfully assigned`), PVC `Bound`, NodePort 30841 allocated. Pods stop at ContainerCreating until a node with a kubelet joins — the honest limit of a kubelet-less control plane.
- Ops knowledge recorded for this mode: pod deletion needs `--force --grace-period=0` (graceful deletion waits for a kubelet that will never ack), and a Retain PV's `claimRef` must be cleared before a recreated PVC can rebind.

- `k3s server --disable-agent`（v1.36.3+k3s1，`--egress-selector-mode=disabled`）：真实控制面——kube-apiserver + controller-manager + scheduler 跑在 sqlite 上。由 `deploy/k3s/dev-server.sh` 拉起。
- 以 KWOK 的方式保活的假节点：10 秒循环 renew `kube-node-lease` 的 Lease + patch 节点 Ready condition；没有它，node-lifecycle-controller 会在 ~40 秒内给节点打 `unreachable` 污点，调度即死。
- 预绑定的静态 PV（`local-path` StorageClass 标签 + `claimRef`）顶替被禁用的 local-path provisioner，使 PVC 可绑定。
- `deploy/k3s/manifest.yaml` 真实 apply：Deployment→ReplicaSet→Pod 链路创建、Pod 调度到节点（`Successfully assigned`）、PVC `Bound`、NodePort 30841 分配。Pod 停在 ContainerCreating 直到有真实 kubelet 的节点加入——这是无 kubelet 控制面的诚实极限。
- 该模式下的运维知识已记录：Pod 删除需 `--force --grace-period=0`（优雅删除在等一个永远不会应答的 kubelet）；Retain PV 的 `claimRef` 须清除后重建的 PVC 才能重新绑定。

`deploy/k3s/smoke.sh` is rerunnable (409 on image rebuild tolerated, unique agent names): healthz → build echo image → create agent → open session → turn to `turn/end`. Verified against a live gateway process; on a k3s with real nodes it targets the NodePort unchanged.

`deploy/k3s/smoke.sh` 可重入（镜像重建容忍 409、agent 名唯一）：healthz → 构建 echo 镜像 → 创建 agent → 开会话 → 跑 turn 到 `turn/end`。已对真实网关进程验证；在有真实节点的 k3s 上原样指向 NodePort 即可。

## D5 Testing — real enforcement, no fakes

## D5 测试——真实执行，不用替身

- **Resources (process)**: a sandbox with `mem_limit_mb` that tries to allocate past the cap dies of `MemoryError`; a busy-loop with `cpu_seconds` dies of SIGXCPU; `/proc/self/limits` inside the sandbox shows the applied rlimits. Real child processes, real kernels.
  **资源（process）**：设 `mem_limit_mb` 的沙箱试图超额分配内存死于 `MemoryError`；设 `cpu_seconds` 的忙循环死于 SIGXCPU；沙箱内读 `/proc/self/limits` 可见生效的 rlimit。真实子进程、真实内核。
- **Resources (runsc)**: OCI config rendering asserted field-by-field; live enforcement runs only when runsc + root are present (skip otherwise, ADR-0002 policy).
  **资源（runsc）**：逐字段断言 OCI config 渲染；真实执行仅在 runsc + root 可用时运行（否则跳过，ADR-0002 策略）。
- **Quota**: cap=2 → the third create gets `whirlwind/quota-exceeded` (HTTP 429); closing a session frees capacity.
  **配额**：上限 2 → 第三个创建得到 `whirlwind/quota-exceeded`（HTTP 429）；关闭会话释放容量。
- **Idempotency**: unit level via ASGI transport against a scratch app (replay identity, 409 in-flight, 422 body mismatch, 5xx key release, TTL expiry); integration level against the real gateway (same key twice → same session id, exactly one session created).
  **幂等**：单元层用 ASGI transport 对临时应用（重放一致性、409 in-flight、422 体重不符、5xx 释放键、TTL 过期）；集成层对真实网关（同键两次 → 同 session id，恰好创建一个会话）。
- **k3s**: manifest asserted field-by-field in `tests/integration/test_k3s_manifest.py` (probes, resources, env wiring, volumes, NodePort — runs everywhere); when a control plane is reachable the same manifest is applied for real and the controller chain verified (Deployment→RS→Pod, scheduling, PVC binding, NodePort). The smoke script is the live test — it fails loudly if the turn never completes; no mocked Kubernetes.
  **k3s**：`tests/integration/test_k3s_manifest.py` 逐字段断言清单（探针、资源、env 接线、卷、NodePort——处处可跑）；控制面可达时同一份清单被真实 apply 并验证控制器链路（Deployment→RS→Pod、调度、PVC 绑定、NodePort）。冒烟脚本即活体测试——turn 未完成即大声失败；不 mock Kubernetes。

## D6 Destructiveness & cohesion assessment

## D6 破坏性与内聚性评估

- **Protocols**: zero change. `SandboxSpec` gains an optional field (default = today's behavior); `KVStore`/`MetadataStore` untouched.
  **Protocol 层**：零变更。`SandboxSpec` 增加可选字段（默认 = 现行为）；`KVStore`/`MetadataStore` 不动。
- **Drivers**: each applies limits inside its own `create` — the only place substrate mechanics belong. Neither driver imports the other.
  **Driver 层**：各自在自己的 `create` 内施加限制——基底机制唯一该在的地方。两个 driver 互不导入。
- **Control plane**: quota is three lines in `create_session` plus one constructor arg; no new module, no new store.
  **控制面**：配额是 `create_session` 内三行加一个构造参数；无新模块、无新存储。
- **Gateway**: idempotency is one new module + one middleware registration + one optional `GatewayDeps` field; handlers unchanged.
  **网关**：幂等是一个新模块 + 一次中间件注册 + 一个可选 `GatewayDeps` 字段；handler 不动。
- **High cohesion / low coupling**: resource mechanics (drivers) vs admission control (manager) vs retry semantics (gateway) stay in their layers; the only shared primitive is the existing KVStore.
  **高内聚低耦合**：资源机制（driver）、准入控制（manager）、重试语义（网关）各守其层；唯一共享原语是既有 KVStore。

## D7 Non-goals this round

## D7 本轮非目标

- Authentication / tenancy (P1.1 — needs a tenant model first)
- CPU rate limiting (cgroup quota/period; belongs with tenant quotas)
- Cross-process quota hardening (Redis counter; same round as tenancy)
- Rate limiting per API key, cluster form on k3s (single replica only)

- 认证 / 租户（P1.1——先要有租户模型）
- CPU 速率限制（cgroup quota/period；与租户配额同轮）
- 跨进程配额加固（Redis 计数器；与租户同轮）
- 按 API key 限流、k3s 上的集群形态（仅单副本）
