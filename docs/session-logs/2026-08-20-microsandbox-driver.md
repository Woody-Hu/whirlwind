# Session: microsandbox 第三底座落地——安装器 / 驱动 / 装配 / 门控测试（2026-08-20）

## 目标

P3.5（ADR-0006）：把 microsandbox（libkrun 微 VM，`Isolation.LIGHT_VM`）从「定稿待实施」推进到「已落地」——安装脚本、`MicrosandboxDriver`、组合根装配、门控集成测试，并在本容器诚实探测 Linux 可行性（用户明确要求复核"需要 KVM"的旧结论）。同时恢复被重置的环境基线（runsc/busybox/PG/Redis）。

## 前置

- ADR-0006（设计定稿 @ Accepted，M0-M4 实施计划）、ADR-0007（平台事实/诚实边界）、ADR-0012 D5（底座钉选，指名不可用即拒绝启动）。
- `drivers/runsc.py` + `tests/integration/test_runsc_driver.py`（CLI 编排型 driver 的样板）。
- `scripts/setup/install-runsc.sh`（分段并行下载 + 校验和的安装器样板）。
- 上一 session 结论「本容器无 /dev/kvm 透传 → libkrun 不可行」待复核。

## 变更清单

| 文件 | 变更 |
| --- | --- |
| `scripts/setup/install-microsandbox.sh` | 新增：msb CLI + libkrunfw（pin v0.6.12，SHA256 校验，分段并行下载，版本化 `libkrunfw.so.<abi>` 布局）；OS 依赖（Linux x86_64/aarch64 需 glibc ≥ 2.28；macOS arm64）与**不可安装项**（宿主 kvm 模块/设备透传）在头部诚实标注；装后 `msb doctor` 自验 |
| `scripts/setup/install-runsc.sh` | 修复分段拼装 bug（见发现 4） |
| `scripts/setup/README.md` | 登记新脚本 + KVM 前提注记 |
| `src/whirlwind/drivers/microsandbox.py` | 新增（M1）：`MicrosandboxDriver` 封装 msb CLI；`_render_run_argv`/`_render_exec_argv`/`_guest_path` 纯渲染；`kvm_available()` 真实 open(2) 探测；DATA 快照 = 宿主侧树拷贝 + merkle 根 |
| `src/whirlwind/drivers/__init__.py` | 导出 `MicrosandboxDriver` |
| `src/whirlwind/config.py` | `sandbox.driver` enum + `microsandbox` |
| `src/whirlwind/cli.py` | `serve --driver` choices + `microsandbox` |
| `src/whirlwind/runtime.py` | `build_driver` microsandbox 分支：`which("msb")` + Linux `kvm_available()` 组合根探测，失败即拒绝启动（无静默回退） |
| `tests/integration/test_microsandbox_driver.py` | 新增（M3）：13 项无条件（渲染/能力/拒绝）+ 2 项真实 VM 生命周期（msb+KVM 门控） |
| `tests/unit/test_runtime_config.py` | +4 组合根分支测试（msb 缺失 / KVM 缺失 / 构造 / delta 模式拒绝） |
| `deploy/whirlwind.example.toml` | driver 注释更新 |
| 文档 | ADR-0006 状态 → Implemented + 实施记录（含 msb-vs-krunkit 偏差）；TODO P3.5 勾选；MEMORY；AGENTS §1.3/§7 |

## 关键决策与发现

1. **复核结论：microsandbox 在本容器"可安装、不可运行"——旧结论部分修正。** msb 0.6.12 经安装脚本真实装入（`msb --version` OK、libkrunfw 解析 OK）；但 `/dev/kvm` 节点存在而 open(2) 返回 **ENODEV**（宿主内核未加载 kvm 模块——mknod 无解，这不是透传问题而是模块缺失）。`msb doctor` 诚实输出 "✗ KVM access unavailable"。libkrun 无 TCG/QEMU 回退。**用户"当前系统应该可以安装运行"的判断对了一半：安装可行，运行需要宿主 kvm 模块。** VM 生命周期测试因此诚实 skip（M4 遗留：KVM 宿主或 Apple Silicon mac 执行）。
2. **后端选型偏差（已记入 ADR-0006 实施记录）：驱动封装 `msb` CLI 而非原设计的裸 krun/krunkit。** 裸 libkrun 是 C 库（无沙箱生命周期面）；krunkit 是面向 containerd 的守护进程（colima VM 后端）；`msb` 恰好暴露全部所需表面（run/exec/stop/start/remove、`--mount-dir`、`-m`、`--rlimit`、`-e`、`-w`、`--detach`，经 `--help` 逐项核实）——与 `RunscDriver` 编排 runsc 同构。
3. **能力诚实性以真实 CLI 探测为准**：`msb snapshot create --resumable` 在 v0.6.x 返回明确 unsupported-feature 错误 → `snapshot_full=False`；`pause` = STOP/BOOT 循环（进程不存活、workspace 存活）；msb 有可编程网络但驱动未接线 → `net_policy=False`；每沙箱一个 microVM → `density=MEDIUM`。
4. **既有安装器隐藏 bug 暴露**：`cat "$(ls "${dest}".part* | sort -V)"` 引号使多行输出成单参数 → 拼装必失败（`install-runsc.sh` 与 msb 脚本同模式）。改为按序号循环 `cat`。环境重置后首跑即触发——上 session 大概率手工绕过未回写。修复后 runsc 实装复验 sha512 OK。
5. **环境重置的恢复成本**：容器重建后 runsc/busybox/PG 角色全丢；PG 库 `whirlwind_test` 为镜像预置（owner=postgres），`createdb -O whirlwind` 被存在性检查跳过 → PG15+ public schema 权限拒绝（1 failed + 8 errors 同根因）。修复：`ALTER DATABASE ... OWNER TO whirlwind` + `GRANT ALL ON SCHEMA public`。**教训：安装脚本自验应包含"库归属/权限"而非仅"可达"。**
6. zsh 的 `/dev/tcp` 重定向不可用（zsh 无该 bash 特性）——TCP 探测假阴性，须用 `pg_isready`/`redis-cli ping` 等真实工具。

## 验证证据

- 环境：Ubuntu 24.04 容器；`msb 0.6.12`（安装器实装）、`runsc release-20260817.0` + busybox 1.35.0（重装修复后 sha512 OK）、PG 16（127.0.0.1:5432 accepting，owner 修复）、Redis PONG。
- `msb doctor`：libkrunfw ✓ / CPU virt vmx ✓ / **KVM access ✗ unavailable**（ENODEV 实录）。
- 单文件：`run_tests.py tests/unit/test_runtime_config.py tests/integration/test_microsandbox_driver.py` → **PASS · 21 passed · 2 skipped**（skip = 2 项 VM 生命周期，KVM ENODEV）。
- 实机诚实失败路径：`uv run whirlwind serve --driver microsandbox` → 启动即 `ValueError: ... requires a working /dev/kvm on Linux ... no silent fallback`（exit=1）。
- 全量（not e2e，环境恢复后）：`uv run python scripts/run_tests.py -q` → **PASS · 347 passed · 11 skipped · exit=0**（基线 330/9 → +17 passed = 4 单测 + 13 集成；+2 skipped = msb VM ×2；剩余 skip = k8s×4、vsock×1、rootless restore×1、e2e×3，均为环境事实）。日志 `.test-logs/20260820-070737--q.log`。
- 脚本语法：`bash -n` 两脚本通过。

## 遗留与 handoff

- **M4（唯一遗留）**：2 项门控 VM 测试（lifecycle end-to-end + 独立 guest kernel 验证）待有 KVM 的 Linux 宿主或 Apple Silicon mac（HVF）执行；届时 virtio-fs 写一致性、`-m`/`--rlimit` 的真实执行才被证实。`snapshot_full` 在真实 restore 通过前保持 False（`msb --resumable` v0.6.x 不支持，大概率不翻）。
- macOS arm64 安装分支为结构性未测（Linux 容器内无法验证 dylib 布局），需真机 `msb doctor` 复核。
- 下一个生产化候选（按 TODO）：P2 可观测性（/metrics、结构化日志）或 P1.1 网关认证（租户维度前置）。
- 环境若再重置：runsc/busybox 用 `scripts/setup/install-runsc.sh`；PG 需补 owner 修复（本 session 发现 5 的命令）；Redis apt 安装即起。
