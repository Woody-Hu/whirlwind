# ADR-0002: M3 沙箱底座——runsc driver、vsock 传输、durable EventLog

- 状态：已接受
- 日期：2026-08-19
- 关联：[ADR-0001](0001-agent-runtime-m1.md)（D2 预留「runsc driver 按同一接口在 M3 加入，调度器零改动」）

---

## 背景

M1/M2 以 process driver 完成竖切（单节点、macOS/Linux 双平台）。M3 引入三个生产化构件：
真实的内核级隔离底座（gVisor）、microVM 形态的传输通道（virtio-vsock）、崩溃安全的事件持久化（WAL）。
三者都已对真实基础设施验证，本文记录验证中确定的工程决策。

## D1 runsc driver：CLI 编排而非 API 绑定

对齐架构 8.2「二进制编排用 subprocess 封装」。driver 是 `runsc` CLI 的薄编排层：
OCI bundle 渲染（rootfs = 镜像 bundle bind-mount 到 `/`，workspace bind-mount 到 `/workspace`）+
子命令映射（run/exec/pause/resume/checkpoint/restore/kill/delete）。

**对真实 gVisor（release-20260810.0, systrap）端到端验证，修正了四个 stub 级测试无法暴露的缺陷：**

1. `--network=netstack` 不是合法 flag 值——runsc 的每沙箱 netstack 取值是 `sandbox`。
   原代码在真实环境第一跳就报错。
2. **detach 管道死锁**：`runsc run --detach` 的父进程立即退出，但 sandbox / gofer 子进程
   继承 stdout/stderr 管道 fd，`communicate()` 永远等不到 EOF（实测挂满 60s 超时）。
   修正：输出走临时文件，等待父进程退出码。
3. exec 的 `--cwd` 误传宿主工作区路径；沙箱内 cwd 必须是 rootfs 相对挂载点（`/workspace`）。
4. exec 注入的 `--` 分隔符被当前 runsc 版本当作可执行名。

**能力声明实证**：沙箱内 `uname -r` 返回 `4.19.0-gvisor`（Sentry 用户态内核）而非宿主
`6.18.5`——`Isolation.LIGHT_VM` 是真实隔离边界，非声明式。FULL 快照经内嵌 CRIU 落盘
（`checkpoint.img` / `pages.img`，busybox 沙箱约 270KB）。

**受限环境适配**（driver 新增 `platform / rootless / ignore_cgroups` 参数）：
无 `CAP_SYS_ADMIN` 的容器（rootless containerd/docker）必须 rootless 运行、跳过 cgroups、
放弃每沙箱 netstack（`--network=none` 或 `host`）。**runsc 上游限制**：restore 不支持
rootless 模式——FULL 快照恢复测试按环境能力跳过，全能力宿主可用。

## D2 传输层：三 substrate 同一抽象

`transport/` 提供 `Endpoint{scheme, address, port}` 与 `connect() / serve()`，TCP / UDS / vsock
统一到 asyncio streams。SandboxAgent 与 Hostlet 的既有 HTTP 协议不变——`http+unix://`、
`http+vsock://` URL scheme 在 `parse_url` 处解出（endpoint, base_path）二元组，跳板语义对上层透明。

vsock 用原生 `AF_VSOCK` socket 手工适配 asyncio（无第三方依赖）。`vsock_available()` 探测
`/dev/vsock`；该设备只在 VM 平台（Firecracker / QEMU）下存在，容器环境无内核模块加载能力，
测试按可用性跳过——这是硬性内核限制而非未实现。

URL 形态约定：`vsock://PORT` 服务端绑定宿主监听；客户端 `http+vsock://{CID}:{PORT}`，
`VMADDR_CID_HOST=2` 指向宿主。

## D3 durable EventLog：WAL + 组提交 fsync

M1 的 `JSONLEventLog` 只做行缓冲 flush，进程崩溃即丢尾部数据——与架构 10.3
「模型可见的每一个输入都可从事件日志重建」的可回放承诺冲突。M3 以 `WALEventLog` 替换：

- **持久性契约**：`append()` 仅在记录 fsync 落盘后返回。崩溃/断电前的写入可丢弃，
  但绝不损坏日志、绝不重排已提交记录。
- **组提交**：并发 append 在同一提交批次共享一次 fsync。实测顺序追加 ~1.4k/s
  （单条 fsync 延迟地板），10k 并发突发 38k/s（~27x 摊销）——ADR ≥5k/s 验收线移至突发路径，
  顺序路径设 1k/s 持久化地板线。
- **崩溃恢复**：reopen 时扫描 WAL、截断撕裂的尾部记录（含「完整 JSON 但换行未落盘」——
  该记录同样未达持久性契约，必须丢弃）；`read()` 兜底跳过撕裂行。
- **错误上抛**：fsync 失败经 future 传回 append 调用方，committer 存活继续服务后续批次。

文件布局不变（每会话一个 JSONL，目录型），`EventLog` 协议零改动，runtime 装配处单行替换。

## 测试与验证记录

- 真实 gVisor 全生命周期（busybox rootfs）：create / exec（cwd 验证 `/workspace`）/
  pause / resume / DATA 快照（merkle）/ FULL 快照（CRIU 镜像落盘）/ destroy，
  另有内核隔离证明用例。
- 传输层 TCP / UDS 回环测试真跑；vsock 按设备存在性跳过。
- WAL：组提交 fsync 计数（100 并发 append ≤ 5 次 fsync）、落盘即见、撕裂尾截断、
  无换行记录丢弃、跨会话 seq 独立、fsync 失败上抛。
- 全量回归：139 passed / 5 skipped（skip 均为环境能力限制：vsock 设备、rootless restore）。

## 后续

- firecracker microVM driver（架构 4.4 第三底座）接入同一 `SandboxDriver` 接口。
- 集群形态：WAL EventLog 的 SQLite/PG provider 位、EventBus 的 NATS provider 位已预留。
