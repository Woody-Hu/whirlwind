# Session: Delta 快照与单一底座钉选（ADR-0012，2026-08-20）

## 目标

任务 #5：把「快照 = 基础 harness 镜像 + 变更」的 delta 能力引入抽象与实现（能力位诚实，非所有底座可做），并以配置把集群钉选到单一 sandbox 实现（`sandbox.driver`）。对应 TODO P1.9 / ADR-0012 D1–D6。

## 前置

- ADR-0012（本 session 起草并随实现修订）；ADR-0009（配置三键登记）；ADR-0002（runsc 快照语义）、ADR-0005 D1（能力诚实范式）、ADR-0011（catalog 接缝）。
- 代码：`drivers/base.py`、`drivers/process.py`、`drivers/runsc.py`、`hostlet/hostlet.py`、`runtime.py`、`config.py`、`cli.py`。

## 变更清单

| 文件 | 变更 | ADR |
| --- | --- | --- |
| `drivers/base.py` | `Caps.delta_snapshots=False` 默认；`SnapshotArtifact.delta`；Protocol 增 `checkpoint(base=...)` 与 `materialize(artifact, dest)` | D1/D3 |
| `drivers/process.py` | `_scan_tree`/`_diff_trees`/`_write_delta_payload`/`_apply_delta`；delta checkpoint（overlay 载荷 + `WHIRLWIND_DELTA.json` 索引 + 端态 merkle）；`materialize` 链式重建（逐跳 merkle 校验 fail-closed）；播种改走 `materialize` | D2/D3 |
| `drivers/runsc.py` | 签名对齐；`base` 给定时在实例查找**之前**抛 `UnsupportedCapability`（不碰二进制）；全量 `materialize` | D1/D3 |
| `hostlet/hostlet.py` | `HostletConfig.snapshot_mode/snapshot_chain_max`；`suspend` 经 `_delta_base` 选 base（血缘 = session 最新 DATA 快照；压实边界 `chain_depth >= chain_max` 翻全量）；Snapshot manifest 携带 `delta/chain_depth/base` | D4 |
| `runtime.py` | `build_driver` 命名构造（process/runsc）；runsc 缺失/非 Linux 启动即 `ConfigError`（不静默回退）；`snapshot_mode=delta` 对组好 driver 的 caps 启动校验 | D4/D5 |
| `config.py` | `sandbox.driver/snapshot_mode/snapshot_chain_max` 三键（schema/env/CLI/render/校验 chain_max≥1） | D6 |
| `cli.py` | `serve --driver/--snapshot-mode/--snapshot-chain-max`（None 哨兵） | D6 |
| `deploy/whirlwind.example.toml`、`docs/adr/0009-*.md` | 三键文档化（schema 块 + env 列表） | D6/ADR-0009 |
| 测试 | `tests/unit/test_delta_snapshots.py`（10 项）、`tests/integration/test_delta_suspend.py`（2 项）、`tests/benchmark/test_delta_bench.py` | 测试策略 |

## 关键决策与发现

1. **端态内容寻址（D2 不变量 1）是好杠杆**：delta 工件的 merkle = 物化端态的 merkle，同态全量/delta 同根——"恢复正确性"免费获得等值校验，测试直接可比。
2. **整树 merkle 断言过宽的教训**：集成测试首版断言 restore 后整树 merkle == suspend 前，失败。逐文件 diff 实证：唯一差异是 `.whirlwind/manifest.json`/`runtime.json`——hostlet **有意**在播种后覆写 per-sandbox plan（新分配的 agent 端口进 `llm_relay_url`）。这是设计（plan 必须赢过快照旧值），不是恢复缺陷；测试改为排除平台 plan 目录的 `_state_merkle`（用户/harness 状态仍逐字节断言）。快照身份断言保留整树（snapshot.merkle == pre_merkle）。
3. **链深抬高两端成本（实测确认）**：delta checkpoint 需先把 base 链物化到 scratch 再 diff，检查点成本也 O(depth)（不只是恢复）：8 跳链 checkpoint 114→662ms/cycle vs 全量恒定 ~130-150ms。`chain_max` 因此一次界定 checkpoint/恢复/存储三端成本——已作为实测观察补进 ADR-0012 风险区；borg/restic 式块索引是已知优化路径，明确 out of scope。
4. **压实边界语义**：`chain_depth >= chain_max` 时 `_delta_base` 返回 None → 全量重置链；`chain_max=2` 时序列为 full→delta→delta→full（集成测试钉死）。
5. **诚实失败的层次**：loader 校验键值域（未知 driver 名）；组合根启动校验语义（runsc 缺失、delta 遇无能底座）；driver 每次调用前置校验（`base` 给定且 caps=False → `UnsupportedCapability` 在任何实例查找之前）；hostlet `_delta_base` 对直接接线（测试/嵌入方）保持 per-suspend 守卫。

## 验证证据

全部经 runner（完整输出 `.test-logs/`，控制台仅 verdict）：

- 定向：`uv run python scripts/run_tests.py tests/unit/test_delta_snapshots.py tests/integration/test_delta_suspend.py -q` → **PASS · 12 passed**（修复 `_state_merkle` 后）。
- 基准：`uv run python scripts/run_tests.py tests/benchmark/test_delta_bench.py -q -s` → PASS；稀疏负载（200×8KiB 稳定 + 4KiB append/cycle，8 跳链）实测：
  - 尺寸：全量 1604→1632 KiB/cycle 线性重存；delta 首跳全量 1604 KiB 后 **8→32 KiB/cycle**（~50x，随链长继续扩大）；
  - checkpoint：全量 ~130-150ms/cycle 恒定；delta 113.9→662.1ms/cycle（O(depth)，见发现 3）；
  - materialize：全量 96.6ms vs 8 跳链 625.2ms；链物化端态 merkle == 记录值（结构性断言，绝对数字只落日志）。
- 全量：`uv run python scripts/run_tests.py`（= `tests -q -m "not e2e"`）→ **PASS · 327 passed / 12 skipped · exit=0**（基线 314→327，+13 新测试；skip 理由与基线一致：容器无 runsc/k8s/vsock/设备）。

## 遗留与 handoff

- **快照 GC 未做**（平台今天不删快照，风险潜伏）：删除被 delta 引用的 base 必须拒绝或先压实——`base` manifest 链接即未来 GC 接缝（ADR-0012 风险区，TODO P4.1 地盘）。
- **runsc delta**：诚实 `False`；CRIU 镜像 diff 可做但未实现，有真实需求再修订。
- **块索引优化**（消除 delta checkpoint 的 base 链全量物化）：ADR-0012 风险区已记为已知路径；若真实 dsh 负载下链压实前的 checkpoint 延迟不可接受，这是第一个该做的优化。
- **跨节点快照摆放**：物化假设 base 链本地可达；多节点是 ObjectStore 接缝（ADR-0004）范围。
- 下一步建议：Loop I（依赖安装脚本 redis/postgres/runsc）或 Loop H（microsandbox Linux 可行性评估）。
