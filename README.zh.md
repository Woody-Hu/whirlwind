# Whirlwind Agent Runtime

[English](README.md) | **中文**

harness 无感的沙箱化智能体运行时——把任意 agent harness（DeepSeek Harness / dsh、echo、自研 loop）当作黑盒进程装进受管沙箱，平台统一负责会话路由、沙箱调度、快照恢复、池化预热、事件流与能力注入。

- **语言**：Python 3.12+ / asyncio / FastAPI（依赖仅 `fastapi`、`uvicorn`、`httpx`、`pydantic`）

- **形态**：单进程 all-in-one（M1 竖切）→ Linux 集群多 substrate（M3 演进中）

- **文档**：[架构设计](agent-runtime-architecture.zh.md) · [ADR-0001 M1/M2 竖切](docs/adr/0001-agent-runtime-m1.md) · [ADR-0002 M3 沙箱底座](docs/adr/0002-m3-substrates.md)

## 核心能力

| 能力 | 说明 |
| --- | --- |
| **Harness 无感挂载** | 平台只与「沙箱 + 事件 + 能力契约」打交道。满足契约的 harness 镜像热插拔挂载（dsh 经 stdio JSON-RPC，echo 为测试基线），平台不修改其任何代码 |
| **沙箱即执行单元** | 一切 agent 执行发生在受管沙箱内。`SandboxDriver` 单接口多实现，能力位（isolation / snapshot / density / net_policy）如实上报，调度只认能力位 |
| **快照与生命周期** | 会话级 suspend / resume：DATA 快照（工作区层）+ FULL 快照（runsc/CRIU 内存+rootfs）；dsh 侧 adopt-or-create shim 实现续播 |
| **密钥不进沙箱** | 沙箱内只有 relay 占位符（`DEEPSEEK_API_KEY=whirlwind-relay`），真实凭证仅存于 Hostlet 的 SecretRelay，出网时替换 Authorization 头 |
| **Capability Seam** | Skill / Tool / Memory 平台概念编译为 Seam 契约（Definition / Provider / Consumer），经 Renderer 注入沙箱；非原生 harness 走 MCP Gateway 兜底 |
| **可回放** | durable WAL 事件日志：append 仅在 fsync 落盘后返回，崩溃恢复截断撕裂记录，组提交摊销 fsync 成本 |
| **多 substrate 通信** | Hostlet ↔ SandboxAgent 链路统一抽象：TCP / Unix Domain Socket / virtio-vsock（microVM 形态）同一套 API |

## 快速上手

```bash
# 安装（uv 或 pip）
uv sync

# 1. 构建镜像（echo 为测试基线；dsh 需本地 checkout，见 ADR D6）
whirlwind image build echo

# 2. 启动 all-in-one 运行时
whirlwind serve --port 8410 --data-dir .whirlwind

# 3. 创建 agent 并发起对话
whirlwind agent create demo --harness echo --image echo --seam fs.v1=sandbox-fs
whirlwind session create demo
whirlwind session send <sid> "hello" --stream   # SSE 流式返回事件
whirlwind session events <sid>                  # 从任意 seq 回放

# 4. 会话生命周期
whirlwind session suspend <sid>                 # 数据快照 + 释放沙箱
whirlwind session resume <sid>                  # 快照恢复续播
whirlwind cron add <agent_id> --schedule '*/5 * * * *' --input 'check in'
```

## 架构一览

```
Gateway (REST + SSE + MCP)      接入层：会话/事件流/cron/镜像管理
  └─ Control (SessionManager / Scheduler / WarmPool / Lifecycle)
       └─ Hostlet (ensure / bind / turn / pause / destroy + SecretRelay)
            └─ SandboxDriver ──→ 沙箱（gVisor / 进程组）
                 └─ SandboxAgent (EventTap / ControlAgent / ResourceInjector / LLM Relay 跳板)
                      └─ Harness (dsh / echo / ...)
```

| 模块 | 职责 |
| --- | --- |
| `core/` | 领域模型：AgentDefinition / Version、AgentSession、SessionEvent、Sandbox、Snapshot、状态机 |
| `storage/` | 五 provider 接口（Metadata / KV / EventLog / ObjectStore / EventBus）+ 内存 / WAL / 目录实现 |
| `transport/` | 传输抽象：endpoint 解析 + TCP / UDS / vsock 的 connect / serve |
| `drivers/` | `SandboxDriver` 接口 + process / runsc (gVisor) 实现 |
| `hostlet/` | 节点代理：沙箱生命周期编排 + SecretRelay（凭证出网） |
| `agent/` | SandboxAgent：沙箱内首进程（stdlib asyncio HTTP，无重框架依赖） |
| `harness/` | HarnessAdapter 接口 + echo 基线 + dsh adapter（stdio JSON-RPC） |
| `seam/` | Seam 模型与 Renderer（AgentVersion → 注入清单） |
| `imaging/` | ImageRegistry + LocalRegistry（构建即真实安装） |
| `control/` | 会话管理、调度、warm 池（CAS 认领）、生命周期 |
| `gateway/` | FastAPI：REST + SSE + MCP Gateway + CronScheduler |
| `timer/` | Kafka 式层次时间轮 + cron 表达式解析（零依赖） |
| `bus/` | 进程内事件总线（主题扇出、seq 游标） |

## 沙箱驱动矩阵

| | process (M1) | runsc / gVisor (M3) |
| --- | --- | --- |
| 隔离级别 | PROCESS（进程组 + env 白名单 + cwd 限定） | LIGHT_VM（用户态内核，独立信任域） |
| FULL 快照（内存） | ✗ | ✓ runsc checkpoint（内嵌 CRIU） |
| DATA 快照（工作区） | ✓ merkle 校验 | ✓ 同左 |
| 网络策略 | ✗ | ✓ netstack（每沙箱独立协议栈） |
| 密度 | HIGH | HIGH |
| 平台 | macOS / Linux | Linux only |

runsc driver 已在真实 gVisor（release-20260810.0, systrap 平台）上全生命周期验证：create / exec / pause / resume / checkpoint / destroy，沙箱内 `uname -r` 报告 Sentry 内核（`4.19.0-gvisor`）而非宿主内核——隔离声明实证。受限容器（无 `CAP_SYS_ADMIN`）自动降级 rootless + `--network=none`；restore 在 rootless 模式暂不支持（runsc 上游限制），全能力宿主可用。

## 传输层

Hostlet 与 SandboxAgent 之间的链路用统一的 `Endpoint` 抽象表达：

```
tcp://127.0.0.1:8000            # M1 默认（同机回环）
unix:///run/whirlwind/agent.sock  # 同机 UDS（不占端口）
http+vsock://2:8000            # microVM 形态（CID 2 = 宿主）
```

`vsock_available()` 探测 `/dev/vsock`；真实 virtio-vsock 需要 VM 平台（Firecracker / QEMU）才存在，测试按可用性跳过。

## 测试

```bash
uv run python -m pytest tests -q -m "not e2e"    # 全量（当前 139 passed, 5 skipped）
uv run python -m pytest tests/integration/test_runsc_driver.py -q   # 真实 gVisor 沙箱
WHIRLWIND_E2E=1 uv run python -m pytest tests/e2e -q  # 真实 DeepSeek API（需密钥）
```

说明：以上 `139 passed / 5 skipped` 为本测试套件在 Argus 时代运行的计数，属历史数据，原样保留。

测试策略（ADR §6）：**禁止 mock / 伪造 / 作弊**——集成测试跑真实子进程、真实文件系统、真实本地 HTTP；runsc 测试在无二进制时跳过而非 stub。基准测试含 ADR 验收线：冷启动 p50 ≤ 250ms、事件日志组提交突发 ≥ 5k/s、时间轮 10k schedules、总线扇出 20k/s。
