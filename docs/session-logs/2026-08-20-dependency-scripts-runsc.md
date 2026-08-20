# Session: 依赖安装脚本 + runsc 实装与真实 gVisor 测试（2026-08-20）

## 目标

任务 #10/#11：为外部依赖（runsc / PostgreSQL / Redis）生成带 OS 标注的安装部署脚本；在当前 Linux 容器实际安装 runsc，让一直 skip 的 gVisor 集成测试真实跑起来（禁 mock 铁律下，装真二进制是唯一的"变绿"方式）。附带 Loop H（microsandbox Linux 可行性）探测。

## 前置

- AGENTS.md §4.2（skip 不伪造）、§7（环境先检测）；MEMORY 环境事实（runsc rootless 限制）。
- `tests/integration/test_runsc_driver.py`（skipif 门控：`shutil.which("runsc")` + `busybox`）。

## 变更清单

| 文件 | 变更 |
| --- | --- |
| `scripts/setup/install-runsc.sh` | Linux-only（x86_64/aarch64）；分段并行 range GET（实测代理下单流 ~30KB/s → 8 段 ~300KB/s，`SEGMENTS=1` 可退单流）；上游 sha512 校验后安装；rootless 注意事项在头部标注 |
| `scripts/setup/install-postgres.sh` | apt/dnf/brew 三分支，OS 依赖标注；装后 `pg_isready` 自验 |
| `scripts/setup/install-redis.sh` | 同上；`redis-cli ping` 自验 |
| `scripts/setup/README.md` | 脚本 × OS 支持矩阵、解除哪些 skip、不可安装项（/dev/vsock、KVM）诚实声明 |
| `tests/integration/test_runsc_driver.py` | `_spec` 默认 init argv 修复（见下） |

## 关键决策与发现

1. **分段并行下载是代理环境的刚需**：runsc 二进制 105MiB，单流 curl 经代理 ~30KB/s（需 ~1h）；8 段并行 range GET ~300KB/s（~5min）。代理允许多并发连接——脚本将其产品化（sha512 校验兜底完整性）。
2. **busybox applet 陷阱（首次真实暴露）**：安装 runsc 后 3 个用例失败，根因是容器 init `busybox sh -c "sleep 300"` 里的 `sleep` 找不到——bundle rootfs 只有一个 busybox 多调用二进制、无 applet 符号链接，`sh` 按PATH 找独立 `sleep` 可执行文件失败 → init exit 127 → 容器 stopped → 后续 exec/checkpoint 连锁失败（"cannot execute in container in state stopped"）。修复：init 改为直接 applet 调用 `busybox sleep 300`。该测试此前在 mac 上始终 skip，从未真实执行——**skip 欠下的债，装上真二进制就会暴露**。
3. **本容器为 Docker 默认 cap 集**（CapEff=0xa80425fb，无 CAP_SYS_ADMIN）→ platform facts 如实 `restricted=True` → runsc 走 rootless + `--network=none` + `--ignore-cgroups`；restore 用例按上游限制 skip（诚实）。
4. **Loop H（microsandbox Linux 可行性）结论**：容器无 `/dev/kvm`、无 `/dev/vsock`（宿主 CPU 有 vmx/svm 但未透传）→ libkrun 类 LIGHT_VM 底座在本环境不可行，与 ADR-0006「实施推迟到真机 session」的定位一致；Linux 服务器生产路径仍是 runsc（已验证）+ 未来 Firecracker（P3.4）。
5. busybox.net binaries 目录最新为 `1.35.0-x86_64-linux-musl`（1.36.0 路径 404，脚本曾用错后修正）。

## 验证证据

- 环境：Ubuntu 24.04.3，kernel 6.18.5 x86_64；PG 5432 accepting、Redis PONG（已在外部就位，未重复安装）；`CapEff=0xa80425fb`（无 SYS_ADMIN）。
- 安装：`runsc version release-20260817.0`（sha512 OK）、`BusyBox v1.35.0` 落 `/usr/local/bin`。
- 修复前：`tests/integration/test_runsc_driver.py` → FAIL · 3 failed · 5 passed · 1 skipped（原因见发现 2）。
- 修复后：同文件 → **PASS · 8 passed · 1 skipped**（skip = rootless restore，上游限制）。
- 全量（not e2e）：`uv run python scripts/run_tests.py` → **PASS · 330 passed / 9 skipped · exit=0**（较 ADR-0012 基线 327/12：runsc×8 由 skip 变真跑；剩余 skip = k8s×4、vsock×1、rootless restore×1、e2e×3，全部为环境事实）。
- 脚本语法：`bash -n` 三脚本通过。

## 遗留与 handoff

- PG/Redis 本机已就位，`install-postgres.sh`/`install-redis.sh` 未在本机重跑（幂等，但仅在 README 标注验证命令）；下次在新环境首装时验证。
- k8s×4、vsock×1 的 skip 仍留（无 k3s 控制面 / 无 /dev/vsock，均为环境事实）。
- runsc rootless restore：上游限制；若需要真实 FULL 快照恢复验证，需有 CAP_SYS_ADMIN 的环境（colima VM 或裸机）——已有测试会自动解除 skip。
