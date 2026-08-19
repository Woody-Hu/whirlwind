# ADR-0006: Microsandbox driver — a locally-testable VM substrate on the `SandboxDriver` interface

# ADR-0006：Microsandbox driver——`SandboxDriver` 接口上一个可本地测试的 VM 底座

- Status: Accepted (design locked; implementation deferred to a real-host session)
- Date: 2026-08-19
- Related: [ADR-0001](0001-agent-runtime-m1.md) (driver seam D2), [ADR-0002](0002-m3-substrates.md) (runsc/gVisor), [ADR-0005](0005-edge-hardening.md) (resource ceilings), [TODO](../TODO.md) P3.4/P3.5

- 状态：已接受（设计已定稿；实施推迟到有真机的 session）
- 日期：2026-08-19
- 关联：[ADR-0001](0001-agent-runtime-m1.md)（driver 接缝 D2）、[ADR-0002](0002-m3-substrates.md)（runsc/gVisor）、[ADR-0005](0005-edge-hardening.md)（资源上限）、[TODO](../TODO.md) P3.4/P3.5

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