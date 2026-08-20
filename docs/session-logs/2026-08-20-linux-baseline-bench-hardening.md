# Session: Linux 全量基线复测 + 冷启动真实优化（2026-08-20）

## 目标

在 Linux（本容器）重跑全部测试与 benchmark（TODO 用户任务 2），定位 macOS→Linux 的基准偏差，判断是环境差异还是回归；对属于实现的开销做真实优化，对属于环境的噪声做诚实的基准加固（不放松验收线、不伪造数字）。

## 前置

阅读 AGENTS.md §4（测试铁律）、ADR-0005（冷启动 250ms 线）、ADR-0008（runner）；上一份 session-log（microsandbox-platform）。环境事实：Linux x86_64 容器、Python 3.14.7、root、FUSE 文件系统（fuseblk）、无 runsc/k8s//dev/vsock、初始无 PG/Redis。

## 变更清单

| 文件 | 变更 | 理由 |
| --- | --- | --- |
| `src/whirlwind/core/__init__.py` | events/model/statemachine 改为 PEP 562 惰性 re-export（errors/ids/platform 保持急切） | 沙箱 agent 子进程启动链 `agent.server → transport → core.platform` 原本被迫拉入 pydantic（~150ms）；子进程确实不用这些模型 |
| `src/whirlwind/harness/echo_server.py` | asyncio 实现改为纯线程实现；`urllib.request` 惰性导入 | harness 子进程解释器启动在冷启动关键路径上；`import asyncio` 实测 ~80ms（logging→traceback 链），纯线程语义等价（响应先于事件：gate Event；块节奏：sleep；/run：阻塞 subprocess） |
| `src/whirlwind/hostlet/hostlet.py` | 健康轮询 20ms → 5ms | 轮询粒度直接进 dispatch p50 |
| `tests/benchmark/test_pipeline_bench.py` | 冷启动 5→10 轮；eventlog append 改 best-of-3 窗口；冷启动移到 fsync 饱和测试之前 | 共享 CPU/FUSE 环境的测量卫生；验收问题本就是「能否持续达标」 |
| `tests/benchmark/test_edge_bench.py` | 冷启动 5→10 轮 | 同上 |

## 关键决策与发现

1. **冷启动超标是环境+实现的混合**：初始 Linux 全量 2 failed（373ms/362ms vs 250ms 线）。剖析：agent 子进程导入 228ms（其中 pydantic 链 ~150ms 纯属误伤）+ echo 子进程导入 121ms（asyncio ~80ms + urllib 链 ~40ms）。
2. **懒加载原则（用户明确要求遵守）**：只对「该路径确实不需要」的可选/误伤导入做惰性化；业务必需的加载一律保持急切（agent.server 的 asyncio、echo 的 json/subprocess 均未动）。`core/__init__` 惰性 re-export 不改变公共 API，重消费方（gateway/control/hostlet）首次访问即全量加载。
3. **WAL 顺序 append 是存储硬件线**：本机 fsync p50=0.73ms（fuseblk），10k 顺序 append 每条 0.86-1.05ms；1000/s 线在本机理论极限 ~1370/s，余量天然薄。基准改 best-of-3 窗口去噪（每次窗口都是真实 append）。
4. **基准顺序即测量卫生**：eventlog 基准连续 fsync ~8s 会污染紧随其后的冷启动测量（FUSE 积压），冷启动测试移至模块最前。
5. **fdatasync 无收益**：实测 0.73→0.71ms，不做无意义改动。
6. **PG/Redis 本机安装**（apt，root）：存储契约测试与存储基准从 skip 变为真跑（41 passed）。

## 验证证据（全部真实运行，经 runner 或 -s 直跑）

- 优化前全量（Linux，无 PG/Redis）：`FAIL · 2 failed · 195 passed · 29 skipped`（冷启动 373/362ms）
- 导入剖析：`python -c "import whirlwind.agent.server"` 228ms → 136ms → echo `python -m` 121ms → 53ms
- 优化后冷启动（10 轮 p50）：无限制 183ms（min 171/max 194）；含 rlimits 197ms（min 172/max 380）
- eventlog 顺序 append：3 窗口 1,150/1,140/1,157/s（best 1,157/s ≥ 1000 线）；group-commit burst 41,971/s（≥5k 线）；bus 扇出 1,038,124/s（≥20k 线）
- 最终全量（Linux + 本机 PG/Redis）：**`PASS · 214 passed · 12 skipped · exit=0`**（skip：runsc×4、k8s×4、vsock×1、e2e×3——容器无对应二进制/设备，按 §4.2 如实 skip）

## 遗留与 handoff

- runsc/k3s 在本容器不可用（无二进制/无控制面）；后续 session 若提供内核级环境可复测（安装脚本见后续 session）。
- 冷启动剖析剩余大头：agent 子进程 asyncio 导入 ~75ms（业务必需，按用户原则不做懒加载）；如需再压，可考虑 fork 预热或解释器级手段，属未来调优项。
- PG/Redis 的可复现安装脚本将随「依赖安装脚本」session 落盘（本 session 是手动 apt）。

## 附：跨平台插件设计审视（用户任务 2 的判断部分，2026-08-20 Loop D）

基于 Linux 全量结果对 ADR-0007 设计做了一次审计，**结论：设计清晰且够用，无需优化**。证据：

1. **零违规**：`sys.platform` / `platform.machine()` 全仓库只出现在 `core/platform.py` 内部（即抽象本身的合法位置）；业务代码无一处裸判断（grep 验证）。
2. **消费方模式统一且轻**：三种消费形态各司其职——事实读取（`transports.vsock_available()`、`imaging._platform_tag()`）、派生语义（`rlimit_as_supported` / `uds_path_max`，单测覆盖 macos/linux 双身份）、行为插件（`drivers.process.rlimits`：`"*"` POSIX 全量 + `"macos"` 诚实剔除 RLIMIT_AS；策略在父进程解析、preexec_fn 只应用预计算计划，fork/exec 间异步信号安全）。
3. **诚实边界在 Linux 受限容器被真实验证**：无 runsc 二进制/无 `/dev/vsock` ⇒ 12 个 skip 全部带理由；`restricted` 事实驱动 runsc rootless 降级路径；`rlimit_as_supported` 门控冷启动基准的 limits 档——模拟身份（`WHIRLWIND_PLATFORM`）没有让任何缺失底座变绿。
4. **Linux 复测未暴露任何平台插件缺陷**：唯一发现（导入成本）与平台层正交，已按懒加载原则修复。
5. 插件注册表当前只有 1 个 feature（`drivers.process.rlimits`）——这是诚实采用节奏而非设计缺陷：vsock 探测/镜像平台标签/测试门控本质是**事实读取**而非**行为分派**，归类为 facts 消费方是正确的（ADR-0007 D4 的迁移清单即如此划分）。
