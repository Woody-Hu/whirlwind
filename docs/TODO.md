# Whirlwind Evolution TODO / 演进待办

# Whirlwind 演进路线（活文档）

> A living roadmap maintained incrementally: every self-contained change updates this file.
> Direction: keep the harness-agnostic sandboxed runtime architecture (Architecture v0.6) unchanged and evolve it toward production grade.

> 活文档，随每个自闭环变更增量维护。方向：保持「harness 无感沙箱化运行时」大架构（架构文档 v0.6）不变，向生产级演进。

- Legend / 图例: `[ ]` todo 待办 · `[~]` in progress 进行中 · `[x]` done 已完成 · `(!)` blocked by external capability 受环境能力限制

---

## P0 — Production foundations / 生产化基础（本轮）

Replaces hand-rolled cron/YAML with mature libraries and lands the first real database providers behind the existing `storage.providers` seams (ADR-0003 / ADR-0004). Zero interface change; default backends stay in-process (no break).

以成熟库替换自实现的 cron/YAML，并在既有 `storage.providers` 接缝后落地首批真实数据库 provider（ADR-0003 / ADR-0004）。零接口变更；默认后端保持进程内实现（无破坏）。

- [x] P0.1 `timer/cron.py`: delegate to `croniter` (5-field contract, strictness and `?` alias preserved; month/dow English names accepted as a compatible extension) — ADR-0003 D1
- [x] P0.2 `harness/adapter.py`: render `cordis.yml` with `PyYAML` (`safe_dump`, deterministic key order) — ADR-0003 D2
- [x] P0.3 `storage/postgres.py`: `PostgresMetadataStore` (asyncpg pool + JSONB docs + typed key columns; semantics parity pinned by a shared contract suite; restart-survival test) — ADR-0004 D1/D2
- [x] P0.4 `storage/redis.py`: `RedisKVStore` (Lua CAS) + `RedisLocks` (token-checked release); cross-instance CAS test against real Redis — ADR-0004 D3
- [x] P0.5 Runtime backend selection: `RuntimeConfig.metadata_backend/kv_backend` + `whirlwind serve` flags; extras `whirlwind[postgres]` / `whirlwind[redis]` — ADR-0004 D4/D5
- [x] P0.6 Storage benchmarks: memory vs PostgreSQL vs Redis (real services, no fabricated numbers) — ADR-0004 D6 + measured baselines
- [x] P0.7 Unified configuration: one TOML (`whirlwind.toml`) + layered injection (code defaults < file < `WHIRLWIND_*` env < CLI), schema-validated loader (`config.py`), `whirlwind config show` introspection, k3s manifest switched to ConfigMap-mounted TOML; rule codified as AGENTS.md §3.5 — ADR-0009

## P1 — Edge hardening / 边缘加固（本轮）

The gateway currently has no authn/authz (Architecture 3.1 G1 promises tenant auth, rate limiting, idempotency keys) and no resource gates (Architecture 6.2 promises tenant quotas).

网关当前无认证鉴权（架构 3.1 G1 承诺租户认证、限流、幂等键校验），无资源闸门（架构 6.2 承诺租户配额）。

- [ ] P1.1 Gateway API-key authn (constant-time compare; `WHIRLWIND_API_KEY`-style config; CLI sends the header) — prerequisite for any network-facing deployment; deferred pending the tenant dimension
- [x] P1.2 Quotas & backpressure: live-session admission cap (`--max-live-sessions`, store-derived count, 429 `whirlwind/quota-exceeded`) — ADR-0005 D2; measured 0.20ms per create at 1k live sessions
- [x] P1.3 Sandbox resource limits: `Resources` on `SandboxSpec` (mem/cpu/pids) enforced via process rlimits and runsc OCI config — ADR-0005 D1; cold start p50 245ms with limits applied (250ms line holds)
- [x] P1.4 Idempotency keys on mutating gateway routes (`Idempotency-Key` header, KVStore-backed replay/claim, 409 in-flight, 422 body mismatch) — ADR-0005 D3
- [x] P1.5 k3s deployment form: `deploy/k3s/` (image build, manifest with probes/resources/PVC/NodePort, rerunnable smoke script); validated on a real k3s v1.36.3 control plane (`--disable-agent` — the sandbox container cannot run kubelet; pods verified through scheduling, PVC binding, NodePort) — ADR-0005 D4
- [ ] P1.6 (next, with tenancy) API-key rate limiting per tenant; tenant-scoped quotas; cross-process quota hardening (Redis counter)

## P2 — Observability / 可观测性

Today: plain text logs only. No metrics, no tracing, no structured logging.

现状：纯文本日志；无 metrics、无 tracing、无结构化日志。

- [ ] P2.1 `/metrics` endpoint (prometheus text format, stdlib-only renderer) with first-class counters: sessions by status, turn latency, WAL append rate, sandbox population
- [ ] P2.2 Structured (JSON) logging with request/session correlation ids
- [ ] P2.3 OTLP tracing (Architecture 6.1 M3 deliverable; deferred until a collector exists in the target env)

## P3 — Cluster form / 集群形态（M3 余量）

ADR-0002 reserved the provider slots; P0 lands PostgreSQL/Redis single-node first. Multi-node needs the registry/discovery layer (Architecture 3.2 C6).

ADR-0002 已预留 provider 位；P0 先落地单节点 PostgreSQL/Redis。多节点需要注册发现层（架构 3.2 C6）。

- [ ] P3.1 Hostlet registry/discovery on Redis leases + list-and-watch (Architecture 3.2 C6)
- [ ] P3.2 WALEventLog → PostgreSQL provider (archived, queryable) and EventBus → NATS provider (ADR-0002 follow-up slots)
- [ ] P3.3 Snapshot locality routing across hostlets (Architecture M3)
- [ ] P3.4 Firecracker microVM driver (third substrate, same `SandboxDriver` interface — ADR-0002 follow-up)
- [~] P3.5 Microsandbox driver for test/dev ergonomics — `Isolation.LIGHT_VM` (libkrun/krunkit, a real VM substrate that runs natively on macOS via Virtualization.framework) riding the existing `SandboxDriver` interface. `process` is too weak to gate VM-grade lifecycle; `runsc` is Linux-only; a libkrun substrate gives a locally-testable VM path on M-series mac AND a dev analog of the Firecracker `MICRO_VM` substrate. Platform facts verified: runsc installs inside colima's Linux Docker VM; colima `--kubernetes` gives macOS k3s; colima `--vm-type krunkit` is a native Apple Silicon VM backend; dsh is a public MIT repo (see MEMORY.env). **Design locked (ADR-0006 Accepted); implementation deferred to a real-host session — document landing done, code pending.**
  - [x] M0-doc ADR-0006 accepted + impl phase planned (spike / driver / wiring / tests / docs)
  - [x] Doc sync: ADR-0006 / TODO / MEMORY / AGENTS (index + §1.3 + §7 env-detection) updated
  - [ ] M0 spike (real mac): probe krunkit availability + CLI surface (create/exec/pause/snapshot)
  - [ ] M1 driver skeleton: `drivers/microsandbox.py` (`SandboxDriver`, `Isolation.LIGHT_VM`)
  - [ ] M2 wiring: `--sandbox-driver microsandbox`, zero `control`/`hostlet` change
  - [ ] M3 tests: `test_microsandbox_driver.py`, gated on backend availability (skip + reason)
  - [ ] M4 memory: flip ADR to fully delivered, refresh TODO/MEMORY/AGENTS

## P4 — Scale-out operations / 规模化运营（M4）

- [ ] P4.1 Snapshot GC: retention policies, reference counting, storage layering downgrade (Architecture 3.2 C5, 6.3)
- [ ] P4.2 Agent teams: hub/direct/router topologies, supervisor, message routing (Architecture 5.1, 8.2)
- [ ] P4.3 Cron-calendar prewarm and capacity elasticity (Architecture M4)
- [ ] P4.4 MCP gateway completeness: ping, notifications/initialized, resources subsystem, per-connection session ids (current: M1 subset — initialize/tools only)

## Done / 已完成

- 2026-08 M1: single-process vertical slice, process driver, echo/dsh adapters, Seam renderer, gateway REST+SSE+MCP, CLI (ADR-0001)
- 2026-08 M2: full session lifecycle (suspend/resume), warm pool CAS claiming, idle/max-duration lifecycle, cron scheduler on the hierarchical timing wheel (ADR-0001)
- 2026-08 M3 (partial): runsc driver on real gVisor, TCP/UDS/vsock transports, durable WAL EventLog with group-commit fsync + crash recovery (ADR-0002)
- 2026-08 P0: mature-library foundations + PostgreSQL/Redis storage providers (ADR-0003, ADR-0004)
- 2026-08 P1 (auth excluded): sandbox resource limits, live-session quota, idempotency keys, k3s deployment form with measured gate costs (ADR-0005)
- 2026-08 EngEx: platform abstraction — one `PlatformFacts` object + `@platform_impl` behaviour plugins + `WHIRLWIND_PLATFORM` identity simulation (ADR-0007); test runner with full output to `.test-logs/` and terse console verdict (ADR-0008); AGENTS.md gains §3.3 platform rule and §4.5 test-logging spec
- 2026-08 Docs: top-level bilingual docs split into separate EN / zh-CN files with language-switch links (README + architecture doc); convention codified as AGENTS.md §5.5 (see session-log 2026-08-20-doc-split.md)
- 2026-08 P0.7: unified configuration — single `whirlwind.toml` with layered injection (file < env < CLI), schema hard-validation, `config show` introspection; hardcoded operator defaults eliminated; AGENTS.md §3.5 rule (ADR-0009)
