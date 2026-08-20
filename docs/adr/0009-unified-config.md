# ADR-0009: Unified configuration — one TOML file, layered injection (file < env < CLI)

# ADR-0009：统一配置——单一 TOML 文件，分层注入（文件 < 环境变量 < CLI）

- Status: Accepted
- Date: 2026-08-20
- Related: [ADR-0004](0004-production-storage.md) D4（后端选择是 RuntimeConfig 关注点）、[ADR-0005](0005-edge-hardening.md) D1/D2（资源上限 / 会话配额配置项）、[ADR-0007](0007-platform-abstraction.md)（`WHIRLWIND_PLATFORM` 保持 env-only）、[AGENTS.md](../../AGENTS.md) §3.5（本 ADR 落为配置规范）

- 状态：已接受
- 日期：2026-08-20
- 关联：[ADR-0004](0004-production-storage.md) D4（后端选择是 RuntimeConfig 关注点）、[ADR-0005](0005-edge-hardening.md) D1/D2（资源上限 / 会话配额配置项）、[ADR-0007](0007-platform-abstraction.md)（`WHIRLWIND_PLATFORM` 保持 env-only）、[AGENTS.md](../../AGENTS.md) §3.5（本 ADR 落为配置规范）

---

## Context

## 背景

Operator-tunable settings currently live in three disconnected places, with defaults **duplicated** between them:

Operator 可调的配置目前散在三处，且默认值在多处**重复**：

1. `cli.py` argparse defaults (`--api-key-env DEEPSEEK_API_KEY`, `--llm-upstream https://api.deepseek.com`, …) — duplicated in
2. `RuntimeConfig` / `HostletConfig` dataclass defaults (`runtime.py`, `hostlet.py`), plus
3. ad-hoc env vars (`WHIRLWIND_URL`, `WHIRLWIND_DSH_REPO`) and the k3s manifest passing CLI args via ConfigMap→env indirection.

1. `cli.py` 的 argparse 默认值（`--api-key-env DEEPSEEK_API_KEY`、`--llm-upstream https://api.deepseek.com`……）——与
2. `RuntimeConfig` / `HostletConfig` 的 dataclass 默认值（`runtime.py`、`hostlet.py`）重复；另有
3. 临时环境变量（`WHIRLWIND_URL`、`WHIRLWIND_DSH_REPO`）与 k3s manifest 经 ConfigMap→env 间接传 CLI 参数。

**English.** The requirement (user, 2026-08-20): the system must have no forced hardcoded settings; every operator-tunable value flows through one configuration file via a single loading mechanism, and this becomes an AGENTS.md rule.

**中文.** 要求（用户，2026-08-20）：系统不得有强制写死的配置；所有可调值统一经一个配置文件、一个加载机制注入，并落为 AGENTS.md 规则。

## Decision — D1: precedence — code defaults < TOML file < `WHIRLWIND_*` env < CLI flags

## 决策——D1：优先级——代码默认值 < TOML 文件 < `WHIRLWIND_*` 环境变量 < CLI 参数

**English.** Standard 12-factor layering. Code defaults are the single fallback (defined once, in the loader); the TOML file is the deployable unit of configuration; env vars serve containers/CI where editing files is awkward; explicit CLI flags win for interactive override. Each layer only needs to specify what differs from the layer below.

**中文.** 标准 12-factor 分层。代码默认值是唯一兜底（只在 loader 里定义一次）；TOML 文件是可部署的配置单元；环境变量服务容器/CI 等不便改文件的场景；显式 CLI 参数用于交互式覆盖，优先级最高。每层只需声明与下一层不同的部分。

## Decision — D2: TOML, parsed by stdlib `tomllib`

## 决策——D2：TOML，标准库 `tomllib` 解析

**English.** Python 3.12 is the project floor, so `tomllib` is guaranteed — **zero new dependency** (§3.3 minimal-deps rule). Config files are hand-written, read-only, so the absence of a stdlib TOML *writer* is irrelevant. YAML was rejected: it is already a dependency (ADR-0003) but its type ambiguity (Norway problem, implicit casting) is a foot-gun for ops files; JSON has no comments.

**中文.** 项目下限是 Python 3.12，`tomllib` 必然可用——**零新增依赖**（§3.3 最小依赖原则）。配置文件由人工编写、只读取，标准库没有 TOML *写入器* 无关紧要。弃用 YAML：它虽已是依赖（ADR-0003），但其类型二义性（Norway 问题、隐式转换）对运维文件是坑；JSON 不支持注释。

## Decision — D3: file discovery — `--config PATH` > `WHIRLWIND_CONFIG` > `./whirlwind.toml` (if present) > none

## 决策——D3：文件发现——`--config PATH` > `WHIRLWIND_CONFIG` > `./whirlwind.toml`（存在时）> 无

**English.** Explicit beats implicit; the conventional `./whirlwind.toml` is a convenience for repo-local runs (dev/CI), not a multi-path magic search. Absent all three, pure defaults apply — the runtime still boots with zero configuration (M1 all-in-one ergonomics unchanged).

**中文.** 显式优先；约定路径 `./whirlwind.toml` 是仓库本地运行的便利项，不是多路径魔法搜索。三者皆无时用纯默认值——运行时仍然零配置可启动（M1 all-in-one 体验不变）。

## Decision — D4: schema — sections mirror the composition root

## 决策——D4：schema——分区镜像组合根

```toml
# whirlwind.toml — every key optional; shown with defaults
[server]
host = "127.0.0.1"          # uvicorn bind
port = 8410

[runtime]
data_dir = ".whirlwind"     # resolved against cwd at load time
repo_root = null            # where image builds install whirlwind from
api_key_env = "DEEPSEEK_API_KEY"   # NAME of the env var holding the key (not the key)
llm_upstream = "https://api.deepseek.com"
wheel_tick_ms = 20

[runtime.warm_pool]         # agent_version_id -> min_warm
# "ver_xxx" = 2

[storage]
metadata_backend = "memory"   # "memory" | "postgres"
postgres_dsn = null           # required when metadata_backend = "postgres"
kv_backend = "memory"         # "memory" | "redis"
redis_url = null              # required when kv_backend = "redis"

[sandbox]
max_live_sessions = null      # admission cap; null = uncapped
driver = "process"            # substrate pinning: "process" | "runsc" (ADR-0012 D5)
snapshot_mode = "full"        # "full" | "delta" — delta validated against driver caps at boot (ADR-0012 D4/D6)
snapshot_chain_max = 16       # delta chain compaction bound (≥1; full snapshot resets the chain)

[sandbox.resources]           # per-sandbox ceilings (ADR-0005 D1)
# mem_limit_mb = 512
# cpu_seconds = 3600
# pids_max = 256

[logging]                     # structured logging (ADR-0013 D2)
level = "INFO"                # DEBUG | INFO | WARNING | ERROR | CRITICAL
format = "json"               # "json" | "text" — single-line JSON vs plain text
environment = "unknown"       # frozen tag injected into every JSON record
```

**English.** The loader (`src/whirlwind/config.py`) produces a frozen `Settings(server=…, runtime=RuntimeConfig, config_path=…, logging=…)`; `RuntimeConfig` remains THE composition-root dataclass with **no signature change** — existing tests and embedders (k3s, e2e) keep constructing it directly. Unknown sections/keys are a hard error (typos must fail loudly, not silently no-op). Type validation happens at load; failures raise `ConfigError` (`whirlwind/config`) per the error-code contract.

**中文.** loader（`src/whirlwind/config.py`）产出冻结的 `Settings(server=…, runtime=RuntimeConfig, config_path=…, logging=…)`；`RuntimeConfig` 仍是组合根 dataclass，**签名不变**——既有测试与嵌入方（k3s、e2e）继续直接构造。未知分区/键是硬错误（拼写错误必须响亮失败，而非静默无效）。加载时做类型校验，失败抛 `ConfigError`（`whirlwind/config`），遵守错误码契约。

## Decision — D5: env overrides — flat `WHIRLWIND_*` names

## 决策——D5：环境变量覆盖——扁平 `WHIRLWIND_*` 命名

`WHIRLWIND_HOST`, `WHIRLWIND_PORT`, `WHIRLWIND_DATA_DIR`, `WHIRLWIND_REPO_ROOT`, `WHIRLWIND_API_KEY_ENV`, `WHIRLWIND_LLM_UPSTREAM`, `WHIRLWIND_WHEEL_TICK_MS`, `WHIRLWIND_METADATA_BACKEND`, `WHIRLWIND_POSTGRES_DSN`, `WHIRLWIND_KV_BACKEND`, `WHIRLWIND_REDIS_URL`, `WHIRLWIND_MAX_LIVE_SESSIONS`, `WHIRLWIND_SANDBOX_DRIVER`, `WHIRLWIND_SNAPSHOT_MODE`, `WHIRLWIND_SNAPSHOT_CHAIN_MAX`.

**English.** Compound values (`warm_pool`, `sandbox.resources`) are file/CLI-only — encoding dicts in env vars is hostile. Secrets themselves are never config values: only the *name* of the env var (`api_key_env`) is configured; the value stays a real environment variable read by the Hostlet (credentials never enter files — §6.2 of the architecture doc).

**中文.** 复合值（`warm_pool`、`sandbox.resources`）只走文件/CLI——在环境变量里编码字典是反人类。密钥本身永远不是配置值：配置的只是环境变量的**名字**（`api_key_env`）；值仍是真实环境变量，由 Hostlet 读取（凭证不进文件——架构文档 §6.2）。

## Decision — D6: CLI flags become pure overrides

## 决策——D6：CLI 参数变为纯覆盖

**English.** argparse defaults change from hardcoded values to `None` sentinels; only explicitly-given flags override. This removes the default duplication between `cli.py` and `runtime.py` — defaults now live exactly once (D1's code-default layer). `serve` gains `--config PATH`; all existing flags keep their names.

**中文.** argparse 默认值从写死值改为 `None` 哨兵；只有显式给出的参数才覆盖。这消除了 `cli.py` 与 `runtime.py` 的默认值重复——默认值现在只存在一处（D1 的代码默认层）。`serve` 新增 `--config PATH`；既有参数名全部保留。

## Decision — D7: `whirlwind config show` — effective-config introspection

## 决策——D7：`whirlwind config show`——生效配置自省

**English.** A new CLI subcommand prints the fully resolved settings (which file was loaded, the effective TOML) so operators can answer "what is actually running?" without replaying the precedence chain in their head. Output is machine-parseable TOML with a one-line provenance header.

**中文.** 新 CLI 子命令打印完全解析后的配置（加载了哪个文件、生效的 TOML），运维无需在脑内重放优先级链即可回答"实际在跑什么"。输出为机器可解析的 TOML，带一行来源头。

## Decision — D8: what deliberately stays OUT of the config file

## 决策——D8：刻意不进配置文件的东西

**English.** (a) Secret *values* — env-only (D5). (b) Sandbox-internal injected vars (`WHIRLWIND_SANDBOX_ID`, `WHIRLWIND_MANIFEST`, `WHIRLWIND_RUNTIME`, `WHIRLWIND_AGENT_LISTEN`…) — runtime injection by the Hostlet, not operator config. (c) `WHIRLWIND_PLATFORM` — dev/test identity simulation, deliberately env-only (ADR-0007 D2: production never sets it). (d) `WHIRLWIND_URL` — client-side CLI concern (which gateway to talk to), not server config. (e) Harness test knobs (`ECHO_LLM_URL`…) — test fixtures. (f) `WHIRLWIND_DSH_REPO` — build-time override for image builds; a candidate for future ingestion, kept env-only until imaging config grows.

**中文.** (a) 密钥**值**——仅环境变量（D5）。(b) 沙箱内部注入变量（`WHIRLWIND_SANDBOX_ID`、`WHIRLWIND_MANIFEST`、`WHIRLWIND_RUNTIME`、`WHIRLWIND_AGENT_LISTEN`……）——Hostlet 的运行时注入，非运维配置。(c) `WHIRLWIND_PLATFORM`——开发/测试身份模拟，刻意仅环境变量（ADR-0007 D2：生产环境永不设置）。(d) `WHIRLWIND_URL`——客户端 CLI 关注点（连哪个 gateway），非服务端配置。(e) harness 测试旋钮（`ECHO_LLM_URL`……）——测试夹具。(f) `WHIRLWIND_DSH_REPO`——镜像构建的构建期覆盖，候选未来收编，在 imaging 配置长大之前保持 env-only。

## Decision — D9: the rule lands in AGENTS.md §3.5

## 决策——D9：规范落为 AGENTS.md §3.5

**English.** "No hardcoded operator config": every new operator-tunable value must enter the schema (section + env override + CLI flag as appropriate), flow through `load_settings`, and be documented in this ADR's schema block. A `ValueError("...")` with a literal default buried in a business module is the anti-pattern this ADR bans.

**中文.** 「禁止写死运维配置」：每个新增可调值必须进入 schema（分区 + 环境变量 + 视情况 CLI 参数），经 `load_settings` 流转，并在本 ADR 的 schema 块中登记。业务模块里埋一个字面量默认值的 `ValueError("...")` 是本 ADR 禁止的反模式。

## Test strategy

## 测试策略

`tests/unit/test_config.py` (pure logic, no mocks):

- defaults apply with no file/env/cli;
- TOML overrides defaults; env overrides TOML; CLI overrides env (per-field precedence ladder);
- discovery order: explicit `--config` > `WHIRLWIND_CONFIG` > conventional `./whirlwind.toml` > none;
- unknown key / unknown section / wrong type / bad TOML → `ConfigError` with stable code;
- compound values (`warm_pool`, `sandbox.resources`) round-trip into `RuntimeConfig`;
- `data_dir` resolves to an absolute path.

An integration test drives `whirlwind config show` as a real subprocess (no mock) and asserts the effective TOML reflects file + env overrides.

`tests/unit/test_config.py`（纯逻辑，无 mock）：无文件/env/cli 时默认值生效；TOML 覆盖默认、env 覆盖 TOML、CLI 覆盖 env（逐字段优先级阶梯）；发现顺序 `--config` > `WHIRLWIND_CONFIG` > `./whirlwind.toml` > 无；未知键/分区、类型错误、坏 TOML → 稳定错误码的 `ConfigError`；复合值（`warm_pool`、`sandbox.resources`）正确进入 `RuntimeConfig`；`data_dir` 解析为绝对路径。另有一条集成测试以真实子进程驱动 `whirlwind config show`（无 mock），断言生效 TOML 反映文件 + env 覆盖。

## Conflict check

## 冲突检查

- Architecture v0.6 §10.3: storage selection via provider interfaces — unchanged; the config mechanism only *feeds* `RuntimeConfig` (ADR-0004 D4's composition-root selection is untouched).
- ADR-0007 D2: `WHIRLWIND_PLATFORM` stays env-only — explicitly preserved (D8c).
- k3s manifest (ADR-0005 D4): switches from CLI-arg plumbing to a ConfigMap-mounted `whirlwind.toml` + `--config`; the smoke script is config-agnostic (REST-only), pod-level revalidation pending a real cluster (headless container limitation, §4.2 honest boundary).

- 架构 v0.6 §10.3：经 provider 接口选择存储——不变；配置机制只负责**喂** `RuntimeConfig`（ADR-0004 D4 的组合根选择不受影响）。
- ADR-0007 D2：`WHIRLWIND_PLATFORM` 保持 env-only——显式保留（D8c）。
- k3s manifest（ADR-0005 D4）：从 CLI 参数管道切换为 ConfigMap 挂载 `whirlwind.toml` + `--config`；smoke 脚本与配置无关（纯 REST），pod 级复测待真机集群（无头容器限制，§4.2 诚实边界）。

## Risks / open points

## 风险与开放点

- `WHIRLWIND_URL` (client) and `WHIRLWIND_DSH_REPO` (build-time) remain env-only; if they cause confusion they can be ingested later without breaking the schema (additive change).
- The loader does not support includes/overrides between multiple TOML files — one file, one truth; layering is the override mechanism.
- `whirlwind config show` prints the effective config including `postgres_dsn`/`redis_url` (they may contain credentials); a `--redact` flag is a future nicety, not landed here.

- `WHIRLWIND_URL`（客户端）与 `WHIRLWIND_DSH_REPO`（构建期）保持 env-only；若造成困扰可后续收编，schema 是加法不破坏。
- loader 不支持多 TOML 文件之间的 include/覆盖——一个文件一个事实源；分层就是覆盖机制。
- `whirlwind config show` 打印的生效配置含 `postgres_dsn`/`redis_url`（可能带凭证）；`--redact` 旗标是未来的锦上添花，本轮不做。
