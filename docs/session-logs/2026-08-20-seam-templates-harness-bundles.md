# Session: Seam 模板/实例 + Harness 组合 + Agent 绑定模型（2026-08-20）

## 目标

用户任务 #6：seam 增扩为「带参数占位符的模板 + 带具体参数的实例」（sandbox 绑定的是实例）；harness
上升为一等「整体镜像组合」概念（如 dsh = SDK + runtime exe + SandboxAgent 的组合）；agent 版本绑定
0..1 harness bundle + 0..N seam 实例，内联声明保持兼容。对应 ADR-0011 全部决策（D1–D7）落地。

## 前置

- 阅读 `seam/model.py`（三元组 + Renderer 纯函数）、`harness/adapter.py`、`imaging/`（ImageBundle
  = root + launcher + env，与 HarnessBundle 是两个层面：镜像是内容，bundle 是命名引用组合）。
- 网上调研（2026-08-20）：Helm chart 的 template/values/values.schema.json 模式（准入期校验、
  required fail-closed、secrets 永不进 values）；OCI image manifest（命名 manifest 组合内容寻址
  layers）。结论写入 ADR-0011 Context。
- ADR-0011 已定稿（上一个最小闭环，commit b500272）。

## 变更清单

闭环 1（目录实体与解析引擎，commit b500272）：

- `core/model.py`：`SeamParamSpec` / `SeamTemplate` / `SeamInstance` / `HarnessBundle`；
  `AgentVersion` 增 `harness_bundle`（0..1）与 `seam_instances`（0..N）。
- `seam/model.py`：`extract_placeholders` / `substitute`（整值保类型 / 其余内嵌，即 Helm
  `--set` vs `--set-json`）/ `validate_template_params` / `resolve_params` / `render_template`。
- `seam/catalog.py`：`SeamCatalog`（模板/实例 CRUD + `resolve_version_bindings` 合并查重）。
- `harness/bundles.py`：`HarnessBundles` + 内建 echo/dsh 兜底（store 文档可遮蔽内建）。
- `storage/providers.py` + `memory.py` + `postgres.py`：通用 catalog 四元组（kind 封闭集合，
  PG 单表 `catalog_docs(kind,name JSONB)`）——ADR-0011 D6。
- `tests/unit/test_seam_catalog.py`：27 个单测。

闭环 2（接线，本次提交）：

- `hostlet/hostlet.py`：`_resolve_version()`——供给期解析管线：bundle 引用 → harness/image/entrypoint
  默认 + env overlay；实例引用 → 内联 decl（合并查重）；引用存在而 wiring 缺失 → fail-closed。
  env 优先级扩展为 image < harness-bundle < user secrets < prepared（ADR-0010 D5 语义保持）。
- `gateway/app.py`：`/seam-templates`、`/seam-instances`、`/harness-bundles` 三组 CRUD（GET 列表/
  单个、POST/PUT upsert、DELETE）；`VersionIn` 增 `harness_bundle`/`seam_instances`（无 bundle 时
  harness/image_ref 仍必填——legacy 形状不变）；`_admit_version_payload()` 在 **agent 记录创建之前**
  完成全部准入校验（bundle 存在、显式值一致否则 422、实例急切解析无撞车），拒绝的载荷不留孤儿
  agent。新增 `Unprocessable`（422，code `whirlwind/unprocessable`）。
- `runtime.py`：组合根构造 `SeamCatalog` + `HarnessBundles`（同一 MetadataStore），hostlet 与
  gateway 双侧注入。
- `cli.py`：`agent create` 增 `--harness-bundle` / `--seam-instance`；`--harness/--image` 改为
  无 bundle 时必填（有 bundle 时可选，给了就必须一致）。
- 测试：`tests/integration/test_seam_catalog_gateway.py`（5 个：全流程到 manifest 断言 / 准入
  拒绝矩阵 / 实例 ConfigMap 活性 / bundle 遮蔽与 env overlay / 内联兼容）；`test_storage.py` 增
  catalog 契约测试（memory/postgres 参数化）；`test_cli.py` 增 bundle 旗标用例。

## 关键决策与发现

- **准入 vs 供给双层校验**：网关在创建时急切解析（gate, not cache——供给期会重新解析，实例引用
  是活的）；Hostlet 在 ensure() 时再次解析。同一 `resolve_version_bindings` 服务两处，语义一致。
- **孤儿 agent 修复**：最初把 bundle 解析放在 `_create_version`（agent 记录已创建之后），未知
  bundle 的 404 会留下无版本的 agent 记录，且同名重试会变成 409。把全部准入校验提升到
  `_admit_version_payload` 并在 `create_agent` 里先于 `store.create_agent` 调用——与 ADR-0010
  时代的 D4 注释意图一致（"a rejected payload must not orphan an agent"）。
- **422 vs 400**：`Unprocessable` 专门承载「形状合法但声明引用不一致」（bundle 不一致）；SeamError
  （未知实例/撞车）维持 400。ADR-0011 D4 的 422 契约由 `_STATUS_BY_ERROR` 显式映射。
- **pytest 同名模块碰撞**：`tests/unit/test_seam_catalog.py` 与 `tests/integration/test_seam_catalog.py`
  在无 `__init__.py` 的布局下 basename 冲突导致 collection error；集成侧改名
  `test_seam_catalog_gateway.py`（更准确：它测的是网关+供给链路）。
- **entrypoint 现状**：`AgentVersion.entrypoint` 与 `HarnessBundle.entrypoint` 均为声明性字段
  （存储+回显），启动 argv 仍来自镜像 manifest 的 launcher——与既有 version.entrypoint 行为一致，
  未额外承诺。真实消费点留待自定义 launcher 需求（诚实边界）。
- **native_seams 仅盘点**：GET /harness-bundles 可见 dsh 的 fs/shell/memory 声明面；不做准入拒绝
  （adapter 仍是执行真相，echo 的空 native_seams 若用于拒绝会破坏现有内联用法）。

## 验证证据

- `uv run python scripts/run_tests.py tests/unit -q` → PASS · 160 passed · exit=0
- `uv run python scripts/run_tests.py tests/integration/test_seam_catalog_gateway.py tests/integration/test_storage.py tests/integration/test_gateway.py -q` → PASS · 47 passed · exit=0
- `uv run python scripts/run_tests.py tests/integration/test_cli.py -q` → PASS · 1 passed · exit=0
- 全量（本 session 最终）：见 TODO/MEMORY 基线行（314 passed / 12 skipped；skip 与前基线同因：
  runsc/k8s/vsock/e2e 环境缺失，PG/Redis 不可达时存储契约参数化侧 skip）。
- 集成断言的是真实供给产物：workspace `.whirlwind/manifest.json`（seams[].policy 为替换后的具体值、
  `instance` 溯源字段）与 `runtime.json`（bundle env overlay 实际进入 harness 进程 env）。

## 遗留与 handoff

- HarnessBundle.entrypoint 的真实消费（自定义 launcher）未做——字段已就位，等需求。
- Headless（0 harness）版本按 ADR-0011 D4 推迟：等 seam 工具经 SandboxAgent 进沙箱后解锁。
- 实例引用「冻结」策略旋钮（创建时快照解析结果）是 ADR-0011 风险节列的开放点，未实现。
- 目录文档无租户归属（单租户 M1；随 P1.1 auth 重审）。
- CLI catalog 子命令按 D7 推迟（REST-first，可 curl）。
- 下一个大项：Loop E（delta 快照抽象，ADR-0012 地盘——通用 catalog 接缝已为其预留扩展点）。
