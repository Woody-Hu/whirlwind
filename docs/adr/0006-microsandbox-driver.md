# ADR-0006: Microsandbox driver — a locally-testable VM substrate on the `SandboxDriver` interface

# ADR-0006：Microsandbox driver——`SandboxDriver` 接口上一个可本地测试的 VM 底座

- Status: Implemented — M1 driver / M2 wiring / M3 gated tests landed 2026-08-20; real-VM lifecycle verification still pending a KVM/HVF-capable host (see Implementation record below)
- Date: 2026-08-19 (design) / 2026-08-20 (implementation)
- Related: [ADR-0001](0001-agent-runtime-m1.md) (driver seam D2), [ADR-0002](0002-m3-substrates.md) (runsc/gVisor), [ADR-0005](0005-edge-hardening.md) (resource ceilings), [ADR-0007](0007-platform-abstraction.md) (platform facts), [TODO](../TODO.md) P3.4/P3.5

- 状态：已实施——M1 驱动 / M2 装配 / M3 门控测试于 2026-08-20 落地；真实 VM 生命周期验证仍待有 KVM/HVF 的宿主（见下方实施记录）
- 日期：2026-08-19（设计）/ 2026-08-20（实施）
- 关联：[ADR-0001](0001-agent-runtime-m1.md)（driver 接缝 D2）、[ADR-0002](0002-m3-substrates.md)（runsc/gVisor）、[ADR-0005](0005-edge-hardening.md)（资源上限）、[ADR-0007](0007-platform-abstraction.md)（平台事实）、[TODO](../TODO.md) P3.4/P3.5

---

## Context

## 背景

Whirlwind's isolation spectrum today has a test-coverage hole on Apple Silicon:

Whirlwind 的隔离谱系在 Apple Silicon 上存在测试覆盖空洞：

| Substrate / 底座 | Isolation / 隔离 | Runs on macOS M-series / 能否在 M 芯片 macOS 运行 |
|---|---|---|
| `process` | `PROCESS` (进程组) | yes — but too weak to gate VM-grade lifecycle paths |
| `runsc` (gVisor) | `LIGHT_VM` (用户态内核) | **no** — Linux-only, needs a Linux host/kernel |
| `firecracker` | `MICRO_VM` (planned P3.4) | no — Linux-only by design |

`process` cannot exercise snapshot-`FULL`/network-policy code paths truthfully; `runsc`
can't run at all on macOS, so the entire VM-grade driver surface is only covered on a
Linux box. That blocks fast local development of the `control` → `hostlet` → driver
lifecycle and forces all VM-ish testing onto CI-that-has-Linux.

`process` 无法真实覆盖 snapshot-`FULL`/网络策略等代码路径；`runsc` 在 macOS 上根本无法运行，导致整个 VM 级 driver 面只在 Linux 机器上才有覆盖。这阻塞了 `control`→`hostlet`→driver 生命周期的本地快速开发，把一切 VM 类测试都押到了有 Linux 的 CI 上。

**Verified platform facts (2026-08):**
**已核实的平台事实（2026-08）：**

- gVisor `runsc` can be installed and registered as a Docker runtime *inside a Linux VM*:
  `/usr/local/bin/runsc install` + `/etc/docker/daemon.json` + `docker run --runtime=runsc`
  (needs host Linux kernel ≥ 4.14.77; verified by the official gVisor install docs). On
  M-series mac the Linux VM is provided by colima. gVisor also runs Docker *inside* a gVisor
  sandbox (`runsc install --runtime=docker-in-gvisor`, Google gVisor blog 2026-04).
- macOS supports k3s via `colima start --kubernetes` (k3s under the hood; verified by the
  colima docs / repo); the earlier "unsupported" symptom was an artifact of a headless
  sandbox executor, not the platform.
- **colima supports `--vm-type krunkit`** (verified by colima v0.10.0+ release notes and the
  config docs): libkrun/krunkit is a first-class virtual-machine backend on Apple Silicon,
  which is exactly the microsandbox backend selected in D1 — so a libkrun substrate is
  locally runnable today, not just reserved for Linux.
- dsh is a public repo: `github.com/deepseek-ai/deepseek-harness` (MIT).

These facts shape the environment story but do not make runsc usable as a mac-native
substrate: gVisor still needs a *Linux* process host (a colima Linux VM at best), so runsc
stays a Linux-CI / Linux-VM path and cannot be the default driver for the mac dev loop.

这些事实界定了环境形态，但并不使 runsc 成为 mac 原生可用底座：gVisor 仍需要一个
*Linux* 进程宿主（顶多是一个 colima Linux VM），因此 runsc 仍是 Linux-CI / Linux-VM 路径，
无法成为 mac 开发回路的默认 driver。

## Decision — D1: a libkrun-based microsandbox driver as a third substrate

## 决策——D1：以 libkrun 为基础的 microsandbox driver 作为第三底座

- Introduce `MicrosandboxDriver` in `drivers/microsandbox.py`, implementing the one
  `SandboxDriver` protocol, honestly reporting `Isolation.LIGHT_VM`.
- Backend: **libkrun / krunkit** — a lightweight microVM that runs natively on macOS
  through Apple's `Virtualization.framework` (and on Linux via KVM/virtio), so it is both
  a *locally runnable VM-grade substrate on M-series mac* and a dev analog of the
  Firecracker `MICRO_VM` path.
- Do **not** add a new `Isolation` value: libkrun is genuinely `LIGHT_VM` class (real
  kernel + real VM boundary). `MICRO_VM` stays reserved for Firecracker-class.
- Same resource-ceiling contract as D1 of [ADR-0005]: `Resources` (mem/cpu/pids) enforced
  by the microVM's device/resource model where truthful, otherwise documented as weaker.

- 新增 `drivers/microsandbox.py` 中的 `MicrosandboxDriver`，实现唯一的 `SandboxDriver`
  协议，并诚实声明 `Isolation.LIGHT_VM`。
- 后端：**libkrun / krunkit**——一个可通过 Apple `Virtualization.framework` 原生运行于
  macOS 的轻量微虚拟机（Linux 上走 KVM/virtio），因此既是在 M 芯片 mac 上*可本地运行的
  VM 级底座*，也是 Firecracker `MICRO_VM` 路径的开发级对应物。
- **不新增** `Isolation` 值：libkrun 确实是 `LIGHT_VM` 类（真实内核 + 真实 VM 边界）。
  `MICRO_VM` 仍预留给 Firecracker 类。
- 沿用 [ADR-0005] D1 的资源上限契约：`Resources`（mem/cpu/pids）在微 VM 的
  设备/资源模型可诚实兑现处执行，否则如实标注为较弱。

### Interface shape (unchanged from the seam)

### 接口形态（沿用接缝不变）

```python
# drivers/base.py
# no change: SandboxDriver Protocol + SandwichSpec + Resources are reused as-is.

class MicrosandboxDriver(SandboxDriver):
    name = "microsandbox"
    def capabilities(self) -> Caps:  # isolation=LIGHT_VM, snapshot_full delegated to
        ...                          #   libkrun snapshot/restore if supported else False
    async def create(self, spec, *, from_snapshot=None) -> Instance: ...
    async def exec(self, sandbox_id, spec) -> ExecResult: ...
    async def pause/resume/checkpoint/destroy(...): ...
```

Because the driver lives behind the `SandboxDriver` seam, `control` and `hostlet` require
**zero changes**: swap the concrete driver at runtime assembly (a new `--sandbox-driver
microsandbox` option) the same way P0 already swaps storage backends.

因为 driver 位于 `SandboxDriver` 接缝之后，`control` 与 `hostlet` **无需任何修改**：在
运行时装配处替换具体 driver（新增 `--sandbox-driver microsandbox` 选项），方式与 P0 切换
存储后端相同。

### Honesty posture (project non-negotiables)

### 诚实声明立场（项目不可妥协项）

- `caps.snapshot_full` is `True` **only if** the chosen backend really can
  checkpoint/restore kernel memory. libkrun snapshot support differs by version/platform;
  default to `False` and flip it on only where the integration test proves it (same rule as
  every driver).
- MAC capability and resource enforcement are reported truthfully per platform; never claim
  stronger than the tests demonstrate.

- `caps.snapshot_full` 仅在所选后端真能 checkpoint/restore 内核内存时为 `True`。libkrun 的
  快照支持依版本/平台而异；默认 `False`，仅在集成测试证实之处开启（与每个 driver 同规则）。
- MAC 能力与资源执行按平台如实上报；绝不声称超出测试能证实的强度。

## Test strategy

## 测试策略

- `tests/integration/test_microsandbox_driver.py` mirrors the runsc suite: capability and
  resource-config rendering logic run unconditionally; **real lifecycle tests run only when
  the backend is available** (skip + reason, never fake). Gate on
  `shutil.which("krun")`/krunkit availability, consistent with the no-mock rule.
- Readiness signal for the dev loop: `WHIRLWIND_DRIVER=microsandbox` + a `colima` VM so the
  "suspend → snapshot → resume" and pool-warm paths get a real VM-grade local run.

- `tests/integration/test_microsandbox_driver.py` 镜像 runsc 套件：能力与资源配置逻辑测试
  无条件运行；**真实生命周期测试仅在后端可用时运行**（skip + 理由，绝不伪造）。按
  `shutil.which("krun")`/krunkit 是否可得来门控，遵循禁 mock 铁律。
- 开发回路就绪信号：`WHIRLWIND_DRIVER=microsandbox` + 一个 `colima` VM，让
  "suspend→snapshot→resume" 与池预热路径获得真实的 VM 级本地运行。

## Open points / risk

## 开放点与风险

- libkrun on macOS ships via krunkit (bundled with colima); exact CLI surface for
  `create/exec/pause/snapshot` differs from `runsc` — the driver wraps whatever the bin
  exposes. Needs a **spike on a real host** before implementation (D1 design is final; the
  spike resolves the exact invocation, not whether the driver exists).
- Whether kernel-memory checkpoint/restore works on Apple Silicon libkrun is unproven
  (`snapshot_full` honesty rule decides).

## Implementation phase / 实施阶段

**实施推迟到有真机的 session。** 本 ADR 已把设计定稿并落盘；下一 session 按 TODO P3.5
子步骤实施：

- M0 spike：在有真机的 macOS 上探测 `krun`/krunkit 的可用性与真实 CLI 面（create/exec/pause/snapshot），据此敲定 `MicrosandboxDriver` 对二进制的封装方式。
- M1 驱动骨架：`drivers/microsandbox.py` 实现 `SandboxDriver`，`Isolation.LIGHT_VM`。
- M2 装配：`--sandbox-driver microsandbox` 切换，`control`/`hostlet` 零改动。
- M3 测试：`tests/integration/test_microsandbox_driver.py` 镜像 runsc 套件，渲染逻辑无条件跑、真实生命周期按后端可得 skip。
- M4 文档与记忆：ADR 状态复核、更新 TODO/MEMORY/AGENTS。

**环境检测（本 session 确立的操作约定）：** 执行环境不一定是 macOS；M0/M1/M3 必须先检测
运行时宿主——平台、`shutil.which("krun")`/krunkit、虚拟化支持——再决定执行或如实 skip。
没有 `Virtualization.framework`/真机时，驱动存在但生命周期测试 skip，绝不出 mock 或假声明。
- 更新 [AGENTS.md](../AGENTS.md)（目录索引 + §1.3 + §7 环境检测）、[MEMORY.md](../memory/MEMORY.md) 与 [TODO.md](../TODO.md) P3.5，作为本 ADR 的实施锚点。

---

## Implementation record (2026-08-)

## 实施记录（2026-08）

**What landed / 已落地**（session-log `2026-08-20-microsandbox-driver.md`）：

- **Deviation from the D1 backend wording — the driver wraps the `msb` CLI, not raw
  krun/krunkit.** The open point above ("spike resolves the exact invocation") resolved
  this way: raw libkrun is a C library with no sandbox lifecycle surface, and krunkit is
  a containerd-oriented daemon (the colima VM backend). The microsandbox CLI (`msb`,
  libkrun-based, same upstream) exposes exactly the surface this driver needs —
  `run/exec/stop/start/remove`, `--mount-dir`, `-m`, `--rlimit`, `-e`, `-w`, `--detach` —
  so `MicrosandboxDriver` orchestrates `msb` the same way `RunscDriver` orchestrates
  `runsc` (architecture 8.2 integration level). This changes the wrapped binary, not the
  design: still one `SandboxDriver` implementation, still `Isolation.LIGHT_VM`, still
  zero `control`/`hostlet` changes.
- `scripts/setup/install-microsandbox.sh`: OS-annotated installer (Linux x86_64/aarch64,
  macOS arm64) with SHA256 verification, segmented parallel download, correct versioned
  `libkrunfw.so.<abi>` layout; documents the un-installable runtime prerequisites
  (loaded host kvm module / device passthrough on Linux; no TCG fallback in libkrun).
- `drivers/microsandbox.py` (M1): CLI rendering (`_render_run_argv` / `_render_exec_argv`
  / `_guest_path`), lifecycle over `msb` subprocesses, DATA snapshots as host-side
  workspace copies with merkle roots (the workspace is a host-mounted virtio-fs
  directory by construction — no VM interaction needed, same contract as the process
  driver's seeding path in ADR-0012 D3).
- Wiring (M2): `sandbox.driver=microsandbox` in the config enum + `whirlwind serve
  --driver microsandbox` + `build_driver` composition-root validation — binary probe via
  `shutil.which("msb")` and, on Linux, `kvm_available()` (a real open(2) on `/dev/kvm`);
  named-but-unavailable fails boot loudly, no silent fallback (ADR-0012 D5 rule).
- Tests (M3): `tests/integration/test_microsandbox_driver.py` — rendering/caps/refusal
  logic runs unconditionally (13 tests); real-microVM lifecycle tests gate on
  `which("msb")` + backend and **skip honestly** where `/dev/kvm` does not open.
  `tests/unit/test_runtime_config.py` gains the composition-root branch tests (probes
  stubbed there are branch logic only; the real verdict comes from the gated suite).

**Capability honesty, as probed against the real `msb` 0.6.12 (2026-08-20):**

- `snapshot_full=False`: `msb snapshot create --resumable` — the only memory-state
  surface — returns an explicit unsupported-feature error in v0.6.x ("reserved by the
  public contract"). Flipping this bit requires a passing restore test on a real backend.
- `pause` is honestly a STOP/BOOT cycle, not a memory freeze: processes do not survive,
  workspace data does. The memory-freeze class of behavior is what `snapshot_full=False`
  already declares absent.
- `net_policy=False`: msb ships programmable networking; this driver wires none of it.
- `density=MEDIUM`: one microVM (own kernel + memory) per sandbox.

**What is still pending / 仍待完成：** the two gated lifecycle tests have not executed on
a real backend — the dev container's `/dev/kvm` node exists but open(2) fails ENODEV
(host kvm module not loaded; `msb doctor` agrees: "KVM access unavailable"). They are
written and waiting for a KVM-capable Linux host or an Apple-Silicon mac (HVF). Per the
honesty rule above, `snapshot_full` stays `False` until a real restore passes there.

- **与 D1 措辞的偏差——驱动封装的是 `msb` CLI，而非裸 krun/krunkit。** 上方开放点
  （"spike 敲定确切调用方式"）的裁决是：裸 libkrun 是 C 库、没有沙箱生命周期面；
  krunkit 是面向 containerd 的守护进程（colima 的 VM 后端）。microsandbox CLI
  （`msb`，同样基于 libkrun）恰好暴露本驱动需要的全部表面——`run/exec/stop/start/remove`、
  `--mount-dir`、`-m`、`--rlimit`、`-e`、`-w`、`--detach`——因此 `MicrosandboxDriver`
  编排 `msb` 的方式与 `RunscDriver` 编排 `runsc` 相同（架构 8.2 的集成层级）。变化的
  只是被封装的二进制，不是设计：仍是唯一的 `SandboxDriver` 实现、仍是
  `Isolation.LIGHT_VM`、`control`/`hostlet` 仍零改动。
- `scripts/setup/install-microsandbox.sh`：带 OS 标注的安装器（Linux x86_64/aarch64、
  macOS arm64），SHA256 校验 + 分段并行下载 + 正确的 `libkrunfw.so.<abi>` 版本化布局；
  并如实文档化安装器装不了的运行时前提（Linux 需宿主 kvm 模块已加载 + 设备透传；
  libkrun 无 TCG 回退）。
- `drivers/microsandbox.py`（M1）：CLI 渲染（`_render_run_argv` / `_render_exec_argv` /
  `_guest_path`）、经 `msb` 子进程的生命周期、DATA 快照为宿主侧 workspace 拷贝 +
  merkle 根（workspace 按构造就是宿主挂载的 virtio-fs 目录——无需与 VM 交互，与
  process driver 的播种路径同一契约，ADR-0012 D3）。
- 装配（M2）：config enum `sandbox.driver=microsandbox` + `whirlwind serve --driver
  microsandbox` + `build_driver` 组合根校验——`shutil.which("msb")` 二进制探测，Linux
  上另加 `kvm_available()`（对 `/dev/kvm` 的真实 open(2)）；指名不可用即启动失败、
  绝不静默回退（ADR-0012 D5 规则）。
- 测试（M3）：`tests/integration/test_microsandbox_driver.py`——渲染/能力/拒绝逻辑
  无条件运行（13 项）；真实 microVM 生命周期测试按 `which("msb")` + 后端门控，
  `/dev/kvm` 打不开处**诚实 skip**。`tests/unit/test_runtime_config.py` 增加组合根
  分支测试（那里的探测 stub 只测分支逻辑；真实裁决来自门控套件）。

**能力诚实性（对真实 `msb` 0.6.12 探测，2026-08-20）：**

- `snapshot_full=False`：`msb snapshot create --resumable`——唯一的内存态表面——在
  v0.6.x 返回明确的不支持特性错误（"reserved by the public contract"）。翻转此位
  需要在真实后端上通过 restore 测试。
- `pause` 诚实地是 STOP/BOOT 循环而非内存冻结：进程不存活、workspace 数据存活。
  内存冻结这一类行为已由 `snapshot_full=False` 声明不存在。
- `net_policy=False`：msb 有可编程网络；本驱动未接线任何网络策略。
- `density=MEDIUM`：每沙箱一个 microVM（独立内核 + 内存）。

**仍待完成：** 两个门控生命周期测试尚未在真实后端上执行——开发容器的 `/dev/kvm`
节点存在但 open(2) 返回 ENODEV（宿主 kvm 模块未加载；`msb doctor` 同样报告
"KVM access unavailable"）。它们已写好，等待有 KVM 的 Linux 宿主或 Apple Silicon mac
（HVF）。按上述诚实规则，`snapshot_full` 在真实 restore 通过前保持 `False`。