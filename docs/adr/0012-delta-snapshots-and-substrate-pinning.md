# ADR-0012: Delta snapshots and single-substrate pinning

# ADR-0012：Delta 快照与单一底座钉选

- Status: Accepted
- Date: 2026-08-20
- Related: [ADR-0001](0001-agent-runtime-m1.md) D2/D3（driver 接缝与能力位诚实）、[ADR-0002](0002-m3-substrates.md)（runsc 快照）、[ADR-0005](0005-edge-hardening.md) D1（资源上限诚实注记范式）、[ADR-0007](0007-platform-abstraction.md)（探测类事实不被覆盖）、[ADR-0009](0009-unified-config.md)（配置统一注入）、[ADR-0011](0011-seam-templates-and-harness-bundles.md) D6（catalog 接缝预留）、架构文档 4.4（SandboxDriver）

- 状态：已接受
- 日期：2026-08-20
- 关联：[ADR-0001](0001-agent-runtime-m1.md) D2/D3（driver 接缝与能力位诚实）、[ADR-0002](0002-m3-substrates.md)（runsc 快照）、[ADR-0005](0005-edge-hardening.md) D1（资源上限诚实注记范式）、[ADR-0007](0007-platform-abstraction.md)（探测类事实不被覆盖）、[ADR-0009](0009-unified-config.md)（配置统一注入）、[ADR-0011](0011-seam-templates-and-harness-bundles.md) D6（catalog 接缝预留）、架构文档 4.4（SandboxDriver）

---

## Context

## 背景

**English.** Today every DATA snapshot is a *full* copy of the sandbox workspace (`ProcessDriver.checkpoint` → `_copy_tree`). A session's workspace is dominated by content that barely changes across suspend/resume cycles — the seeded plan files, staged skills, harness session logs that only append — yet each suspend re-stores all of it. The user's observation (task #5): *snapshots are really "a base harness image + changes"; encoding the delta explicitly shrinks snapshot size*. Not every substrate can do this (a gVisor CRIU dump is opaque image files, not a diffable tree), so the capability must be a truthful driver bit, never a silent assumption. The same task asks for the cluster-level counterpart: *pin a cluster to exactly one sandbox implementation by configuration* — a homogeneous fleet is simpler to load-balance and capacity-plan, and today the driver is hardcoded in the runtime composition root.

**中文.** 今天每份 DATA 快照都是 sandbox workspace 的*全量*拷贝（`ProcessDriver.checkpoint` → `_copy_tree`）。一个 session 的 workspace 里，跨 suspend/resume 周期几乎不变的内容占大头——播种的 plan 文件、staged skills、只追加的 harness session 日志——但每次 suspend 都全量重存。用户的观察（任务 #5）：*快照本质上是「基础 harness 镜像 + 变更」；显式编码 delta 可以缩小快照*。不是所有底座都能做（gVisor 的 CRIU dump 是不透明镜像文件，不可 diff），所以能力必须是如实的 driver 能力位，绝不静默假设。同一任务还要求集群级配套：*通过配置把集群钉选到恰好一种 sandbox 实现*——同构舰队更易负载均衡与容量规划，而今天 driver 在运行时组合根里是写死的。

## Decision — D1: `Caps.delta_snapshots` — a truthful capability bit

## 决策——D1：`Caps.delta_snapshots`——如实上报的能力位

**English.** `Caps` gains `delta_snapshots: bool = False`. Declaring it means the driver can (a) produce a *delta* checkpoint against a caller-supplied base artifact and (b) materialize a delta artifact chain back into a full tree. Default `False` keeps every undeclared driver honest for free. `process` declares `True` (workspace trees are plain directories — file-level diff/apply is driver-owned pure logic); `runsc` declares `False` (its DATA artifacts *could* be diffed in principle, but claiming so without an implementation would violate the declare-what-you-enforce rule — revisit when a real need exists). The scheduler/hostlet filter on this bit exactly as they do on `snapshot_full`/`snapshot_data`.

**中文.** `Caps` 新增 `delta_snapshots: bool = False`。声明它意味着 driver 能够 (a) 基于调用方给的 base 产出 *delta* 检查点，且 (b) 把 delta 工件链物化回完整目录树。默认 `False` 让每个未声明的 driver 自动保持诚实。`process` 声明 `True`（workspace 就是普通目录——文件级 diff/apply 是 driver 自有的纯逻辑）；`runsc` 声明 `False`（其 DATA 工件原则上*可以* diff，但没有实现就声明会违反「声明即执行」——有真实需求时再修订）。调度器/hostlet 按此位过滤，与 `snapshot_full`/`snapshot_data` 完全同构。

## Decision — D2: delta artifact shape — overlay payload + materialized-state merkle

## 决策——D2：delta 工件形态——overlay 载荷 + 物化态 merkle

**English.** `SnapshotArtifact` gains `delta: bool = False`; the manifest of a delta artifact carries `base = {snapshot_id, path, merkle, delta}` (identity of the direct predecessor) and `chain_depth` (0 for a full artifact, +1 per delta hop). The payload directory stores only *changed content*: new/modified files at their relative paths, plus a `WHIRLWIND_DELTA.json` index recording deletions and the base identity — the classic overlay/incremental-backup shape (rsync `--backup`, overlayfs upperdir, ZFS send-streams all look like this). Two invariants:

1. **Content-addressed end state.** A delta artifact's `merkle` is the merkle of the *materialized* tree, not of the payload — a full snapshot and a delta snapshot of the same workspace state share the same root hash, so "restore produced exactly the pre-suspend state" is a free equality check.
2. **Link integrity.** Materialization verifies the base's recorded `merkle` against the base artifact's own root before applying (and re-verifies per hop); a broken chain fails closed with a stable `whirlwind/driver` error naming the bad hop.

`size` reports the payload bytes only — that is what "delta shrinks the snapshot" means operationally.

**中文.** `SnapshotArtifact` 新增 `delta: bool = False`；delta 工件的 manifest 携带 `base = {snapshot_id, path, merkle, delta}`（直接前驱的身份）与 `chain_depth`（全量为 0，每经一跳 delta 加一）。载荷目录只存*变化的内容*：新增/修改文件按相对路径存放，另加一份 `WHIRLWIND_DELTA.json` 索引记录删除与 base 身份——经典的 overlay/增量备份形态（rsync `--backup`、overlayfs upperdir、ZFS send 流都是这个样子）。两条不变量：

1. **端态内容寻址。** delta 工件的 `merkle` 是*物化后*目录树的 merkle，不是载荷的 merkle——同一份 workspace 状态的全量快照与 delta 快照共享同一根哈希，「恢复得到的就是 suspend 前的状态」由此免费获得等值校验。
2. **链完整性。** 物化前按 base 工件自身的根校验其记录的 `merkle`（每一跳都验）；断链以稳定 `whirlwind/driver` 错误指名坏掉的跳，fail-closed。

`size` 只报载荷字节数——这才是「delta 缩小快照」在运营意义上的含义。

## Decision — D3: driver API — `checkpoint(base=...)` + `materialize(...)`

## 决策——D3：driver API——`checkpoint(base=...)` + `materialize(...)`

**English.** `SandboxDriver.checkpoint` gains a keyword-only `base: SnapshotArtifact | None = None`. `base=None` → full artifact (identical to today). `base` given: a driver with `delta_snapshots=False` raises `UnsupportedCapability` (honest, checked before any work); a capable driver diffs the live workspace against the materialized base and emits the overlay. A new `materialize(artifact, dest)` operation reconstructs a full tree at `dest` — full artifacts copy, delta artifacts resolve their base chain (bounded by the recorded `chain_depth`, each hop merkle-verified) and apply overlays in order. The *hostlet* routes its snapshot seeding through `materialize` (it used to `shutil.copytree` the artifact path directly — correct only for full trees), so chain reconstruction lives in exactly one place: the driver that produced the chain. `create(from_snapshot=...)` signatures are unchanged.

**中文.** `SandboxDriver.checkpoint` 新增仅关键字参数 `base: SnapshotArtifact | None = None`。`base=None` → 全量工件（与今天完全一致）。给了 `base`：`delta_snapshots=False` 的 driver 抛 `UnsupportedCapability`（诚实，且在任何实际工作之前检查）；有能力的 driver 把活 workspace 与物化后的 base 做 diff 并输出 overlay。新增 `materialize(artifact, dest)` 操作在 `dest` 重建完整目录树——全量工件直接拷贝，delta 工件解析其 base 链（以记录的 `chain_depth` 为界，每跳 merkle 校验）并依序应用 overlay。*hostlet* 的快照播种改走 `materialize`（原先直接 `shutil.copytree` 工件路径——只对全量树正确），链重建因此只存在于产出该链的 driver 一处。`create(from_snapshot=...)` 签名不变。

## Decision — D4: hostlet policy — chained deltas per session lineage, capped with periodic full

## 决策——D4：hostlet 策略——按 session 血缘链式 delta，链深封顶周期性全量

**English.** Suspend-time policy in the hostlet, driven by `snapshot_mode` (`"full"` default | `"delta"`):

- `full` — exactly today's behavior (zero change; the default keeps every existing deployment byte-identical).
- `delta` — if the driver declares `delta_snapshots` *and* the session has a prior DATA snapshot (the lineage: same `session_id`, the snapshot the current sandbox was itself seeded from or later), checkpoint against it as base; otherwise take a full (first snapshot of a lineage, warm-pool sandboxes with no session, or any case without a predecessor). Chains therefore grow along suspend/resume cycles, where the size win concentrates (append-only session logs over a stable seed).
- **Compaction:** when the prospective chain depth reaches `snapshot_chain_max` (default 16), the hostlet requests a full instead — bounded restore cost, the standard incremental-backup trade (git packfile / ZFS snapshot retention both do this).

Restore needs no policy: `materialize` walks whatever chain the latest snapshot carries. The runtime *boot-validates* `snapshot_mode=delta` against the composed driver's caps and refuses to start with a `ConfigError` — an incapable substrate must fail loudly at boot, not per-suspend.

**中文.** hostlet 的 suspend 期策略，由 `snapshot_mode`（默认 `"full"` | `"delta"`）驱动：

- `full`——与今天的行为完全一致（零变化；默认值保证存量部署逐字节不变）。
- `delta`——若 driver 声明了 `delta_snapshots` *且* session 已有前一份 DATA 快照（血缘：同 `session_id`，即当前 sandbox 自己被播种的来源或更晚者），以它为 base 做检查点；否则做全量（血缘首份、无 session 的 warm 池沙箱、任何没有前驱的情形）。链因此沿 suspend/resume 周期增长——这正是体积收益集中的地方（稳定种子上只追加的 session 日志）。
- **压实（compaction）：** 当预期链深达到 `snapshot_chain_max`（默认 16）时，hostlet 改为请求全量——恢复成本有界，增量备份的标准取舍（git packfile / ZFS 快照保留都是这么做的）。

恢复无需策略：`materialize` 走最新快照携带的链。运行时在*启动期*用组好的 driver 能力位校验 `snapshot_mode=delta`，不接受就拒绝启动（`ConfigError`）——不称职的底座必须在启动时大声失败，而不是每次 suspend 才失败。

## Decision — D5: substrate pinning by configuration — `sandbox.driver`

## 决策——D5：按配置钉选底座——`sandbox.driver`

**English.** The composition root constructs the driver named by `sandbox.driver` (`"process"` default | `"runsc"`), with roots under `data_dir` exactly as today. A named-but-unavailable substrate (runsc binary missing, non-Linux host) fails boot with an actionable `ConfigError` — no silent fallback to process: a cluster that *thinks* it runs gVisor while actually running process groups is a security regression, and the honest-failure rule (AGENTS.md §3.2 能力诚实) applies to composition, not just to caps. Homogeneous pinning is then a one-line config repeated per node (`sandbox.driver = "runsc"`), which is precisely the "one substrate per cluster to reduce load complexity" ask — the scheduler already only ever sees one driver in this deployment form, and the config surface is the single source of truth. New substrates (microsandbox, ADR-0006) join by adding a constructor branch — the open-closed extension point is the *registry of named constructors*, not a scheduler change.

**中文.** 组合根按 `sandbox.driver`（默认 `"process"` | `"runsc"`）指名的 driver 构造，各 root 仍如今天落在 `data_dir` 之下。指名但不可用的底座（runsc 二进制缺失、非 Linux 宿主）以可行动的 `ConfigError` 拒绝启动——绝不静默回退到 process：一个*以为*在跑 gVisor 实际在跑进程组的集群是安全回退，诚实失败原则（AGENTS.md §3.2 能力诚实）同样约束组合根，而不只约束能力位。同构钉选由此变成每节点一行的配置（`sandbox.driver = "runsc"`）——正是「一个集群只用一种 sandbox 实现以减轻负载复杂度」的诉求：该部署形态下调度器只会看到一个 driver，配置面是唯一事实源。新底座（microsandbox，ADR-0006）以增加一个命名构造分支加入——开闭扩展点是*命名构造器注册表*，不是调度器改动。

## Decision — D6: configuration keys & validation

## 决策——D6：配置键与校验

**English.** Three new keys in the ADR-0009 schema (all under `[sandbox]`, env-overridable, CLI-overridable): `driver` (string, `"process"` | `"runsc"`), `snapshot_mode` (string, `"full"` | `"delta"`), `snapshot_chain_max` (int ≥ 1, default 16). `whirlwind config show` renders them like every other key. Cross-key validation at load time: unknown driver names are a `ConfigError`; `snapshot_mode=delta` is validated against the *composed* driver at boot (not at config load — the loader is driver-agnostic, the composition root owns the caps check, mirroring ADR-0009's "schema in the loader, semantics in the consumer" split).

**中文.** ADR-0009 schema 新增三键（均在 `[sandbox]` 下，可被 env 覆盖、可被 CLI 覆盖）：`driver`（字符串，`"process"` | `"runsc"`）、`snapshot_mode`（字符串，`"full"` | `"delta"`）、`snapshot_chain_max`（≥1 整数，默认 16）。`whirlwind config show` 与其他键一样渲染。加载期做键内校验：未知 driver 名是 `ConfigError`；`snapshot_mode=delta` 与*组好的* driver 的校验放在启动期（不在配置加载期——loader 与 driver 无关，能力位检查归组合根，对应 ADR-0009「schema 在 loader、语义在消费方」的分工）。

## Test strategy

## 测试策略

- **Unit (real fs, tmp dirs):** diff/apply round-trip (added/modified/deleted/symlink/empty-dir cases); delta merkle == full merkle of the same end state; corrupt base (mutated file) → materialize fails naming the hop; `chain_depth` bookkeeping; runsc `checkpoint(base=...)` raises `UnsupportedCapability` before touching the binary; `chain_max` compaction flips to full.
- **Integration (real processes):** hostlet suspend/resume × N under `snapshot_mode=delta` — snapshots 2..N carry deltas, restored workspace merkle equals pre-suspend merkle every cycle; compaction resets the chain at `chain_max`; `full` mode output byte-identical to pre-ADR behavior (regression pin); boot validation errors for delta+runsc-missing and driver=runsc-missing.
- **Benchmark (real measurement, no fabricated thresholds):** sparse-mutation workload (representative workspace, small % changed per cycle) — measured full vs delta `size` and checkpoint wall time per cycle + materialize time; the only *asserted* property is structural (delta payload smaller than full for the sparse workload); numbers land in the session-log as the baseline.

**中文.** 单测（真实文件系统、tmp 目录）：diff/apply 往返（新增/修改/删除/符号链接/空目录）；同一端态下 delta merkle == 全量 merkle；base 被篡改 → materialize 指名跳数失败；`chain_depth` 记账；runsc `checkpoint(base=...)` 在碰二进制之前抛 `UnsupportedCapability`；`chain_max` 压实翻全量。集成（真实进程）：`snapshot_mode=delta` 下 hostlet suspend/resume × N——第 2..N 份快照携带 delta、每周期恢复后 workspace merkle 等于 suspend 前 merkle；`chain_max` 处链重置；`full` 模式输出与 ADR 前逐字节一致（回归钉）；delta+缺 runsc、driver=缺 runsc 的启动校验报错。基准（真实测量，不设伪造阈值）：稀疏变更负载（代表性 workspace、每周期小比例变更）——实测全量 vs delta 的 `size`、每周期检查点耗时与物化耗时；唯一*断言*的性质是结构性的（稀疏负载下 delta 载荷小于全量）；数字作为基线落 session-log。

## Conflicts with the architecture document

## 与架构文档的冲突检查

None. Architecture 4.4 already reserves capability-gated snapshot evolution; the `Snapshot` store model is untouched (delta identity lives in the artifact manifest the `Snapshot.manifest` already carries); ADR-0002's runsc semantics unchanged (`delta_snapshots=False` there). ADR-0009 gains three keys within its existing schema mechanism — no precedence change.

**中文.** 无。架构 4.4 本就预留了能力位门控的快照演进；`Snapshot` 存储模型不动（delta 身份放在 `Snapshot.manifest` 本就携带的工件 manifest 里）；ADR-0002 的 runsc 语义不变（其 `delta_snapshots=False`）。ADR-0009 在既有 schema 机制内新增三键——优先级阶梯不变。

## Implementation order

## 实施顺序

1. `drivers/base.py`: `Caps.delta_snapshots`, `SnapshotArtifact.delta`, `checkpoint(base=...)`, `materialize` in the Protocol.
2. `drivers/process.py`: diff/apply + chain materialization + delta checkpoint.
3. `drivers/runsc.py`: signature alignment, honest `UnsupportedCapability`.
4. `hostlet`: policy in `suspend`, seeding via `materialize`; `HostletConfig` fields.
5. `runtime` + `config.py`: driver registry, boot validation, three keys, env/CLI/render.
6. Tests + benchmark; docs (TODO/MEMORY/session-log).

**中文.** ① base.py 能力位与工件字段与协议；② process driver diff/apply 与链物化与 delta 检查点；③ runsc 签名对齐与诚实拒绝；④ hostlet 策略与 materialize 播种与配置字段；⑤ runtime + config 驱动注册表、启动校验、三键、env/CLI/渲染；⑥ 测试 + 基准与文档。

## Risks & open points

## 风险与开放点

- **Base garbage-collection:** delta artifacts keep their bases alive; deleting a snapshot that a delta still references must be refused (or compacted first). This ADR ships *no* GC — snapshots today are never deleted by the platform, so the risk is latent; the `base` manifest link is the seam a future GC will walk.
- **runsc delta:** deferred until a real need (CRIU image diffing is possible but unimplemented — the caps bit stays `False`, honestly).
- **Cross-node artifacts:** materialization assumes the base chain is reachable on local disk (`snapshots_root`); multi-node snapshot placement (object store) is the ADR-0004 `ObjectStore` seam's territory and out of scope here.
- **chain_max default 16:** a guess informed by incremental-backup practice, not a measurement; revisit with real dsh workloads (the config knob exists precisely so operators can tune per workload).
- **Measured cost shape (2026-08-20, Linux/x86_64 container, `tests/benchmark/test_delta_bench.py`):** chain depth raises BOTH ends, not just restore — a delta checkpoint must materialize its base chain into a scratch dir before diffing, so checkpoint time grows with depth too (sparse 1.6 MiB workspace, 8-hop chain: full ~130-150 ms/cycle flat; delta 114 → 662 ms/cycle; materialize full 97 ms vs chain 625 ms; delta payload 8-32 KiB vs full 1604-1632 KiB — the ~50x size win is where the design pays). `chain_max` therefore bounds checkpoint cost, restore cost AND storage blowup at once; a content-addressed block index (borg/restic-style) that avoids full base materialization is the known future optimization path, deliberately out of scope.

**中文.** base 垃圾回收：delta 工件会钉住其 base；删除仍被引用的 base 必须拒绝（或先压实）。本 ADR 不带 GC——平台今天从不删除快照，风险是潜伏的；`base` manifest 链接即未来 GC 要走的接缝。runsc delta：推迟到有真实需求（CRIU 镜像可 diff 但未实现——能力位诚实保持 `False`）。跨节点工件：物化假设 base 链在本地磁盘（`snapshots_root`）可达；多节点快照摆放（对象存储）是 ADR-0004 `ObjectStore` 接缝的地盘，超出本篇。chain_max 默认 16：受增量备份实践启发的估计值而非测量值；随真实 dsh 负载复评（配置旋钮存在正是为了让运营按负载调）。**实测成本形态（2026-08-20，Linux/x86_64 容器，`tests/benchmark/test_delta_bench.py`）：** 链深同时抬高两端成本，不只是恢复——delta 检查点必须先把 base 链物化到 scratch 目录才能 diff，检查点耗时也随深度增长（稀疏 1.6 MiB workspace、8 跳链：全量 ~130-150ms/周期恒定；delta 114 → 662ms/周期；物化全量 97ms vs 链 625ms；delta 载荷 8-32KiB vs 全量 1604-1632KiB——~50 倍的体积收益正是设计的回报所在）。`chain_max` 由此一次界定检查点成本、恢复成本与存储膨胀三件事；避免全量 base 物化的内容寻址块索引（borg/restic 风格）是已知的未来优化路径，刻意不在本篇范围。
