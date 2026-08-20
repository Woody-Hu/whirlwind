# ADR-0011: Seam templates & instances, harness bundles, and the agent binding model

# ADR-0011：Seam 模板/实例、Harness 组合与 Agent 绑定模型

- Status: Accepted
- Date: 2026-08-20
- Related: [ADR-0001](0001-agent-runtime-m1.md) D5（Seam 三层与 Renderer）、[ADR-0004](0004-production-storage.md)（provider 契约模式）、[ADR-0009](0009-unified-config.md) D4（schema 镜像组合根）、[ADR-0010](0010-agent-env-secrets.md) D1（引用/值分离先例）、[AGENTS.md](../../AGENTS.md) §3.1（开闭原则）、架构文档 4.6（能力 Seam）

- 状态：已接受
- 日期：2026-08-20
- 关联：[ADR-0001](0001-agent-runtime-m1.md) D5（Seam 三层与 Renderer）、[ADR-0004](0004-production-storage.md)（provider 契约模式）、[ADR-0009](0009-unified-config.md) D4（schema 镜像组合根）、[ADR-0010](0010-agent-env-secrets.md) D1（引用/值分离先例）、[AGENTS.md](../../AGENTS.md) §3.1（开闭原则）、架构文档 4.6（能力 Seam）

---

## Context

## 背景

**English.** Two gaps in today's binding model (user task list, 2026-08-20):

1. `SeamBindingDecl` is an *inline* declaration — every `AgentVersion` repeats the full `{seam, provider, policy, consumers}` quad. There is no reusable, parameterized form: two agents wanting "filesystem with mode X" each carry their own copy, and platform-recommended shapes (a vetted web-egress policy, a memory scope) cannot be packaged once and instantiated many times. The user's ask: *seams should have templates with parameter placeholders and instances with concrete parameters; what a sandbox binds are instances.*
2. "Harness" is a bare adapter-id string plus a separate `image_ref`. The DeepSeek harness as deployed is really an *integral image combination* (SDK + bundled runtime exe + the whirlwind SandboxAgent) — but that composition lives in hardcoded build functions (`imaging/base.py`), is invisible to the API, and cannot be varied (a dsh image with extra tools baked in) without code. The user's ask: *a first-class harness concept; an agent definition binds at most one harness image (possibly none) and multiple seam instances.*

Research (2026-08-20, web): the recurring pattern across packaging/orchestration systems is **template + values + schema validation** — Helm charts (parameterized templates, `values.schema.json` validation, required values fail closed, secrets never in values); OCI image manifests (a named manifest composing content-addressed layers, an index pointing at implementations). Both separate *what a thing is* (template/manifest, platform-curated) from *how it is configured for this use* (values/instance, user-owned) — and both validate the join at admission, not at execution.

**中文.** 今天绑定模型的两处缺口（用户任务清单，2026-08-20）：

1. `SeamBindingDecl` 是*内联*声明——每个 `AgentVersion` 重复完整的 `{seam, provider, policy, consumers}` 四元组。没有可复用的参数化形态：两个想要「mode 为 X 的文件系统」的 agent 各带一份拷贝；平台推荐的形态（审过的 web 出网策略、memory 作用域）无法「打包一次、实例化多次」。用户要求：*seam 应有带参数占位符的模板与带具体参数的实例；sandbox 里绑定的应该是实例。*
2. 「Harness」只是一根 adapter id 字符串加一个独立的 `image_ref`。DeepSeek harness 部署形态实际上是一个*整体镜像组合*（SDK + 打包的 runtime 可执行 + whirlwind SandboxAgent）——但这个组合硬编码在构建函数（`imaging/base.py`）里，API 不可见，也无法在不写代码的情况下做变体（多烤了工具的 dsh 镜像）。用户要求：*一等公民的 harness 概念；agent 定义绑定至多一个 harness 镜像（可以没有）与多个 seam 实例。*

调研（2026-08-20，网络）：打包/编排系统反复出现的模式是**模板 + 值 + schema 校验**——Helm chart（参数化模板、`values.schema.json` 校验、required 缺失即失败、密钥永不进 values）；OCI image manifest（命名 manifest 组合内容寻址的 layers，index 指向各实现）。两者都把「这个东西是什么」（模板/manifest，平台策展）与「这次怎么配」（values/实例，用户所有）分开——且都在准入时校验拼接结果，而非执行时。

## Decision — D1: `SeamTemplate` — a named, parameterized, stored seam-binding declaration

## 决策——D1：`SeamTemplate`——命名、参数化、可存储的 seam 绑定声明

**English.** A `SeamTemplate` is a catalog entity: `{name, seam, provider, policy, consumers, params, description}`. The `policy`/`consumer.config` bodies may contain `${param}` placeholders; `params` declares each parameter (`name`, `type` ∈ string/int/number/bool/list, `required`, `default`, `description`). Registration validates that every placeholder referenced in the body is a declared param (fail at registration, never at resolution) and that `seam`/`provider` exist in the `SeamRegistry`. Templates are platform-curated assets — updatable like a Helm chart (a new template revision changes future resolutions, never already-running sandboxes).

**中文.** `SeamTemplate` 是一个目录实体：`{name, seam, provider, policy, consumers, params, description}`。`policy`/`consumer.config` 体内可含 `${param}` 占位符；`params` 声明每个参数（`name`、`type` ∈ string/int/number/bool/list、`required`、`default`、`description`）。注册时校验体内引用的每个占位符都是已声明参数（注册期失败，绝不拖到解析期），且 `seam`/`provider` 在 `SeamRegistry` 中存在。模板是平台策展资产——可更新，语义同 Helm chart（模板新版本影响未来解析，绝不影响已在跑的沙箱）。

## Decision — D2: `SeamInstance` — template + concrete params; the unit a sandbox binds

## 决策——D2：`SeamInstance`——模板 + 具体参数；sandbox 绑定的单元

**English.** A `SeamInstance` is a catalog entity `{name, template, params, description}`. `AgentVersion.seam_instances: list[str]` references instances **by name**. Instance references are *live*: resolution happens at sandbox provision time (Kubernetes ConfigMap semantics — update the instance, and sandboxes provisioned afterwards see the new values; running sandboxes keep their resolved state, which the persisted `InjectionManifest` snapshots). Creation-time validation resolves eagerly and fails closed (unknown instance/template, missing required param, type mismatch, or a seam collision with an inline binding). The rendered `SeamBinding` gains an `instance` field recording provenance — the manifest answers "which instance produced this binding".

**中文.** `SeamInstance` 是目录实体 `{name, template, params, description}`。`AgentVersion.seam_instances: list[str]` **按名字**引用实例。实例引用是*活的*：解析发生在沙箱供给时（Kubernetes ConfigMap 语义——更新实例，之后供给的沙箱看到新值；运行中的沙箱保持已解析状态，由持久化的 `InjectionManifest` 快照）。创建期校验急切解析并 fail-closed（未知实例/模板、缺 required 参数、类型不匹配、或与内联绑定撞 seam）。渲染出的 `SeamBinding` 增加 `instance` 字段记录来源——manifest 能回答「这条绑定来自哪个实例」。

## Decision — D3: `${param}` substitution — type-preserving whole values, string-embedding otherwise

## 决策——D3：`${param}` 替换——整值保类型，其余按字符串内嵌

**English.** Substitution walks the template body (policy dicts, nested values, consumer configs) replacing every `${name}` occurrence. A string that is *exactly* one placeholder substitutes the param value with its native type (a list param becomes a list, an int an int); anything else embeds `str(value)` — the Helm `--set` vs `--set-json` distinction, deterministic and easy to reason about. Instance params are validated against the template spec: unknown keys, missing required-without-default, and type mismatches are `SeamError`s with stable code `whirlwind/seam`.

**中文.** 替换遍历模板体（policy 字典、嵌套值、consumer 配置），替换每个 `${name}` 出现。*恰好*是单个占位符的字符串按原生类型替换（list 参数替换成 list，int 替换成 int）；其余情况内嵌 `str(value)`——即 Helm `--set` 与 `--set-json` 的区分，确定性好推理。实例参数按模板 spec 校验：未知键、缺 required-且无-default、类型不匹配都是稳定错误码 `whirlwind/seam` 的 `SeamError`。

## Decision — D4: the agent binding model — `harness_bundle` 0..1 + `seam_instances` 0..N, legacy inline stays

## 决策——D4：Agent 绑定模型——`harness_bundle` 0..1 + `seam_instances` 0..N，内联形态保留

**English.** `AgentVersion` gains `harness_bundle: str = ""` and `seam_instances: list[str] = []`. The old fields (`harness`, `image_ref`, `seam_bindings`) remain fully supported — inline bindings and instance references merge (duplicate seam id across sources fails closed), and a version with no bundle keeps its explicit harness/image pair. When `harness_bundle` is set, the bundle supplies harness + image_ref (+ optional entrypoint/env defaults); an explicit value that disagrees with the bundle is a 422, not a silent override. Headless (zero harness) is **deferred**: turns require a harness process today, and the MCP seam path is host-side M1 (seam tools do not yet ride the SandboxAgent), so a declarable-but-unbootable state would be dishonest — the 0..1 shape lands in the schema now, the "0" case unlocks when in-sandbox seam serving arrives. This constraint is stated here so the eventual ADR can cite it.

**中文.** `AgentVersion` 新增 `harness_bundle: str = ""` 与 `seam_instances: list[str] = []`。旧字段（`harness`、`image_ref`、`seam_bindings`）完全保留——内联绑定与实例引用合并（跨来源撞 seam id 即 fail-closed），无 bundle 的版本继续用显式 harness/image 对。设置 `harness_bundle` 时由 bundle 提供 harness + image_ref（+ 可选 entrypoint/env 默认）；与之不一致的显式值是 422，不是静默覆盖。Headless（零 harness）**推迟**：turn 今天必须要有 harness 进程，且 MCP seam 路径是宿主侧 M1 子集（seam 工具尚未走 SandboxAgent 进沙箱），「可声明但不可启动」的状态不诚实——0..1 的形状现在落 schema，「0」的情形等沙箱内 seam 服务落地后解锁。此约束在此写明，供未来 ADR 引用修订。

## Decision — D5: `HarnessBundle` — the integral harness image combination, first-class

## 决策——D5：`HarnessBundle`——整体 harness 镜像组合，一等公民

**English.** A `HarnessBundle` is a catalog entity `{name, harness, image_ref, version, description, entrypoint, env, native_seams}` — the named answer to "what does it take to run this harness": the adapter id, the built image, and an optional entrypoint/env overlay. `native_seams` is a *declarative mirror* of what the adapter actually mounts natively (fs/shell/memory for dsh) — used for early validation and inventory, while the adapter remains the executor of truth (capability-honesty rule: the adapter fails closed on a seam it cannot mount, exactly as today). Builtins `echo` and `dsh` are resolved as fallbacks when not present in the store, so the concept is inspectable and bindable from day one with zero seeding ceremony; custom bundles (same harness adapter, different image — e.g. dsh-plus-tools) are plain CRUD.

**中文.** `HarnessBundle` 是目录实体 `{name, harness, image_ref, version, description, entrypoint, env, native_seams}`——「跑起来这个 harness 需要什么」的命名答案：adapter id、构建好的镜像、可选的 entrypoint/env 覆盖。`native_seams` 是 adapter 实际原生挂载能力的*声明式镜像*（dsh 的 fs/shell/memory）——用于早期校验与盘点，adapter 仍是执行真相（能力诚实原则：adapter 对挂不了的 seam 照旧 fail-closed）。内建 `echo` 与 `dsh` 在 store 中不存在时作为兜底解析——概念从第一天起可盘点、可绑定、零播种仪式；自定义 bundle（同一 harness adapter、不同镜像——如 dsh-plus-tools）就是普通 CRUD。

## Decision — D6: one generic catalog seam in MetadataStore, typed wrappers above it

## 决策——D6：MetadataStore 一个通用 catalog 接缝，类型化包装在其上

**English.** Rather than 3 entities × 4 typed methods (12 protocol methods × 3 backends), `MetadataStore` gains ONE generic quadruple: `put_catalog_doc(kind, doc)` / `get_catalog_doc(kind, name)` / `list_catalog_docs(kind)` / `delete_catalog_doc(kind, name)` with `kind` validated against a closed set (`seam_template` | `seam_instance` | `harness_bundle`). Postgres gets a single `catalog_docs(kind, name, body JSONB)` table; memory a nested dict. Typed validation and pydantic models live in wrapper modules (`seam/catalog.py`, `harness/bundles.py`) — the store stays dumb, the domain stays typed. Rationale: these entities are plain catalog documents (no lifecycle state machines like sessions/sandboxes — those keep typed methods); and the seam is the extension point for future catalog entities (delta-snapshot base registrations in ADR-0012's territory) with zero storage churn — the open-closed principle applied where it pays.

**中文.** 不做「3 实体 × 4 类型化方法」（12 个协议方法 × 3 后端），`MetadataStore` 增加**一**个通用四元组：`put_catalog_doc(kind, doc)` / `get_catalog_doc(kind, name)` / `list_catalog_docs(kind)` / `delete_catalog_doc(kind, name)`，`kind` 校验封闭集合（`seam_template` | `seam_instance` | `harness_bundle`）。Postgres 落一张 `catalog_docs(kind, name, body JSONB)` 表；memory 用嵌套 dict。类型化校验与 pydantic 模型放包装模块（`seam/catalog.py`、`harness/bundles.py`）——存储保持哑的，领域保持类型化。理由：这些实体就是普通目录文档（没有 session/sandbox 那样的生命周期状态机——那些保留类型化方法）；且该接缝是未来目录实体的扩展点（delta 快照 base 注册就是 ADR-0012 的地盘）零存储改动——开闭原则用在刀刃上。

## Decision — D7: gateway surface — catalog CRUD + resolve-before-create, provenance echoed

## 决策——D7：网关表面——目录 CRUD + 创建前解析，来源回显

**English.** REST: `GET/POST /seam-templates[/{name}]` (+ PUT/DELETE), the same for `/seam-instances` and `/harness-bundles`; `POST /agents` version payloads accept `harness_bundle` and `seam_instances`. Version creation resolves eagerly (bundle exists, instances exist and validate, no seam collisions) — a broken reference is a 4xx at admission, not a provision-time crash. Responses carry the declared references (bundle name, instance names) — the resolved concrete bindings appear in the provisioned `InjectionManifest`, which is where execution truth lives. CLI: `agent create` gains `--harness-bundle` / `--seam-instance` flags; catalog CRUD stays REST-first (curl-able), CLI subcommands deferred.

**中文.** REST：`GET/POST /seam-templates[/{name}]`（+ PUT/DELETE），`/seam-instances` 与 `/harness-bundles` 同构；`POST /agents` 的 version 载荷接受 `harness_bundle` 与 `seam_instances`。版本创建急切解析（bundle 存在、实例存在且校验通过、无 seam 撞车）——坏引用在准入期就是 4xx，不是供给期崩溃。响应回显声明引用（bundle 名、实例名）——解析后的具体绑定出现在供给的 `InjectionManifest` 里，那是执行真相的所在地。CLI：`agent create` 增加 `--harness-bundle` / `--seam-instance` 旗标；目录 CRUD 保持 REST 优先（可 curl），CLI 子命令推迟。

## Detailed design

## 详细设计

**English.** Resolution pipeline (hostlet `ensure()`): `HarnessBundles.resolve(version.harness_bundle)` → harness/image/entrypoint/env → `SeamCatalog.resolve_version_bindings(version)` → legacy inline decls + instance-materialized decls (dedup-checked) → renderer stays pure/sync, receiving a resolved version copy. The substitution engine (`seam/model.py`: `extract_placeholders` / `substitute` / `validate_template` / `validate_params`) is stdlib-only and unit-testable without any store. Template registration validates against the `SeamRegistry` (seam/provider exist) — the same registry the renderer uses, so a template can never declare something unrenderable.

**中文.** 解析管线（hostlet `ensure()`）：`HarnessBundles.resolve(version.harness_bundle)` → harness/image/entrypoint/env → `SeamCatalog.resolve_version_bindings(version)` → 内联声明 + 实例物化声明（查重）→ renderer 保持纯函数/同步，接收已解析的版本副本。替换引擎（`seam/model.py`：`extract_placeholders` / `substitute` / `validate_template` / `validate_params`）仅标准库、无 store 即可单测。模板注册对 `SeamRegistry` 校验（seam/provider 存在）——与 renderer 同一注册表，模板永远不可能声明出渲染不了的东西。

## Testing strategy

## 测试策略

**English.** Unit: substitution (whole-value type preservation, embedding, nested structures), template registration validation (unknown placeholder, unknown seam/provider), instance validation (unknown/missing/typed-wrong params), catalog wrapper round-trips, builtin bundle fallbacks. Integration (real subprocesses, no stubs): template → instance → version → turn, with the provisioned manifest asserting concrete substituted policy values and `instance` provenance; instance update → newly provisioned sandbox sees new values; unknown-reference creation rejected 4xx; legacy inline bindings still work unchanged; shared catalog contract tests across memory/postgres backends.

**中文.** 单元：替换（整值保类型、内嵌、嵌套结构）、模板注册校验（未知占位符、未知 seam/provider）、实例校验（未知/缺失/类型错参数）、catalog 包装往返、内建 bundle 兜底。集成（真实子进程，禁 stub）：模板 → 实例 → 版本 → turn，断言供给 manifest 中的具体替换值与 `instance` 来源；实例更新 → 新供给沙箱看到新值；未知引用创建被 4xx 拒绝；内联绑定原样可用；memory/postgres 后端共享 catalog 契约测试。

## Conflicts with the architecture document

## 与架构文档的冲突检查

**English.** Architecture 4.6's seam triple (Definition / Provider / Consumer) is unchanged — templates/instances are a *packaging layer above* the existing triple, and the InjectionManifest (the intermediate format crossing the sandbox boundary) keeps its shape, gaining only the provenance field. The "harness as black box" principle is strengthened: a HarnessBundle names a combination without exposing harness internals. ADR-0001 D5's renderer stays pure; resolution is additive plumbing around it. No deviations.

**中文.** 架构 4.6 的 seam 三元组（Definition / Provider / Consumer）不变——模板/实例是既有三元组*之上的打包层*；InjectionManifest（跨沙箱边界的中间格式）保持形状，只增加来源字段。「harness 黑盒」原则被强化：HarnessBundle 只命名组合，不暴露 harness 内部。ADR-0001 D5 的 renderer 保持纯净；解析只是其外围的增量管道。无偏差。

## Implementation order

## 实施顺序

**English.** (1) core models + generic catalog store (memory/postgres) + contract tests; (2) substitution engine + `SeamCatalog` + `HarnessBundles` wrappers + unit tests; (3) gateway CRUD + version binding + hostlet resolution + CLI flags + integration tests; (4) docs (TODO/MEMORY/session-log) — each step lands as its own minimal closed loop.

**中文.** （1）core 模型 + 通用 catalog 存储（memory/postgres）+ 契约测试；（2）替换引擎 + `SeamCatalog` + `HarnessBundles` 包装 + 单测；（3）网关 CRUD + 版本绑定 + hostlet 解析 + CLI 旗标 + 集成测试；（4）文档（TODO/MEMORY/session-log）——每步各自落地为最小闭环。

## Risks & open points

## 风险与开放点

**English.** Live instance references mean a version's effective bindings can drift after creation — intentional (ConfigMap semantics) but must be documented for operators; a future "freeze" (snapshot resolved bindings into the version at creation) is a policy knob we may add. `native_seams` can drift from adapter truth if misdeclared — the adapter still fails closed at provision, so the cost is a late error, not a broken sandbox. Headless versions remain deferred (D4). Catalog docs carry no per-tenant ownership yet (single-tenant M1 reality; revisit with P1.1 auth).

**中文.** 活实例引用意味着版本创建后有效绑定可能漂移——有意为之（ConfigMap 语义），但必须对运维者写明；未来的「冻结」（创建时把解析结果快照进版本）是可能加的策略旋钮。`native_seams` 声明错会与 adapter 真相漂移——adapter 供给时仍 fail-closed，代价是报错偏晚而非沙箱损坏。Headless 版本继续推迟（D4）。目录文档尚无租户归属（单租户 M1 现实；随 P1.1 auth 重审）。
