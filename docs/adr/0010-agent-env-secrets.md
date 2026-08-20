# ADR-0010: Agent-defined environment secrets — reference/value separation, encrypted at rest, injected at provision

# ADR-0010：Agent 定义的环境密钥——引用/值分离、静态加密、供给期注入

- Status: Accepted
- Date: 2026-08-20
- Related: [ADR-0001](0001-agent-runtime-m1.md) §6（SecretRelay：平台凭证永不进沙箱）、[ADR-0003](0003-mature-libs.md)（成熟库优先的先例）、[ADR-0004](0004-production-storage.md)（存储 provider 模式）、[ADR-0009](0009-unified-config.md) D5/D8（密钥值不进配置文件）、[AGENTS.md](../../AGENTS.md) §3.3（最小依赖 / 成熟库原则）

- 状态：已接受
- 日期：2026-08-20
- 关联：[ADR-0001](0001-agent-runtime-m1.md) §6（SecretRelay：平台凭证永不进沙箱）、[ADR-0003](0003-mature-libs.md)（成熟库优先的先例）、[ADR-0004](0004-production-storage.md)（存储 provider 模式）、[ADR-0009](0009-unified-config.md) D5/D8（密钥值不进配置文件）、[AGENTS.md](../../AGENTS.md) §3.3（最小依赖 / 成熟库原则）

---

## Context

## 背景

**English.** Agent definitions need user-provided environment variables (a GitHub token for the agent's tools, an external API key the harness code must call). The requirement (user, 2026-08-20): values are sensitive — store them as such — and inject them into the sandbox when it boots. Today `AgentVersion` has no env surface at all, and the only credential path is the platform-owned SecretRelay (the DeepSeek key stays host-side; the sandbox sees the `whirlwind-relay` placeholder).

Research (2026-08-20, web): the recurring best practices across agent runtimes and orchestration systems are —
1. **Reference/value separation** (Kubernetes Secret↔Deployment, GitHub Actions secrets, the "vault pattern" in agent-runtime writeups): the definition carries *references or names*, never values; the execution layer resolves them below the agent.
2. **Encryption at rest** with a master key from the environment (never in config files), per-record random nonces, authenticated encryption.
3. **Write-only from the client's perspective**: APIs never return secret values; redaction everywhere values could surface (responses, logs, serialized definitions).
4. In-sandbox env is a *different trust zone* from the LLM context window: values must reach harness process env (that is their purpose), so the platform concern is at-rest protection + API redaction, not hiding them from the sandbox.

The hard constraint specific to this architecture: `AgentVersion` is dumped by every GET endpoint and serialized into the metadata store — putting values inside it would leak through every existing surface. 

**中文.** Agent 定义需要用户提供的环境变量（agent 工具用的 GitHub token、harness 代码要调用的外部 API key）。要求（用户，2026-08-20）：值是敏感的——按敏感信息存储——并在沙箱启动时注入。今天 `AgentVersion` 完全没有 env 面；唯一的凭证通路是平台自有的 SecretRelay（DeepSeek key 留在宿主侧，沙箱只见 `whirlwind-relay` 占位符）。

调研（2026-08-20，网络）：agent 运行时与编排系统反复出现的最佳实践——
1. **引用/值分离**（Kubernetes Secret↔Deployment、GitHub Actions secrets、agent 运行时文献中的 vault 模式）：定义里携带*引用或名字*，永不携带值；执行层在 agent 之下解析。
2. **静态加密**：主密钥来自环境（永不进配置文件），每条记录随机 nonce，认证加密。
3. **对客户端只写**：API 永不返回密钥值；值可能出现的每个面（响应、日志、序列化的定义）都脱敏。
4. 沙箱内 env 与 LLM 上下文窗口是*不同的信任域*：值必须到达 harness 进程 env（这正是它们的目的），所以平台关注点是静态保护 + API 脱敏，而非对沙箱隐藏。

本架构特有的硬约束：`AgentVersion` 会被每个 GET 端点 dump、序列化进元数据存储——把值放进它等于从每个既有表面泄漏。

## Decision — D1: names in `AgentVersion`, values in a separate encrypted store

## 决策——D1：名字进 `AgentVersion`，值进独立的加密存储

**English.** `AgentVersion` gains `env_secrets: list[str]` — the declared variable **names only** (list order is irrelevant; a plain name list is enough). Values live in a `SecretStore` keyed by `version_id`, stored as **encrypted envelopes**. Consequences: every existing `model_dump()` surface (REST responses, JSONB persistence, logs) can only ever leak names; version immutability is preserved (sealing happens once at version creation); the store's on-disk artifacts carry ciphertext only.

**中文.** `AgentVersion` 新增 `env_secrets: list[str]`——只声明变量**名字**。值放在以 `version_id` 为键的 `SecretStore`，存的是**加密信封**。推论：既有每个 `model_dump()` 表面（REST 响应、JSONB 持久化、日志）最多泄漏名字；版本不可变性保持（封存在版本创建时一次性完成）；存储的落盘产物只含密文。

## Decision — D2: `pynacl` SecretBox (libsodium XSalsa20-Poly1305) — not a hand-rolled cipher

## 决策——D2：`pynacl` SecretBox（libsodium XSalsa20-Poly1305）——不手搓密码学

**English.** New base dependency `pynacl`. Rationale: ADR-0003's precedent is "mature libraries over hand-rolled" *even for a cron parser*; cryptography is the strongest form of that rule, and the stdlib offers no AEAD. `SecretBox` gives authenticated encryption with a 192-bit random nonce per record (collision-safe at any realistic volume) in one call. Cost honesty: `import nacl` measures ~60ms on this Linux box — the module is therefore imported **only** by the gateway/hostlet write path (`whirlwind/secrets.py`), never by the sandbox cold-start chain (`agent.server`, `echo_server`), so the ADR-0008-era cold-start budget is untouched. Envelope format is versioned — `v1:<key_id>:<base64(nonce||ct||tag)>` — where `key_id` is a non-secret digest prefix of the master key (wrong-key decrypts fail fast with a clear error; the prefix is the rotation seam a future ADR needs).

**中文.** 新增基础依赖 `pynacl`。理由：ADR-0003 的先例是「成熟库优先于自造轮子」——*哪怕只是 cron 解析器*；密码学是该规则的最强形态，而标准库没有 AEAD。`SecretBox` 一行调用即得认证加密，每条记录 192-bit 随机 nonce（任何现实量级下无碰撞之虞）。成本诚实注记：本机实测 `import nacl` 约 60ms——因此该模块**只**被 gateway/hostlet 的写入路径（`whirlwind/secrets.py`）导入，沙箱冷启动链（`agent.server`、`echo_server`）永不导入，ADR-0008 时代的冷启动预算不受影响。信封格式带版本——`v1:<key_id>:<base64(nonce||ct||tag)>`——其中 `key_id` 是主密钥的非机密摘要前缀（错钥解密封快速失败并给出清晰错误；该前缀也是未来轮转 ADR 需要的接缝）。

## Decision — D3: master key — `WHIRLWIND_SECRET_KEY` env var, dev fallback auto-generated `data_dir/secret.key` (0600)

## 决策——D3：主密钥——`WHIRLWIND_SECRET_KEY` 环境变量，开发兜底自动生成 `data_dir/secret.key`（0600）

**English.** The master key is a 32-byte value, base64-encoded in the environment (`runtime.secret_key_env` names the variable, mirroring `api_key_env` — values never enter config files, ADR-0009 D5). When absent, a key is generated and persisted to `data_dir/secret.key` with `0600` so dev/test boot with zero ceremony. Honest caveat, stated here and in the code: the fallback key sits on the same disk as the ciphertexts — it protects against accidental exposure of the metadata artifacts (backup leaks, `GET /agents` dumps, repo commits), not against an attacker with host filesystem access. Production deployments provide the env var (the k3s path wires it from a Kubernetes Secret).

**中文.** 主密钥是 32 字节值，base64 编码放在环境里（`runtime.secret_key_env` 命名该变量，与 `api_key_env` 同构——值永不进配置文件，ADR-0009 D5）。缺失时自动生成并持久化到 `data_dir/secret.key`（权限 0600），开发/测试零仪式可启动。诚实注记（此处与代码中均写明）：兜底密钥与密文同盘——它防的是元数据产物的意外暴露（备份泄漏、`GET /agents` dump、误提交仓库），不防拿到宿主文件系统访问权的攻击者。生产部署应提供环境变量（k3s 路径从 Kubernetes Secret 接线）。

## Decision — D4: name validation — `WHIRLIND_*` platform namespace and the relay boundary are reserved

## 决策——D4：名字校验——`WHIRLWIND_*` 平台命名空间与 relay 边界保留

**English.** At seal time the gateway rejects: names starting with `WHIRLWIND_` (platform injection namespace: `WHIRLWIND_SANDBOX_ID`, `WHIRLWIND_MANIFEST`, …) and the exact name `DEEPSEEK_API_KEY` (the SecretRelay placeholder — a user-supplied real key there would smuggle a live platform credential into the sandbox, breaking the architecture's egress boundary). Validation is defense-in-depth layer one; layer two is injection precedence (D5).

**中文.** 封存时网关拒绝：`WHIRLWIND_` 前缀名（平台注入命名空间：`WHIRLWIND_SANDBOX_ID`、`WHIRLWIND_MANIFEST`……）与精确名 `DEEPSEEK_API_KEY`（SecretRelay 占位符——用户在那里放真 key 等于把活的平台凭证偷运进沙箱，破坏架构的出网边界）。校验是纵深防御第一层；第二层是注入优先级（D5）。

## Decision — D5: injection at provision time, precedence `bundle.env < user secrets < prepared.env`

## 决策——D5：供给期注入，优先级 `bundle.env < 用户密钥 < prepared.env`

**English.** The Hostlet resolves and decrypts the version's secrets in `ensure()` and merges them into the **harness env** (the `runtime.json` env consumed by the harness child process — not the SandboxAgent's own `WHIRLWIND_*` env). Precedence: image bundle defaults are overridden by user secrets, which are overridden by adapter-prepared wiring. This ordering means even a name that slipped past validation cannot clobber `DSH_*`/`DEEPSEEK_*` platform wiring — the relay boundary survives a hostile definition. Secrets are decrypted per-provision and live only in process env + the sandbox's own runtime.json; they are never written into the `InjectionManifest` (which is persisted as `manifest.json` and echoed into events) — the manifest carries nothing but names via the version.

**中文.** Hostlet 在 `ensure()` 里解析并解密该版本的密钥，合并进 **harness env**（`runtime.json` 里 harness 子进程消费的 env——而非 SandboxAgent 自身的 `WHIRLWIND_*` env）。优先级：镜像 bundle 默认 < 用户密钥 < adapter 装配接线。这个顺序意味着即使某个名字绕过了校验也无法覆盖 `DSH_*`/`DEEPSEEK_*` 平台接线——relay 边界扛得住恶意定义。密钥按供给解密，只存在于进程 env 与沙箱自身的 runtime.json；永不写入 `InjectionManifest`（它会持久化为 `manifest.json` 并回显进事件）——manifest 经由版本只携带名字。

## Decision — D6: `SecretStore` protocol + local-file backend; API is write-only for values

## 决策——D6：`SecretStore` 协议 + 本地文件后端；API 对值只写

**English.** `storage/providers.py` gains a `SecretStore` protocol (`put_version_env` / `get_version_env` / `delete_version_env`, envelope strings in and out — the store never sees plaintext, decryption stays in `whirlwind/secrets.py`). Backends landed now: `LocalFileSecretStore` (default; one `0600` JSON file of envelopes per version under `data_dir/secrets/`) and an in-memory implementation for the shared contract tests. REST surface: `POST /agents` (and version creation) accepts `"env": {NAME: value}`; responses carry `env_secrets` names only; **no endpoint ever returns values**. Deleting/replacing a version's env replaces the whole envelope set (immutable version unit — no partial mutation).

**中文.** `storage/providers.py` 新增 `SecretStore` 协议（`put_version_env` / `get_version_env` / `delete_version_env`，进出都是信封字符串——存储永不接触明文，解密只在 `whirlwind/secrets.py`）。本轮落地后端：`LocalFileSecretStore`（默认；每版本一个 `0600` JSON 信封文件，位于 `data_dir/secrets/`）与共享契约测试用的进程内实现。REST 面：`POST /agents`（及版本创建）接受 `"env": {NAME: value}`；响应只携带 `env_secrets` 名字；**任何端点都不返回值**。删除/替换版本的 env 是整组信封替换（不可变版本单元——无部分变更）。

## Detailed design

## 详细设计

```python
# whirlwind/secrets.py (host-side only; never imported by sandbox processes)
SecretBox.from_master_key(key: bytes) -> SecretBox          # key_id = sha256(key)[:8].hex()
SecretBox.seal(plaintext: str) -> str                        # "v1:<key_id>:<b64(nonce||ct||tag)>"
SecretBox.open(envelope: str) -> str                         # wrong key_id / bad tag -> SecretBoxError
SecretBox.from_env_or_file(env_name: str, data_dir: Path) -> SecretBox
validate_env_names(names: Iterable[str]) -> None             # D4 rejections, SecretNameError
```

```python
# storage/providers.py (new protocol) — envelope strings only
class SecretStore(Protocol):
    async def put_version_env(self, version_id: str, envelopes: dict[str, str]) -> None: ...
    async def get_version_env(self, version_id: str) -> dict[str, str]: ...
    async def delete_version_env(self, version_id: str) -> None: ...
```

Runtime wiring: `RuntimeConfig.secret_key_env: str = "WHIRLWIND_SECRET_KEY"` (config schema + env override `WHIRLWIND_SECRET_KEY_ENV`, ADR-0009 ladder); `WhirlwindRuntime` builds one `SecretBox` + `LocalFileSecretStore` and hands both to gateway (seal on write) and hostlet (open on provision).

运行时接线：`RuntimeConfig.secret_key_env: str = "WHIRLWIND_SECRET_KEY"`（进配置 schema + env 覆盖 `WHIRLWIND_SECRET_KEY_ENV`，走 ADR-0009 阶梯）；`WhirlwindRuntime` 构造唯一 `SecretBox` + `LocalFileSecretStore`，同时交给 gateway（写入时封存）与 hostlet（供给时解密）。

## Test strategy

## 测试策略

- Unit (`tests/unit/test_secrets.py`): box round-trip; envelope format & key_id; wrong-key failure; tamper detection; `from_env_or_file` env-wins + 0600 perms; name validation (reserved prefix, relay name, empty/invalid); `AgentVersion.env_secrets` round-trips through the model with no value surface.
- Contract (shared, memory vs local file store): put/get/delete semantics, replace-on-put, isolation between versions.
- Integration (real files, real processes, no mocks): create an agent with `env` via REST → (a) `GET` responses contain names only; (b) the on-disk secrets file contains the envelope, not the plaintext; (c) file mode is 0600; (d) a real session provision reads the decrypted value into the sandbox `runtime.json` harness env (gateway → manager → hostlet → real process driver chain); (e) a reserved name is rejected with a 4xx and nothing is persisted.

- 单元（`tests/unit/test_secrets.py`）：box 往返；信封格式与 key_id；错钥失败；篡改检测；`from_env_or_file` env 优先 + 0600 权限；名字校验（保留前缀、relay 名、空/非法）；`AgentVersion.env_secrets` 经模型往返且无值表面。
- 契约（共享，memory vs 本地文件存储）：put/get/delete 语义、put 即整组替换、版本间隔离。
- 集成（真实文件、真实进程、无 mock）：经 REST 创建带 `env` 的 agent →（a）`GET` 响应只有名字；（b）落盘 secrets 文件含信封不含明文；（c）文件模式 0600；（d）真实会话供给把解密值读进沙箱 `runtime.json` 的 harness env（gateway → manager → hostlet → 真实 process driver 链）；（e）保留名被 4xx 拒绝且不落任何东西。

## Conflict check

## 冲突检查

- Architecture §6.2 / ADR-0001 D-secret-relay: the platform LLM credential still never enters the sandbox (placeholder + relay unchanged). This ADR adds *user-owned* secrets whose destination is, by definition, the sandbox — a different trust zone; the relay boundary is doubly protected (D4 validation + D5 precedence).
- ADR-0009 D5: secret **values** stay env-only — the only new config key is the *name* of the key-holding env var, exactly the `api_key_env` pattern.
- ADR-0003 (minimal deps): `pynacl` is a new base dependency — justified here per the same ADR's own principle (mature libs over hand-rolled), and import-isolated from the sandbox cold-start path.
- ADR-0004 (storage seams): `SecretStore` follows the provider-protocol pattern; PG/Redis secret backends are future additions behind the same interface.

- 架构 §6.2 / ADR-0001 SecretRelay：平台 LLM 凭证依然永不进沙箱（占位符 + relay 不变）。本 ADR 增加的是*用户自有*密钥，其目的地本就是沙箱——不同的信任域；relay 边界获得双重保护（D4 校验 + D5 优先级）。
- ADR-0009 D5：密钥**值**保持仅环境变量——唯一新增配置项是持钥环境变量的**名字**，与 `api_key_env` 完全同构。
- ADR-0003（最小依赖）：`pynacl` 是新的基础依赖——依据正是该 ADR 自身的原则（成熟库优先于自造），且导入与沙箱冷启动路径隔离。
- ADR-0004（存储接缝）：`SecretStore` 沿 provider 协议模式；PG/Redis 密钥后端是同一接口下的未来扩展。

## Risks / open points

## 风险与开放点

- **Key rotation** is not implemented: swapping the env key invalidates existing envelopes (fail-fast via key_id). Migration tooling (decrypt-old/re-encrypt-new scan) is future work; the `v1` envelope prefix is the seam.
- **PostgreSQL/Redis SecretStore backends** deferred; on a PG-metadata deployment today, secrets stay in the local data_dir (single-host assumption of the all-in-one form). A cluster form needs the store to follow the metadata backend.
- **Leak-surface honesty**: values DO appear in the harness process env inside the sandbox — that is the feature. The platform protects at-rest + API surfaces; it cannot protect a harness that prints its own env into model context (agent-side redaction is the harness's problem, per the research above).
- The auto-generated dev key file (`data_dir/secret.key`) is a convenience, not a security control (D3 caveat).

- **密钥轮转**未实现：换 env 密钥即使既有信封失效（经 key_id 快速失败）。迁移工具（旧钥解密/新钥重加密扫描）是后续工作；`v1` 信封前缀即接缝。
- **PostgreSQL/Redis SecretStore 后端**推迟；今天在 PG 元数据部署上，密钥仍在本地 data_dir（all-in-one 形态的单机假设）。集群形态需要存储跟随元数据后端。
- **泄漏面诚实**：值确实出现在沙箱内 harness 进程 env——这就是功能本身。平台保护的是静态 + API 表面；无法保护把自身 env 打进模型上下文的 harness（agent 侧脱敏是 harness 的问题，见上文调研）。
- 自动生成的开发密钥文件（`data_dir/secret.key`）是便利设施，不是安全控制（D3 注记）。
