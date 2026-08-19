# Session: 平台事实核实 + microsandbox 排期文档落盘（2026-08-19）

## 目标

1. 联网核实三个平台事实：runsc/gVisor 能否装进 Docker；macOS 是否支持 k3s；dsh 是否公开仓库。
2. 排期让系统支持 microSandbox（`Isolation.LIGHT_VM`）方便测试——本轮**只文档落盘**，代码实施推迟到有真机的 session。
3. 落一条操作约定：**执行环境不一定是 macOS，运行前先检测所在环境**（同步进 AGENTS / ADR / MEMORY）。

## 前置

- 阅读 [ADR-0006 草稿](0006-...)（microsandbox，基于 libkrun/krunkit）。
- [docs/TODO.md](../TODO.md) P3.5 已有 microsandbox 条目。
- [docs/memory/MEMORY.md](../memory/MEMORY.md) 为项目记忆载体。

## 变更清单

| 文件 | 变更 |
| --- | --- |
| [ADR-0006](../adr/0006-microsandbox-driver.md) | Status Draft→Accepted（设计定稿、实施待真机）；补充 2026 已核实的 krunkit 佐证 + 新增「Implementation phase / 实施阶段」（M0~M4）与「环境检测」约定 |
| [TODO.md](../TODO.md) P3.5 | [~] in progress；拆 M0-doc / doc-sync / M0 spike / M1 驱动 / M2 装配 / M3 测试 / M4 记忆 七个子步骤，前两步勾选完成 |
| [AGENTS.md](../AGENTS.md) | §1.3 加"已定稿待实施：microsandbox"；目录索引 drivers/ 追加 microsandbox.py；§7 头部加「环境先检测，不预设平台」约定并扩充 gVisor/microsandbox/k3s/dsh 词条 |
| [MEMORY.md](../memory/MEMORY.md) | 环境事实追加"已核实（2026-08，联网）"；进行中登记 microsandbox 设计已定稿/实施推迟；决策索引登记 ADR-0006 |

## 关键决策与发现

- **核实 1（runsc in Docker）**：gVisor 官方安装文档确认在 Linux VM 内 `runsc install` + `daemon.json` 即可作为 Docker runtime（`docker run --runtime=runsc`，宿主内核 ≥ 4.14.77）；mac 上的 Linux VM 由 colima 提供；官方还支持 Docker-in-gVisor（`runsc install --runtime=docker-in-gvisor`）。
- **核实 2（macOS k3s）**：`colima start --kubernetes` 底层就是 k3s，colima 文档/仓库均验证。此前"不支持"确是无头沙箱执行环境的假象，非平台问题。
- **核实 3（dsh）**：`github.com/deepseek-ai/deepseek-harness`，MIT，公开，当前 0.1.0-rc.5（2026-08-13）；可通过 `npx @deepseek-ai/dsh web` 或源码运行。
- **新增佐证**：colima 原生支持 `--vm-type krunkit`（v0.10.0+），libkrun/krunkit 是 Apple Silicon 一等 VM 后端——正是 ADR-0006 microsandbox 选用的后端，说明该底座在 M 芯片 mac 上今天即可本地跑。
- **设计结论**：microsandbox 是 `Isolation.LIGHT_VM`（真实内核 + 真实 VM 边界），复用 `SandboxDriver` 接缝，`control`/`hostlet` 零改动，运行时可切——与 runsc/Firecracker 形成隔离谱系在 mac 上的开发/生产对应。
- **操作约定（用户明确提出）**：开发/执行环境不一定是 macOS，凡 VM/容器级工作流（krun、docker、k3s、runsc）执行前先检测环境（平台 / `shutil.which` / virtualization），再决定路径或如实 skip；禁止把"当前沙箱不可用"误判为"平台不支持"。

## 验证证据

- 三处联网搜索均在 2026-08 命中了权威来源：
  - gVisor 官方安装文档 https://gvisor.dev/docs/user_guide/install/ 与 gVisor blog 2026-04-15「Multi-Agent gVisor Isolation (MAGI)」。
  - colima 仓库 https://github.com/abiosoft/colima 与配置文档 https://colima.run/docs/configuration/（k3s 经 `--kubernetes`；krunkit 经 `--vm-type krunkit`）。
  - https://github.com/deepseek-ai/deepseek-harness（MIT，0.1.0-rc.5）。
- 文档变更为纯 markdown 落盘，无代码/无测试运行；本轮明确不做实施（避免在无真机环境伪造 krun CLI）。

## 遗留与 handoff

- **Microsandbox 代码未实施**：M0 spike（真机探测 krunkit CLI）→ M1 `drivers/microsandbox.py` → M2 `--sandbox-driver microsandbox` → M3 `test_microsandbox_driver.py`（后端可得才跑真实生命周期，无 mock）→ M4 记忆回收。见 TODO P3.5 子步骤。
- **无头沙箱限制**：本环境无法执行 `krun`（需 `Virtualization.framework`/真机）；下一 session 若在本机（真实 macOS）进行 M0 spike。
- **约定落地检查**：AGENTS §7 的环境检测约定 + ADR-0006 实施阶段的 M0~M4 是本 session 最重要的 handoff 锚点。
- 提醒：README 中测试计数是历史数据；引用性能数字必须注明 ADR/实测来源。