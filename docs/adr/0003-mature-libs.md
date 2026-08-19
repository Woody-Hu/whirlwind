# ADR-0003: Adopt mature libraries for cron and YAML

# ADR-0003：cron 与 YAML 采用成熟库

- Status: Accepted
- Date: 2026-08-19
- Related: [ADR-0001](0001-agent-runtime-m1.md) (D3 cron, D5 cordis rendering), [TODO](../TODO.md) P0.1–P0.2

- 状态：已接受
- 日期：2026-08-19
- 关联：[ADR-0001](0001-agent-runtime-m1.md)（D3 cron、D5 cordis 渲染）、[TODO](../TODO.md) P0.1–P0.2

---

## Context

## 背景

M1 chose zero-dependency hand-rolled implementations for two leaf utilities: the 5-field cron parser (`timer/cron.py`, ~100 lines) and a YAML "subset" serializer for `cordis.yml` (`harness/adapter.py::_yaml_dump`, ~40 lines). That was a deliberate bootstrap trade-off (ADR-0001: 4 runtime deps total). The system now evolves toward production grade; both utilities are on the critical path of user-facing behavior (cron schedules drive real turns; `cordis.yml` is read by the real dsh harness), and both are exactly the kind of surface where hand-rolled code accumulates silent edge-case defects (escaping, unicode, DST, dom/dow semantics).

M1 对两个叶子工具选择了零依赖自实现：5 字段 cron 解析器（`timer/cron.py`，约 100 行）与 `cordis.yml` 的 YAML「子集」序列化器（`harness/adapter.py::_yaml_dump`，约 40 行）。这是刻意的引导期取舍（ADR-0001：运行时依赖共 4 个）。系统现向生产级演进，两者都处于用户可感行为的必经路径（cron 调度驱动真实 turn；`cordis.yml` 由真实 dsh harness 读取），且恰恰是自实现代码容易积累隐性边界缺陷（转义、unicode、夏令时、dom/dow 语义）的面。

## D1 cron → croniter

## D1 cron → croniter

**Interface unchanged.** `CronExpr.parse(expr)`, `CronExpr.next_after(after)`, and `CronParseError` keep their exact signatures; callers (`gateway/cron.py`, `gateway/app.py`) are untouched. The class becomes a thin validating wrapper over `croniter`:

**接口不变。** `CronExpr.parse(expr)`、`CronExpr.next_after(after)`、`CronParseError` 签名保持原样；调用方（`gateway/cron.py`、`gateway/app.py`）零改动。该类变为 `croniter` 之上的薄校验包装：

- **5-field contract preserved**: the wrapper rejects non-5-field expressions before delegating (croniter itself also accepts Quartz 6-field; the system's cron surface stays 5-field).
- **5 字段契约保留**：包装层先拒绝非 5 字段表达式再委托（croniter 本身还接受 Quartz 6 字段；系统 cron 面保持 5 字段）。
- **`?` → `*` normalization stays in the wrapper**, so the documented cloud-cron dialect keeps working regardless of upstream behavior.
- **`?` → `*` 归一化留在包装层**，文档化的云 cron 方言不依赖上游行为。
- **Compatible extension (not a break)**: croniter accepts English names in the month / day-of-week fields (`MON-FRI`, `JAN`) that the old parser rejected; previously-invalid-now-valid input is a relaxation, no previously-valid input changes meaning. Strict rejection of out-of-range values (`60 * * * *`), reversed numeric ranges (`5-1` — croniter alone is lenient here, the wrapper keeps rejecting), zero steps (`*/0`), Quartz 6-field syntax, and `@` macros is preserved.
- **兼容性扩展（非破坏）**：croniter 接受旧解析器拒绝的月份 / 星期字段英文名（`MON-FRI`、`JAN`）；「先前非法、现在合法」是放宽，先前合法输入语义不变。对越界值（`60 * * * *`）、倒序数字区间（`5-1`——croniter 单独使用时是宽容的，包装层保持拒绝）、零步长（`*/0`）、Quartz 6 字段语法与 `@` 宏的严格拒绝保留。
- **Dropped internals**: `minute/hour/dom/month/dow` frozensets, `describe()`, `days_in_month()` have no callers outside the old implementation and tests; they are removed with the rewrite (the old public API surface — `parse`/`next_after`/`CronParseError` — is what callers use).
- **删除的内部项**：`minute/hour/dom/month/dow` frozenset、`describe()`、`days_in_month()` 除旧实现与测试外无调用方，随重写删除（调用方使用的旧公开 API 面是 `parse`/`next_after`/`CronParseError`）。

**Test impact**: existing vectors (every-minute, step/list, range, DOW 0/7, dom-dow OR semantics, parse errors, `?` alias) are kept verbatim as the parity contract; names-in-dow/month capability and non-5-field dialect rejection tests are added.

**测试影响**：既有用例（每分钟、步长/列表、区间、DOW 0/7、dom-dow OR 语义、解析错误、`?` 别名）原样保留作为语义契约；新增月份/星期名字能力与非 5 字段方言拒绝用例。

## D2 YAML subset → PyYAML

## D2 YAML 子集 → PyYAML

`_yaml_dump` is deleted; `DshAdapter` renders with `yaml.safe_dump(components, sort_keys=False, default_flow_style=False)`. Determinism is preserved (insertion-ordered dicts, `sort_keys=False`); the output is full standard YAML instead of a subset that raised on anything unexpected.

删除 `_yaml_dump`；`DshAdapter` 改用 `yaml.safe_dump(components, sort_keys=False, default_flow_style=False)` 渲染。确定性保留（dict 插入序 + `sort_keys=False`）；产物从「遇到意外结构即抛错」的子集变为完整标准 YAML。

**Test impact**: assertions move from textual matching to structural matching (`yaml.safe_load` → assert the parsed document), because exact emitter formatting is now the library's implementation detail. The behavioral contract (component list, ids, config values, no `resumeSessionId` leakage) is unchanged. `pyyaml` moves from the dev group to runtime dependencies.

**测试影响**：断言从文本匹配改为结构匹配（`yaml.safe_load` → 断言解析后的文档），因为精确的 emitter 格式现在是库的实现细节。行为契约（组件列表、id、config 值、不泄漏 `resumeSessionId`）不变。`pyyaml` 从 dev 依赖组移入运行时依赖。

## D3 Dependency budget

## D3 依赖预算

Runtime dependencies grow from 4 to 6: `croniter>=2.0` and `pyyaml>=6.0` (both pure-Python, widely deployed, actively maintained). No transitive burden of consequence. This is the accepted cost of not owning cron/YAML correctness.

运行时依赖从 4 个增至 6 个：`croniter>=2.0` 与 `pyyaml>=6.0`（均为纯 Python、广泛部署、持续维护）。无实质传递负担。这是「不再自担 cron/YAML 正确性」的接受成本。

## Consequences

## 结果

- `cordis.yml` byte format changes (e.g. PyYAML quoting choices); any consumer must treat it as YAML, not match it textually. The only known consumer is the dsh harness, which parses YAML.
- `cordis.yml` 字节格式会变化（如 PyYAML 的引号选择）；任何消费方必须把它当 YAML 解析而非文本匹配。已知唯一消费方 dsh harness 按 YAML 解析。
- Cron gains name/macro syntax (documented capability extension).
- cron 获得名字/宏语法（文档化的能力扩展）。
- Rollback is trivial: both wrappers are leaf modules with pinned test vectors.
- 回滚简单：两个包装都是带固定测试向量的叶子模块。
