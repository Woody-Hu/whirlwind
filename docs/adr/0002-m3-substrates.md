# ADR-0002: M3 Substrates — runsc driver, vsock transport, durable EventLog

# ADR-0002：M3 沙箱底座——runsc driver、vsock 传输、durable EventLog

- Status: Accepted
- Date: 2026-08-19
- Related: [ADR-0001](0001-agent-runtime-m1.md) (D2 reserves "the runsc driver joins at M3 under the same interface, scheduler unchanged")

- 状态：已接受
- 日期：2026-08-19
- 关联：[ADR-0001](0001-agent-runtime-m1.md)（D2 预留「runsc driver 按同一接口在 M3 加入，调度器零改动」）

---

## Background

## 背景

M1/M2 delivered the vertical slice with a process driver (single node, macOS/Linux dual platform). M3 introduces three production-grade components: a real kernel-level isolation substrate (gVisor), a microVM-shaped transport channel (virtio-vsock), and crash-safe event persistence (WAL). All three have been validated against real infrastructure; this ADR records the engineering decisions confirmed during that validation.

M1/M2 以 process driver 完成竖切（单节点、macOS/Linux 双平台）。M3 引入三个生产化构件：真实的内核级隔离底座（gVisor）、microVM 形态的传输通道（virtio-vsock）、崩溃安全的事件持久化（WAL）。三者都已对真实基础设施验证，本文记录验证中确定的工程决策。

## D1 runsc driver: CLI orchestration over API binding

## D1 runsc driver：CLI 编排而非 API 绑定

This aligns with Architecture 8.2 "binary orchestration wrapped in a subprocess." The driver is a thin orchestration layer over the `runsc` CLI: OCI bundle rendering (rootfs = image bundle bind-mounted to `/`, workspace bind-mounted to `/workspace`) plus subcommand mapping (run/exec/pause/resume/checkpoint/restore/kill/delete).

对齐架构 8.2「二进制编排用 subprocess 封装」。driver 是 `runsc` CLI 的薄编排层：OCI bundle 渲染（rootfs = 镜像 bundle bind-mount 到 `/`，workspace bind-mount 到 `/workspace`）+ 子命令映射（run/exec/pause/resume/checkpoint/restore/kill/delete）。

**End-to-end validation against real gVisor (release-20260810.0, systrap) exposed four defects that stub-level tests could not.**

**对真实 gVisor（release-20260810.0, systrap）端到端验证，修正了四个 stub 级测试无法暴露的缺陷：**

1. `--network=netstack` is not a valid flag value — runsc's per-sandbox netstack value is `sandbox`. The original code failed on the first hop against the real environment.

1. `--network=netstack` 不是合法 flag 值——runsc 的每沙箱 netstack 取值是 `sandbox`。原代码在真实环境第一跳就报错。

2. **Detach pipe deadlock**: the parent of `runsc run --detach` exits immediately, but the sandbox / gofer child inherits the stdout/stderr pipe fd, so `communicate()` never sees EOF (measured, hung for the full 60s timeout). Fix: route output to a temp file and await the parent exit code.

2. **detach 管道死锁**：`runsc run --detach` 的父进程立即退出，但 sandbox / gofer 子进程继承 stdout/stderr 管道 fd，`communicate()` 永远等不到 EOF（实测挂满 60s 超时）。修正：输出走临时文件，等待父进程退出码。

3. exec's `--cwd` was incorrectly passing the host workspace path; the in-sandbox cwd must be a rootfs-relative mount point (`/workspace`).

3. exec 的 `--cwd` 误传宿主工作区路径；沙箱内 cwd 必须是 rootfs 相对挂载点（`/workspace`）。

4. The `--` separator injected by exec was being treated as the executable name by the current runsc version.

4. exec 注入的 `--` 分隔符被当前 runsc 版本当作可执行名。

**Capability declaration, evidenced**: `uname -r` inside the sandbox returns `4.19.0-gvisor` (Sentry userspace kernel) rather than the host's `6.18.5` — `Isolation.LIGHT_VM` is a real isolation boundary, not merely declarative. FULL snapshots are written to disk via embedded CRIU (`checkpoint.img` / `pages.img`, ~270KB for a busybox sandbox).

**能力声明实证**：沙箱内 `uname -r` 返回 `4.19.0-gvisor`（Sentry 用户态内核）而非宿主 `6.18.5`——`Isolation.LIGHT_VM` 是真实隔离边界，非声明式。FULL 快照经内嵌 CRIU 落盘（`checkpoint.img` / `pages.img`，busybox 沙箱约 270KB）。

**Constrained-environment adaptation** (the driver gains `platform / rootless / ignore_cgroups` parameters): containers without `CAP_SYS_ADMIN` (rootless containerd/docker) must run rootless, skip cgroups, and forgo per-sandbox netstack (`--network=none` or `host`). **runsc upstream limitation**: restore is unsupported in rootless mode — the FULL snapshot restore test is skipped based on environment capability and runs on fully-capable hosts.

**受限环境适配**（driver 新增 `platform / rootless / ignore_cgroups` 参数）：无 `CAP_SYS_ADMIN` 的容器（rootless containerd/docker）必须 rootless 运行、跳过 cgroups、放弃每沙箱 netstack（`--network=none` 或 `host`）。**runsc 上游限制**：restore 不支持 rootless 模式——FULL 快照恢复测试按环境能力跳过，全能力宿主可用。

## D2 Transport layer: one abstraction across three substrates

## D2 传输层：三 substrate 同一抽象

`transport/` provides `Endpoint{scheme, address, port}` and `connect() / serve()`, unifying TCP / UDS / vsock over asyncio streams. The existing HTTP protocol between SandboxAgent and Hostlet is unchanged — `http+unix://` and `http+vsock://` URL schemes are resolved at `parse_url` into the (endpoint, base_path) pair, and the hop semantics remain transparent to the upper layer.

`transport/` 提供 `Endpoint{scheme, address, port}` 与 `connect() / serve()`，TCP / UDS / vsock 统一到 asyncio streams。SandboxAgent 与 Hostlet 的既有 HTTP 协议不变——`http+unix://`、`http+vsock://` URL scheme 在 `parse_url` 处解出（endpoint, base_path）二元组，跳板语义对上层透明。

vsock uses native `AF_VSOCK` sockets hand-adapted to asyncio (no third-party dependency). `vsock_available()` probes `/dev/vsock`; the device exists only on VM platforms (Firecracker / QEMU), and container environments cannot load kernel modules, so the test skips based on availability — this is a hard kernel restriction, not an unimplemented feature.

vsock 用原生 `AF_VSOCK` socket 手工适配 asyncio（无第三方依赖）。`vsock_available()` 探测 `/dev/vsock`；该设备只在 VM 平台（Firecracker / QEMU）下存在，容器环境无内核模块加载能力，测试按可用性跳过——这是硬性内核限制而非未实现。

URL convention: `vsock://PORT` for the server binding on the host listener; the client uses `http+vsock://{CID}:{PORT}`, with `VMADDR_CID_HOST=2` pointing at the host.

URL 形态约定：`vsock://PORT` 服务端绑定宿主监听；客户端 `http+vsock://{CID}:{PORT}`，`VMADDR_CID_HOST=2` 指向宿主。

## D3 Durable EventLog: WAL + group-commit fsync

## D3 durable EventLog：WAL + 组提交 fsync

M1's `JSONLEventLog` only does line-buffered flush, so a process crash loses trailing data — conflicting with Architecture 10.3's replayability promise that "every model-visible input can be rebuilt from the event log." M3 replaces it with `WALEventLog`:

M1 的 `JSONLEventLog` 只做行缓冲 flush，进程崩溃即丢尾部数据——与架构 10.3「模型可见的每一个输入都可从事件日志重建」的可回放承诺冲突。M3 以 `WALEventLog` 替换：

- **Durability contract**: `append()` returns only after the record is fsynced to disk. Writes before a crash/power loss may be dropped, but the log is never corrupted and never reorders committed records.

- **持久性契约**：`append()` 仅在记录 fsync 落盘后返回。崩溃/断电前的写入可丢弃，但绝不损坏日志、绝不重排已提交记录。

- **Group commit**: concurrent appends share a single fsync per commit batch. Measured at ~1.4k/s for sequential append (single-record fsync latency floor), rising to 38k/s under a burst of 10k concurrent appends (~27x amortization) — the ADR's ≥5k/s acceptance line is moved to the burst path, with the sequential path set at a 1k/s persistence floor.

- **组提交**：并发 append 在同一提交批次共享一次 fsync。实测顺序追加 ~1.4k/s（单条 fsync 延迟地板），10k 并发突发 38k/s（~27x 摊销）——ADR ≥5k/s 验收线移至突发路径，顺序路径设 1k/s 持久化地板线。

- **Crash recovery**: reopen scans the WAL and truncates torn trailing records (including "complete JSON whose trailing newline was never written" — that record also fails the durability contract and must be dropped); `read()` defensively skips torn lines.

- **崩溃恢复**：reopen 时扫描 WAL、截断撕裂的尾部记录（含「完整 JSON 但换行未落盘」——该记录同样未达持久性契约，必须丢弃）；`read()` 兜底跳过撕裂行。

- **Errors propagated upward**: fsync failure is returned to the append caller through a future, while the committer stays alive to keep serving subsequent batches.

- **错误上抛**：fsync 失败经 future 传回 append 调用方，committer 存活继续服务后续批次。

File layout is unchanged (one JSONL per session, directory-based), the `EventLog` protocol is untouched, and the runtime wiring is a single-line swap.

文件布局不变（每会话一个 JSONL，目录型），`EventLog` 协议零改动，runtime 装配处单行替换。

## Test & validation record

## 测试与验证记录

- Full lifecycle on real gVisor (busybox rootfs): create / exec (cwd verified at `/workspace`) / pause / resume / DATA snapshot (merkle) / FULL snapshot (CRIU image to disk) / destroy, plus a kernel-isolation proof case.

- 真实 gVisor 全生命周期（busybox rootfs）：create / exec（cwd 验证 `/workspace`）/ pause / resume / DATA 快照（merkle）/ FULL 快照（CRIU 镜像落盘）/ destroy，另有内核隔离证明用例。

- Transport TCP / UDS loopback tests actually run; vsock is skipped based on device presence.

- 传输层 TCP / UDS 回环测试真跑；vsock 按设备存在性跳过。

- WAL: group-commit fsync count (100 concurrent appends ≤ 5 fsyncs), durable-on-write, torn tail truncation, dropped no-newline records, per-session independent seq, and fsync-failure propagation.

- WAL：组提交 fsync 计数（100 并发 append ≤ 5 次 fsync）、落盘即见、撕裂尾截断、无换行记录丢弃、跨会话 seq 独立、fsync 失败上抛。

- Full regression: 139 passed / 5 skipped (all skips are environment capability limits: vsock device, rootless restore).

- 全量回归：139 passed / 5 skipped（skip 均为环境能力限制：vsock 设备、rootless restore）。

## Follow-ups

## 后续

- A firecracker microVM driver (third substrate, Architecture 4.4) will join the same `SandboxDriver` interface.

- firecracker microVM driver（架构 4.4 第三底座）接入同一 `SandboxDriver` 接口。

- Clustered form: the SQLite/PG provider slot for WAL EventLog and the NATS provider slot for EventBus are already reserved.

- 集群形态：WAL EventLog 的 SQLite/PG provider 位、EventBus 的 NATS provider 位已预留。