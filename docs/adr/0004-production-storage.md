# ADR-0004: Production storage providers — PostgreSQL MetadataStore, Redis KV/Locks

# ADR-0004：生产级存储 provider——PostgreSQL MetadataStore、Redis KV/锁

- Status: Accepted
- Date: 2026-08-19
- Related: [ADR-0001](0001-agent-runtime-m1.md) D10 (provider seams; its "SQLite persistence in M2" note is superseded by this ADR), [ADR-0002](0002-m3-substrates.md) (reserved the SQLite/PG/NATS provider slots), Architecture 10.3, [TODO](../TODO.md) P0.3–P0.6

- 状态：已接受
- 日期：2026-08-19
- 关联：[ADR-0001](0001-agent-runtime-m1.md) D10（provider 接缝；其「SQLite 持久化 M2 加」的备注由本文取代）、[ADR-0002](0002-m3-substrates.md)（预留的 SQLite/PG/NATS provider 位）、架构 10.3、[TODO](../TODO.md) P0.3–P0.6

---

## Context

## 背景

The all-in-one runtime injects in-process providers: `MemoryMetadataStore` (metadata dies with the process), `MemoryKVStore` (warm claims are process-local), `MemoryLocks` (in-process singleflight). The five `storage.providers` Protocols exist precisely so cluster deployments can inject Redis/PG/NATS (ADR-0001 D10; architecture 10.3/10.5). Direction is now production grade: land the first real database providers **behind the existing seams, with zero Protocol change**, and make backend selection a runtime configuration concern.

all-in-one 运行时注入的是进程内 provider：`MemoryMetadataStore`（元数据随进程消亡）、`MemoryKVStore`（warm 认领仅进程内）、`MemoryLocks`（进程内 singleflight）。`storage.providers` 的五个 Protocol 正是为了让集群形态注入 Redis/PG/NATS（ADR-0001 D10；架构 10.3/10.5）。当前方向是生产级：**在既有接缝之后、零 Protocol 变更**落地首批真实数据库 provider，并把后端选择变成运行时配置项。

Scope this round: **MetadataStore → PostgreSQL; KVStore + LockProvider → Redis**. EventLog→PG and EventBus→NATS stay in their reserved slots (TODO P3.2) — WAL EventLog is already durable single-node, and no multi-node consumer exists yet.

本轮范围：**MetadataStore → PostgreSQL；KVStore + LockProvider → Redis**。EventLog→PG 与 EventBus→NATS 留在预留位（TODO P3.2）——WAL EventLog 单机已 durable，且暂无多节点消费方。

## D1 Interface design — no Protocol change, lifecycle by duck typing

## D1 接口设计——零 Protocol 变更，生命周期走鸭子类型

The five Protocols are untouched; `storage/providers.py` is unchanged. New modules depend only on core models + the stdlib:

五个 Protocol 不动；`storage/providers.py` 零改动。新模块只依赖核心模型 + 标准库：

```python
# storage/postgres.py
class PostgresMetadataStore:            # satisfies MetadataStore
    def __init__(self, dsn: str, *, skills_dir: Path | None = None, ...) -> None
    async def start(self) -> None       # pool + idempotent DDL (fail fast on bad DSN)
    async def aclose(self) -> None

# storage/redis.py
class RedisKVStore:                     # satisfies KVStore
    def __init__(self, url: str, *, prefix: str = "wh:kv:") -> None
    async def start(self) -> None       # connect + ping (fail fast)
    async def aclose(self) -> None

class RedisLocks:                       # satisfies LockProvider
    def __init__(self, url: str, *, prefix: str = "wh:lock:") -> None
```

Lifecycle (`start`/`aclose`) is **not** added to the Protocols: only the composition root (`runtime.py`) constructs and starts stores; business modules keep receiving pure Protocols. `MemoryMetadataStore` / `MemoryKVStore` gain no-op `start()`/`aclose()` so the runtime can call them uniformly.

生命周期（`start`/`aclose`）**不**进入 Protocol：只有装配根（`runtime.py`）构造并启动存储；业务模块继续只依赖纯 Protocol。`MemoryMetadataStore` / `MemoryKVStore` 增加空操作 `start()`/`aclose()`，使运行时可以统一调用。

**Coupling check**: `postgres.py` imports `whirlwind.core` models + `asyncpg`; `redis.py` imports only `redis`. No module outside `runtime.py` changes. Imports of `asyncpg`/`redis` live inside the modules (and are installed as optional extras), so the base package never requires them.

**耦合检查**：`postgres.py` 仅导入 `whirlwind.core` 模型 + `asyncpg`；`redis.py` 仅导入 `redis`。除 `runtime.py` 外无任何模块改动。`asyncpg`/`redis` 的导入都在模块内部（并以可选 extra 安装），基础包永不强制依赖它们。

## D2 Data model — PostgreSQL

## D2 数据模型——PostgreSQL

**Design principle: JSONB documents for the Pydantic model (single source of truth), typed columns only where the Protocol queries.** This keeps schema evolution in lockstep with the Pydantic models and avoids a parallel relational schema to maintain. Snapshot documents round-trip via `model_dump(mode="json")` / `model_validate`.

**设计原则：Pydantic 模型（唯一事实源）存 JSONB 文档，仅在 Protocol 有查询需求处设类型化列。** 模式演化与 Pydantic 模型同步，避免维护一份平行关系模式。快照文档经 `model_dump(mode="json")` / `model_validate` 往返。

Every list-returning method observes **insertion order** (parity with the dict-backed memory store — the cron REUSE policy picks `live[-1]`), so each table carries a `seq BIGSERIAL` ordering column.

所有返回列表的方法保持**插入序**（与 dict 内存实现一致——cron REUSE 策略取 `live[-1]`），因此每表带 `seq BIGSERIAL` 排序列。

```sql
CREATE TABLE IF NOT EXISTS agents (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    seq  BIGSERIAL,
    doc  JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_versions (
    id       TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    seq      BIGSERIAL,
    doc      JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_versions_agent ON agent_versions (agent_id);
CREATE TABLE IF NOT EXISTS sessions (
    id       TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    seq      BIGSERIAL,
    doc      JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_agent ON sessions (agent_id);
CREATE TABLE IF NOT EXISTS sandboxes (
    id  TEXT PRIMARY KEY,
    seq BIGSERIAL,
    doc JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    seq      BIGSERIAL PRIMARY KEY,   -- insertion order = latest wins (parity with list append)
    subject  TEXT NOT NULL,           -- manifest.session_id if present, else snapshot.subject
    kind     TEXT NOT NULL,           -- golden | full | data
    doc      JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snapshots_subject ON snapshots (subject, seq DESC);
CREATE TABLE IF NOT EXISTS crons (
    id       TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    seq      BIGSERIAL,
    doc      JSONB NOT NULL
);
```

**Semantics parity table** (pinned by the shared contract suite; deviations from the memory store would be bugs):

**语义契约表**（由共享契约测试钉死；与内存实现的任何偏离都算缺陷）：

| Method / 方法 | Memory semantics / 内存语义 | PG mapping / PG 映射 |
|---|---|---|
| `create_agent` | `Conflict` on duplicate name | `INSERT` → unique violation on `name` → `Conflict` |
| `update_agent` | `NotFound` when id missing | `UPDATE ... RETURNING`; no row → `NotFound` |
| `create_version` / `create_session` / `upsert_sandbox` / `save_cron` | dict assign = upsert, no error | `INSERT ... ON CONFLICT (id) DO UPDATE` |
| `update_session` | dict assign = upsert, no existence check | `INSERT ... ON CONFLICT (id) DO UPDATE` (parity kept as-is) |
| `save_snapshot` | indexes full/data only, keyed by `manifest.session_id or subject` | inserts all kinds; `subject` column = same derivation; `latest_session_snapshot` filters `kind IN ('full','data') ORDER BY seq DESC LIMIT 1` (golden rows retained for audit — no observable difference through the Protocol) |
| `save_cron` (empty id) | generates `cron_<uuid12>` | same Python-side rule before insert |
| `delete_cron` | silent when missing | `DELETE` without check |
| `save_skill` / `skill_path` | real files under `skills_dir` | unchanged filesystem logic (skill archives are blobs → ObjectStore's concern; PG stores metadata only) |

Skills（skill 归档是 blob → ObjectStore 职责；PG 只存元数据）保持原文件系统逻辑不变。

`jsonb` codec: registered once per pool connection via asyncpg's `init` hook (`set_type_codec`), so documents flow as Python dicts.

`jsonb` 编解码：经 asyncpg `init` 钩子在每条连接上注册（`set_type_codec`），文档以 Python dict 直达。

## D3 Data model — Redis

## D3 数据模型——Redis

Hot state only; every key is namespaced by a configurable prefix so one Redis can host multiple deployments.

只存热状态；所有 key 经可配置前缀命名空间化，一台 Redis 可承载多部署。

| Purpose / 用途 | Key / 键 | Notes / 说明 |
|---|---|---|
| KV entries / KV 条目 | `<prefix><key>` (default `wh:kv:<key>`) | `put` uses plain `SET` (overwrites clear TTL — memory parity), `SET EX` with `ttl_s` |
| CAS / 原子比较交换 | same key | Lua script: atomic GET-compare-SET; `expected=None` matches missing key; success clears TTL (memory parity) |
| Locks / 锁 | `<prefix><name>` (default `wh:lock:<name>`) | `SET NX PX`; release is token-checked via Lua (only the holder's token deletes — stricter than the memory impl's blind release, documented strengthening) |

CAS Lua（原子 GET-比较-SET；`expected=None` 匹配缺失键；成功清除 TTL——与内存语义一致）。锁用 `SET NX PX`；释放经 Lua 校验持有者 token（仅持有者可删——比内存实现的「无条件释放」更严格，属文档化的增强）。

**Cross-process claim proof**: two `RedisKVStore` instances in one test (simulating two host processes) racing `cas(key, None, ...)` — exactly one wins. This is the property WarmPool claiming needs for the multi-process form.

**跨进程认领证明**：测试中两个 `RedisKVStore` 实例（模拟两个宿主进程）竞争 `cas(key, None, ...)`——恰好一个成功。这是 WarmPool 认领走向多进程形态所需的性质。

## D4 Runtime wiring & packaging

## D4 运行时装配与打包

```python
@dataclass
class RuntimeConfig:
    ...
    metadata_backend: str = "memory"   # "memory" | "postgres"
    postgres_dsn: str | None = None    # postgresql://user:pass@host:port/db
    kv_backend: str = "memory"         # "memory" | "redis"
    redis_url: str | None = None       # redis://[:pass@]host:port/db
```

Defaults are unchanged → the all-in-one mode is byte-for-byte backward compatible. `whirlwind serve` gains `--metadata-backend/--postgres-dsn/--kv-backend/--redis-url`. Drivers ship as optional extras: `whirlwind[postgres]` (asyncpg), `whirlwind[redis]` (redis). A backend selection without its driver installed fails at startup with an actionable message.

默认值不变 → all-in-one 模式完全向后兼容。`whirlwind serve` 新增 `--metadata-backend/--postgres-dsn/--kv-backend/--redis-url`。驱动以可选 extra 发布：`whirlwind[postgres]`（asyncpg）、`whirlwind[redis]`（redis）。选了后端但未装驱动时，启动即报可操作的错误。

## D5 Testing strategy — real services or skip, contract suite shared

## D5 测试策略——真实服务或跳过，共享契约套件

Same policy as runsc/vsock (ADR-0002): **no fakes**. Integration tests probe a real service and skip when absent (`WHIRLWIND_TEST_POSTGRES_DSN`, default `postgresql://whirlwind:whirlwind@127.0.0.1:5432/whirlwind_test`; `WHIRLWIND_TEST_REDIS_URL`, default `redis://127.0.0.1:6379/15`). No fakeredis, no sqlite-shim-for-postgres.

与 runsc/vsock 同策略（ADR-0002）：**不用替身**。集成测试探测真实服务，缺失即跳过（`WHIRLWIND_TEST_POSTGRES_DSN`，默认 `postgresql://whirlwind:whirlwind@127.0.0.1:5432/whirlwind_test`；`WHIRLWIND_TEST_REDIS_URL`，默认 `redis://127.0.0.1:6379/15`）。不用 fakeredis，不用 sqlite 冒充 postgres。

1. **Contract suite parameterized over backends**: the existing storage tests become the shared contract; memory runs always, PG/Redis run when the service is up. Parity is enforced, not assumed.
   **契约套件按后端参数化**：既有存储测试升级为共享契约；memory 恒跑，PG/Redis 在服务可用时跑。语义一致是被测出来的，不是被假设的。
2. **Restart survival (PG)**: create agent/version/session/cron → `aclose()` → new store instance on the same DSN → everything survives. The headline production property.
   **重启存活（PG）**：创建 agent/version/session/cron → `aclose()` → 同 DSN 新实例 → 全部存活。这是本轮的头号生产性质。
3. **Cross-instance CAS (Redis)**: two clients, one winner. Plus TTL expiry against real Redis expiry, not sleeps around an in-proc dict.
   **跨实例 CAS（Redis）**：两个客户端、一个赢家。TTL 过期对真实 Redis 过期测试，而非围绕进程内 dict 的 sleep。
4. **Runtime-level integration**: a `WhirlwindRuntime` assembled with PG + Redis backends serves a real session turn end-to-end.
   **运行时级集成**：以 PG + Redis 后端装配的 `WhirlwindRuntime` 端到端服务一次真实会话 turn。

## D6 Benchmarks

## D6 基准

pytest-benchmark against real localhost services, memory vs PG vs Redis: agent create+get roundtrip, session update, KV put/get, CAS. Numbers are recorded from real runs in §8 and serve as regression baselines; no fabricated thresholds — acceptance is "same order of magnitude as the first recorded baseline on the same host class" until a production host defines harder lines.

用真实本机服务跑 pytest-benchmark，memory 对 PG 对 Redis：agent create+get 往返、session update、KV put/get、CAS。数字来自真实运行并记录于 §8，作为回归基线；不设编造阈值——验收标准是「同级别主机上与首次记录基线同数量级」，直到生产主机给出更硬的线。

## D7 Destructiveness & cohesion assessment

## D7 破坏性与内聚性评估

- **Protocol layer**: zero change (`providers.py` untouched). Providers remain the only persistence contract.
- **Protocol 层**：零变更（`providers.py` 不动）。Provider 仍是唯一持久化契约。
- **Business modules**: zero change (manager/hostlet/pool/gateway untouched — they keep receiving Protocols).
- **业务模块**：零变更（manager/hostlet/pool/gateway 不动——它们继续只拿 Protocol）。
- **Memory impls**: additive no-op lifecycle methods only.
- **内存实现**：仅新增空操作生命周期方法。
- **Runtime**: gains a backend factory (composition root is exactly where backend selection belongs).
- **运行时**：新增后端工厂（装配根正是后端选择该在的地方）。
- **High cohesion / low cohesion check**: each new module owns one concern (PG metadata / Redis hot state); neither imports the other; both are replaceable leaves behind stable seams.
- **高内聚低耦合检查**：每个新模块只管一件事（PG 元数据 / Redis 热状态）；互不导入；都是稳定接缝后可替换的叶子。

## D8 Non-goals this round

## D8 本轮非目标

- EventLog → PG, EventBus → NATS (ADR-0002 reserved slots; TODO P3.2)
- Multi-node hostlet registry/discovery (TODO P3.1)
- Connection-pool tuning beyond sane defaults; read replicas; migrations framework (idempotent DDL now, versioned migrations when the first breaking change arrives)
- EventLog → PG、EventBus → NATS（ADR-0002 预留位；TODO P3.2）
- 多节点 hostlet 注册发现（TODO P3.1）
- 连接池只取合理默认，不做调优、只读副本、迁移框架（当前幂等 DDL；首个破坏性变更到来时再引入版本化迁移）
