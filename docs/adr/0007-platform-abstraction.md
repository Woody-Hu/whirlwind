# ADR-0007: Platform abstraction — one facts object + pluggable per-platform behaviour

# ADR-0007：平台抽象——统一事实对象 + 可插拔的平台行为插件

- Status: Accepted
- Date: 2026-08-19
- Related: [ADR-0001](0001-agent-runtime-m1.md) (D2 driver seam), [ADR-0002](0002-m3-substrates.md) (Linux-only runsc), [ADR-0005](0005-edge-hardening.md) (D1 resource ceilings + honesty note), [ADR-0006](0006-microsandbox-driver.md), [AGENTS.md](../../AGENTS.md) §3.3 / §7

- 状态：已接受
- 日期：2026-08-19
- 关联：[ADR-0001](0001-agent-runtime-m1.md)（D2 driver 接缝）、[ADR-0002](0002-m3-substrates.md)（runsc 仅 Linux）、[ADR-0005](0005-edge-hardening.md)（D1 资源上限与诚实性注记）、[ADR-0006](0006-microsandbox-driver.md)、[AGENTS.md](../../AGENTS.md) §3.3 / §7

---

## Context

## 背景

Platform-dependent branches were hand-rolled at every site, each with its own detection and its own honesty caveat:

平台相关的分支此前散落在各处手写，各自探测、各写一套 caveat：

| Site / 位置 | Branch / 分支 |
| --- | --- |
| `drivers/process.py` | RLIMIT_AS soft=hard 在 macOS 被内核拒绝（静默不生效），在 Linux 是真实执行 |
| `transport/transports.py` | `vsock_available()` 自行探测 `AF_VSOCK` + `/dev/vsock` |
| `imaging/base.py` | `_platform_tag()` 直接读 `sys.platform` / `platform.machine()` |
| `tests/integration/test_process_driver.py` | `RLIMIT_AS_SUPPORTED = sys.platform != "darwin"` |
| `tests/benchmark/test_edge_bench.py` | `RES_LIMIT_PROFILE_SUPPORTED = sys.platform != "darwin"` |
| `tests/integration/test_runsc_driver.py` | `IS_RESTRICTED` 自行读 `/proc/self/status` 判 CAP_SYS_ADMIN |
| `tests/integration/test_transport.py` | UDS 路径 104 字节上限仅存在于注释中 |

Consequences: duplicated knowledge, no single place to answer "which system am I on", no way to simulate another OS family when developing/testing pure logic paths, and honesty caveats copy-pasted instead of centralized.

后果：知识重复；没有唯一入口回答"当前是什么系统"；开发/测试纯逻辑路径时无法模拟另一个 OS 家族；诚实性 caveat 靠复制粘贴而非集中治理。

## Decision — D1: `PlatformFacts`, one frozen value object in `core/`

## 决策——D1：`core/` 中的唯一冻结值对象 `PlatformFacts`

New module `src/whirlwind/core/platform.py` (core = dependency bottom, importable by drivers/transport/imaging/tests alike):

新增模块 `src/whirlwind/core/platform.py`（core 为依赖最底层，drivers/transport/imaging/tests 均可导入）：

```python
@dataclass(frozen=True, slots=True)          # 接口层值对象（AGENTS §3.2）
class PlatformFacts:
    system: str        # "macos" | "linux" | "windows" | "unknown"
    machine: str       # "arm64" | "x86_64" | 原始 platform.machine()
    vsock: bool        # 真实探测：AF_VSOCK + /dev/vsock（永不被环境变量覆盖）
    restricted: bool   # 真实探测：Linux 受限容器（非 root 或缺 CAP_SYS_ADMIN）

    # OS 家族语义事实（由 system 派生，随覆盖值走）
    rlimit_as_supported -> bool   # 仅 Linux（macOS 内核拒绝 soft=hard）
    uds_path_max -> int           # macOS 104 / 其他 108（AF_UNIX sun_path）
    overridden -> bool            # WHIRLWIND_PLATFORM 是否显式生效（诚实信号）
```

`detect_facts()` = real detection; `current_facts()` = detection with the D2 override applied. **Not cached** — probes are stat-level cheap and tests change the env between calls.

`detect_facts()` 为真实探测；`current_facts()` 为应用 D2 覆盖后的事实。**不做缓存**——探测是 stat 级开销，且测试会在调用之间改变环境变量。

## Decision — D2: `WHIRLWIND_PLATFORM` env override — identity simulation with a hard honesty boundary

## 决策——D2：`WHIRLWIND_PLATFORM` 环境变量覆盖——带硬性诚实边界的身份模拟

```
WHIRLWIND_PLATFORM = auto (default/unset) | macos | linux | windows [ / machine ]
```

- Overrides **identity only**: `system` (and optionally `machine`), plus everything derived from identity (`rlimit_as_supported`, `uds_path_max`, D3 plugin dispatch).
- **Never overrides probes**: `vsock` and `restricted` always reflect the real host. An override cannot fabricate a device, a binary, or kernel enforcement — real-execution tests keep gating on real probes (`shutil.which`, `/dev/vsock`), so a simulated platform can never turn an absent substrate green. This is the §4.2 no-fake rule applied to platform simulation.
- Invalid values raise `ValueError` at `current_facts()` (typos must be loud, not silent).
- Purpose: dev/test of *logic paths* (policy decisions, rendering, dispatch) on another OS family; production leaves it unset.

- 只覆盖**身份**：`system`（及可选 `machine`），与由身份派生的一切（`rlimit_as_supported`、`uds_path_max`、D3 插件分发）。
- **从不覆盖探测**：`vsock` 与 `restricted` 永远反映真实宿主。覆盖不能伪造设备、二进制或内核执行——真实执行类测试仍按真实探测门控（`shutil.which`、`/dev/vsock`），模拟平台永远无法让缺失的底座变绿。这是 §4.2 禁伪造铁律在平台模拟上的应用。
- 非法值在 `current_facts()` 抛 `ValueError`（拼错必须响亮，不能静默）。
- 用途：在另一 OS 家族上开发/测试**逻辑路径**（策略决策、渲染、分发）；生产环境不设置。

## Decision — D3: `@platform_impl` / `resolve_impl` — behaviour plugins dispatched on the active system key

## 决策——D3：`@platform_impl` / `resolve_impl`——按当前系统键分发的行为插件

```python
@platform_impl("drivers.process.rlimits", "*")      # 默认实现（POSIX/Linux 语义）
def _rlimits_posix(res): ...

@platform_impl("drivers.process.rlimits", "macos")  # 平台特化实现
def _rlimits_macos(res): ...

limits = resolve_impl("drivers.process.rlimits")(res)
```

- Registry keyed `(feature, platform-key)`; `resolve_impl` dispatches on `current_facts().system`, falls back to the `"*"` impl, raises `PlatformImplError` (stable code `whirlwind/platform/impl-not-found`) when nothing fits.
- Registration happens at import time of the defining module — **imports are the wiring**, no dynamic discovery, no DI container (consistent with the repo's explicit-assembly stance).
- Policy resolution happens in the **parent**; only the precomputed plan crosses into `preexec_fn` (async-signal-safety: no env reads/probes between fork and exec).

- 注册表按 `(feature, platform-key)` 键控；`resolve_impl` 按 `current_facts().system` 分发，回退到 `"*"` 实现，无匹配时抛 `PlatformImplError`（稳定错误码 `whirlwind/platform/impl-not-found`）。
- 注册发生在定义模块的导入期——**导入即接线**，无动态发现、无 DI 容器（与仓库显式装配的立场一致）。
- 策略解析发生在**父进程**；只有预计算的计划进入 `preexec_fn`（异步信号安全：fork 与 exec 之间不做环境读取/探测）。

## Decision — D4: migration closed by this ADR

## 决策——D4：本 ADR 收编的迁移点

| Site / 位置 | Before / 迁移前 | After / 迁移后 |
| --- | --- | --- |
| `drivers/process.py` | `_apply_rlimits` 无条件写 RLIMIT_AS（macOS 靠静默吞错） | 插件 `drivers.process.rlimits`：macOS 实现诚实剔除 RLIMIT_AS，Linux/`*` 全量应用 |
| `transport/transports.py` | 自行探测 AF_VSOCK + /dev/vsock | `vsock_available()` 委托 `current_facts().vsock` |
| `imaging/base.py` | `sys.platform` / `platform.machine()` | `current_facts().system` / `.machine` |
| `tests/...test_process_driver.py` | `sys.platform != "darwin"` | `current_facts().rlimit_as_supported` |
| `tests/...test_edge_bench.py` | `sys.platform != "darwin"` | `current_facts().rlimit_as_supported` |
| `tests/...test_runsc_driver.py` | 自读 `/proc/self/status` 判 CAP_SYS_ADMIN | `current_facts().restricted` |

Observable behaviour on real hosts is unchanged (macOS: RLIMIT_AS was silently unenforced, now explicitly dropped by policy; Linux: identical). What changes is *where the knowledge lives* and *what can be simulated*.

真实宿主上的可观测行为不变（macOS：RLIMIT_AS 此前静默不生效，现在由策略显式剔除；Linux：完全一致）。变化的是**知识所在的位置**与**可模拟的范围**。

## Test strategy

## 测试策略

`tests/unit/test_platform.py` (pure logic, runs everywhere):

- facts derivation on both OS families through the env override (monkeypatched env — the override is the SUT's own feature, not a mock of it);
- `system[/machine]` parsing, invalid value → `ValueError`;
- honesty boundary: override to `linux` never flips `vsock`/`restricted` away from the real probe;
- plugin dispatch: per-system impl wins, `"*"` fallback, unregistered feature raises;
- `overridden` flag tracks the env.

Existing gates migrate to facts; skip behaviour on real hosts must be identical (macOS skips stay skips).

`tests/unit/test_platform.py`（纯逻辑，全平台运行）：两 OS 家族的事实派生（monkeypatch 环境变量——覆盖本身就是被测特性，不是 mock）；`system[/machine]` 解析与非法值；诚实边界（覆盖为 `linux` 永不改变 `vsock`/`restricted` 的真实探测值）；插件分发（平台特化优先、`*` 回退、未注册报错）；`overridden` 标志。既有门控迁移到 facts，真实宿主上的 skip 行为必须一致（macOS 的 skip 依旧 skip）。

## Conflict check

## 冲突检查

No conflict with architecture v0.6: this adds no layer and no harness-facing surface; it is a cross-cutting core module below every consumer. AGENTS.md §3.3 gains one coding rule (no direct `sys.platform` branching; go through `core/platform`).

与架构 v0.6 无冲突：不新增层次、不新增 harness 可见面；它是位于所有消费方之下的横切 core 模块。AGENTS.md §3.3 增加一条编码规则（禁止直接 `sys.platform` 分支，统一走 `core/platform`）。

## Implementation order

## 实施顺序

1. `core/platform.py` + core exports；
2. src 迁移（D4 表）；
3. `tests/unit/test_platform.py` + 既有门控迁移；
4. AGENTS.md（§2 索引 / §3.1 接口速查 / §3.3 编码规则 / §7 环境速查）。

## Risks / open points

## 风险与开放点

- The override is process-global state: tests must monkeypatch the env per-case (facts are uncached by design, so this works); long-running servers should never set it.
- `restricted` is Linux-container semantics only; on macOS it is `False` (runsc gating there is the binary probe, which is correct).
- 覆盖是进程全局状态：测试需逐例 monkeypatch（facts 故意不缓存，因此可行）；长驻服务不得设置。
- `restricted` 仅是 Linux 容器语义；macOS 上为 `False`（那里的 runsc 门控是二进制探测，本就正确）。
