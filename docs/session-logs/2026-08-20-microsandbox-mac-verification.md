# Session: microsandbox 第三底座——Apple Silicon (HVF) 真实 VM 生命周期验证 + benchmark + --replace 修复（2026-08-20）

## 目标

把 P3.5/ADR-0006 的 M4 遗留项收尾：在 **Apple Silicon mac（HVF）** 上真实执行此前一直 skip 的 microsandbox 生命周期套件，并补充 benchmark（冷启动 / exec 延迟 / DATA checkpoint）。过程中修正驱动与真实 msb 0.6.8 CLI 的差异，解决跨平台路径问题（macOS 符号链接、MSB_HOME/libkrunfw），最后更新文档与记忆。

## 前置

- ADR-0006（Implemented，M4 遗留真实 VM 验证）、上一 session-log `2026-08-20-microsandbox-driver.md`（Linux 容器 KVM ENODEV 实录）。
- 本机：macOS arm64、msb **0.6.8**（`~/.local/bin/msb` → `~/.microsandbox/bin/msb`，`msb doctor` 全 ✓：libkrunfw / Apple Silicon / HVF）、本地 docker（busybox 镜像 `rancher/mirrored-library-busybox:1.36.1` 已缓存）。
- 测试框架约定：经 `scripts/run_tests.py`（ADR-0008）；guest 载荷用 docker 导出 busybox rootfs（MEMORY 已记的「规范化验证路径」）。

## 变更清单

| 文件 | 变更 |
| --- | --- |
| `src/whirlwind/drivers/microsandbox.py` | `_render_run_argv` 恒增 `--replace`：防 `msb run` 对已存在名称**静默复用**旧 VM（创建标志被忽略的 spec 诚实性陷阱），create() 强制执行请求 spec |
| `tests/integration/test_microsandbox_driver.py` | 渲染测试断言 `--replace`；`_msb_cleanup` 语义注释修正（0.6.x 是 warn+复用而非 refuse）；删除两处生命周期测试里已冗余的预清理（驱动自愈）；新增 `test_create_replaces_stale_record`（同一 id 重创、经 guest env 交换证明替换而非复用）；`extractall` 加 `filter="fully_trusted"`（修 3.14 deprecation，且默认 "data" filter 会拒绝 /etc/mtab 绝对符号链接） |
| `tests/benchmark/test_microsandbox_bench.py` | 新增/修正：冷启动改为**只对 create() 计时**（原先连 destroy 一起计，175ms 虚高）；验收线按实测收紧（500/50/50ms，~3x 余量）；去掉冗余 `_msb_cleanup` |
| 文档 | ADR-0006 状态 → Implemented + M4 verified + 验证记录；TODO P3.5 → M4 DONE + Done 区更新；MEMORY（快照/进行中/环境事实/基线）；AGENTS §1.3/§2 目录/§7 |

## 关键决策与发现

1. **TRAE 沙箱拦截默认 MSB_HOME 写入 → 首个失败是环境假象。** `msb run` 需写 `~/.microsandbox/db/`，沙箱报 `hit restricted`；测试里 `finally: destroy` 把 `create` 的 DriverError 掩盖成 `SandboxNotFound`。解法：`MSB_HOME=/tmp/whirlwind-msb-home` + **驱动已解析的真实二进制路径**（`~/.microsandbox/bin/msb`），binary-relative 仍能找到 libkrunfw。
2. **`MSB_HOME` 重定向到空目录会丢 libkrunfw（实测）**：msb 查找顺序含 `MSB_HOME/lib`。直接 `msb`（未解析路径）在临时 MSB_HOME 下 `doctor` 报 libkrunfw not found；用解析后二进制 + 临时 MSB_HOME 则全 ✓。→ 驱动 resolve() 二进制的既有修复是跨平台必需的，不是过度设计。
3. **`msb run --detach` 返回即 guest 就绪**：create 后立即第一次 `msb exec` 以稳态延迟成功（实测 run=182ms / 首次 exec=23ms vs 稳态 ~20ms）。驱动无需就绪等待循环。
4. **`--replace` 是 spec 诚实性修复（核心发现）**：不带 `--replace` 时，`msb run` 对已存在名称**不失败**，仅 warn 并静默复用旧沙箱、**创建标志（env/bundle/resources）被忽略**（0.6.8 实测 rc=0）。崩溃遗留陈旧 store 记录会让 create() 静默返回不符合请求 spec 的旧 VM。驱动恒传 `--replace` 后，同一 id 重创可观察 env 从 MARKER=first 换成 second。`_msb_cleanup` 注释此前声称"refuse"，与真实行为不符，已修正。
5. **tar 解包 filter 选择**：`extractall` 默认无 filter 在 3.14 会弃用；"data" filter 拒绝 busybox rootfs 的绝对符号链接（`/etc/mtab` → ENOTDIR/报错）。用 `filter="fully_trusted"`（= 旧行为显式化），载荷是受信本地镜像，诚实合理。
6. **benchmark 测量语义**：首版把 create+destroy 一起计时（p50=175ms）——与"沙箱创建预算"契约不符。改为只计 create() 后 p50=**116ms**。数值必须可复核、与契约一致（§4.3）。

## 验证证据

- 环境：macOS arm64；`msb 0.6.8`（`msb doctor` 全 ✓：libkrunfw / Apple Silicon / HVF）；`MSB_HOME=/tmp/whirlwind-msb-home`；`PATH` 含 `~/.local/bin`；docker daemon 20.10.7 + busybox 1.36.1 镜像。
- 手工探测（真实 msb CLI，非 mock）：
  - 不带 `--replace` 二创同 id → warn + 复用（rc=0，创建标志被忽略）；
  - 带 `--replace` 二创同 id → env MARKER=first→second 生效（exec 验证）。
- 单文件：`run_tests.py tests/integration/test_microsandbox_driver.py tests/benchmark/test_microsandbox_bench.py -q` → **PASS · 19 passed · exit=0**（13 渲染/能力/拒绝 + 3 真实 VM 生命周期 + 3 benchmark，全部真跑，0 skip）。
- benchmark（真实 HVF，10 轮）：冷启动（create only）p50=**116ms** p90=139ms min=108ms max=139ms；exec p50=**11ms** max=14ms；DATA checkpoint p50=**1ms**（~1MiB workspace）。
- 全量回归（本 mac，`run_tests.py -q`）：**336 passed · 26 skipped · exit=0**。skip 全为环境事实：无本地 PG×9、k3s 控制面×4、runsc×4、vsock×1、RLIMIT_AS(macOS 诚实事实)×1、e2e×3 等。

## 遗留与 handoff

- **Linux-KVM 路径仍未执行**（本 mac/容器均无 KVM 宿主）——生命周期套件在 Linux 上仍会诚实 skip；`snapshot_full` 在任意后端有通过 restore 测试前保持 False。
- **k3s 可选验证**：用户允许用本地 k3s/docker 验证，但 microsandbox 驱动不在 k3s 内运行，k3s manifest 测试与本次变更无关；本 session 仅用了 docker（guest rootfs 导出）。如需补 k3s 冒烟，`deploy/k3s/dev-server.sh` / `colima start --kubernetes` 即可，本 mac 未起。
- 若在**非 TRAE 沙箱**的真机跑，`MSB_HOME` 用默认 `~/.microsandbox` 即可（沙箱拦截是本次特有的环境因素）；临时 MSB_HOME 时必须配合解析后的 msb 二进制路径（驱动已内置）。
- 后续可考虑：exec 延迟（11ms）主导项是每次 `msb exec` 子进程+CLI 启动；若需更低延迟，需持久化命令通道（超出当前驱动契约，未做）。
