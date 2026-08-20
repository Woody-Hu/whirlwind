# Session: Agent 定义环境变量密钥——引用/值分离 + 静态加密 + 供给期注入（2026-08-20）

## 目标

用户任务清单第 7 项：「agent 定义中应该允许用户提供一些环境变量，并视为敏感信息进行保存后续放入sandbox中」。先调研最佳实践（K8s Secret↔Deployment 引用/值分离、GitHub Actions secrets、vault 模式），落 ADR-0010，再实现 SecretBox / SecretStore / 网关封存脱敏 / Hostlet 供给期解密注入全链路。

另有用户中途反馈：「性能优化需要合理——业务上必须的加载等就没有必要改成懒加载」。已完成全仓懒加载审计（各点位均符合：懒加载只用于当前路径确实不需要的导入），并将该原则固化进 AGENTS.md §3.3。

## 前置

- 阅读 ADR-0001 §6（SecretRelay：平台凭证永不进沙箱——本 ADR 增加的是用户自有密钥，不同信任域）、ADR-0003（成熟库优先先例）、ADR-0009 D5/D8（密钥值永不进配置文件）；
- 网络调研 agent 运行时/编排系统的密钥最佳实践：引用/值分离、静态加密（主密钥来自环境）、API 只写 + 全面脱敏、沙箱 env 与 LLM 上下文是不同信任域；
- 实测 `import nacl` ≈ 60ms（Linux 容器）——决定 pynacl 只进 gateway/hostlet 写入路径，沙箱冷启动链（agent.server / echo_server）永不导入；
- 核对 ADR-0008 时代冷启动基线：含 rlimits p50 197ms。

## 变更清单

- **ADR-0010**（新增，Accepted）：D1 名字进 `AgentVersion.env_secrets`、值进加密 SecretStore / D2 pynacl SecretBox（XSalsa20-Poly1305，信封 `v1:<key_id>:<b64(nonce||ct||tag)>`）/ D3 主密钥 `WHIRLWIND_SECRET_KEY` env + 开发兜底 `data_dir/secret.key`（0600，诚实注记：兜底密钥与密文同盘）/ D4 名字校验保留 `WHIRLWIND_*` 与 `DEEPSEEK_API_KEY`（relay 边界）/ D5 供给期注入优先级 `bundle.env < 用户密钥 < prepared.env` / D6 SecretStore 协议 + 本地文件后端 + API 对值只写。
- **src/whirlwind/secrets.py**（新增）：`SecretBox`（seal/open/seal_env/open_env/from_env_or_file）、`validate_env_names`（D4）、`SecretBoxError`/`SecretNameError`（稳定错误码 `whirlwind/secrets/box`、`whirlwind/secrets/name`）。
- **src/whirlwind/storage/providers.py**：新增 `SecretStore` 协议（信封进出，存储永不接触明文）。
- **src/whirlwind/storage/memory.py / local.py**：`MemorySecretStore`（契约测试用）与 `LocalFileSecretStore`（默认后端：每版本一个 0600 JSON 信封文件，put 即整组替换）。
- **src/whirlwind/core/model.py**：`AgentVersion.env_secrets: list[str]`（只存名字——每个 model_dump 表面最多泄漏名字）。
- **src/whirlwind/gateway/app.py**：`VersionIn.env`（只写面）；`_create_version` 校验 → 封存 → 先写信封再建版本（metadata 失败即清理信封）；`create_agent` 在创建 agent 记录**之前**校验（拒绝的 payload 不留孤儿 agent）；`_STATUS_BY_ERROR` 增加两个映射（SecretNameError→400 / SecretBoxError→500）。
- **src/whirlwind/hostlet/hostlet.py**：`ensure()` 经 `_resolve_env_secrets` 解密合并（fail-closed：声明了密钥但信封缺失/解密失败 = 供给失败，绝不静默缺凭证启动）；注入优先级 D5。
- **src/whirlwind/runtime.py / config.py**：`RuntimeConfig.secret_key_env`（`runtime.secret_key_env` 配置键 + `WHIRLWIND_SECRET_KEY_ENV` env 覆盖 + `config show` 渲染）；runtime 构造唯一 SecretBox + LocalFileSecretStore，注入 gateway 与 hostlet。
- **src/whirlwind/cli.py**：`agent create --env NAME`（值取本地 env，不进 shell history）/ `--env NAME=VALUE`。
- **deploy/k3s/manifest.yaml**：Secret 增 `WHIRLWIND_SECRET_KEY`（openssl rand -base64 32），Deployment envFrom secretKeyRef 接线（生产路径按 D3 走 env，不依赖兜底文件）。
- **AGENTS.md**：§3.3 依赖清单加 pynacl（ADR-0010 论证）+ 懒加载合理性原则；目录索引补 secrets.py。
- **测试**：`tests/unit/test_secrets.py`（21 用例：往返/信封格式/错钥/篡改/键加载优先级/0600/名字校验）；`tests/integration/test_storage.py` 增 SecretStore 契约（memory + local 双后端 4 用例 + 密文落盘专项）；`tests/integration/test_agent_env_secrets.py`（4 用例：全链路/保留名拒绝且零残留/D5 优先级实证/信封缺失 fail-closed）。

## 关键决策与发现

1. **值绝不进 AgentVersion**（D1 的由来）：`AgentVersion` 被 GET dump、序列化进元数据存储——值放里面等于从每个既有表面泄漏。名字列表 + version_id 键控的独立 SecretStore 是唯一不污染既有表面的形态。
2. **信封先写、版本后建**：`create_version` 是 upsert 语义（无冲突检查），因此顺序改为「封存 → 写信封（整组替换，可安全重试）→ 建版本；metadata 失败则清理信封」——任何失败方向都不留半状态。
3. **供给期 fail-closed**：版本声明了密钥但信封丢失/解密失败时，供给直接失败——静默启动一个缺凭证的 harness 是隐性配置错误，不是降级模式。集成测试用「删掉信封文件 → 首个 turn 必须失败且无沙箱启动」钉死。
4. **修复既有 bug**：`create_agent` 路由原本先建 agent 记录再建版本——版本校验失败会留下无版本的孤儿 agent。现在 payload 校验前置（`_validate_version_payload` 幂等，两处调用）。
5. **D5 优先级的实证测试**：用户密钥故意取名 `ECHO_LLM_MODEL`（通过名字校验）+ `model_config_decl.model` 声明——runtime.json 里 adapter 装配值获胜。平台接线扛得住敌意定义。
6. **冷启动不受影响**：pynacl 只被 `whirlwind.secrets`（gateway/hostlet 写路径）导入；沙箱冷路径（agent.server）不经过它。复测含 rlimits 冷启动 p50=192ms（250ms 线内，与上轮 197ms 相当）。
7. **cli.py 编辑事故**：一次 Edit 把 line 111 行尾 `}` 损坏成 `]`（dict 推导式闭合错配）——子进程形式的 CLI 集成测试立刻暴露 SyntaxError。教训：改完必须跑含子进程调用的集成测试，纯 ASGI 测试发现不了 CLI 语法问题。

## 验证证据

- `uv run python scripts/run_tests.py tests/unit/test_secrets.py -q` → **PASS · 21 passed · exit=0**
- `uv run python scripts/run_tests.py tests/integration/test_storage.py -q` → **PASS · 31 passed · exit=0**（含 SecretStore 契约双后端）
- `uv run python scripts/run_tests.py tests/integration/test_agent_env_secrets.py tests/integration/test_gateway.py -q` → **PASS · 13 passed · exit=0**（首轮 1 failed 暴露孤儿 agent bug，修复后全绿）
- 单元+集成回归（经 runner，Linux/x86_64 容器 + 本机 PG/Redis）：`PASS · 258 passed · 9 skipped · exit=0`（log：`.test-logs/20260820-011243-tests_unit_tests_integration_-q.log`）
- 全量回归（unit+integration+benchmark，经 runner）：`PASS · 280 passed · 12 skipped · exit=0`（log：`.test-logs/20260820-012548-tests_-q_-m_not_e2e.log`；skip = runsc×4、k8s×4、vsock×1、e2e×3，全部环境限制；上轮全量 248 passed）
- 冷启动基准（真实测量）：`uv run python -m pytest tests/benchmark/test_edge_bench.py -q -s -k cold` → `cold start with Resources(mem=256mb,cpu=30s,pids=64): p50=192ms min=178ms max=204ms`（250ms 基线内）

## 遗留与 handoff

- 密钥轮转未实现：换 env 密钥使既有信封失效（key_id 快速失败）；迁移工具（旧钥解密/新钥重加密扫描）是后续工作，`v1` 前缀即接缝（ADR-0010 风险节已登记）。
- PostgreSQL/Redis SecretStore 后端推迟：PG 元数据部署上密钥仍在本地 data_dir（all-in-one 单机假设）；集群形态需存储跟随元数据后端。
- `config show --redact`（postgres_dsn 可能带凭证）沿袭 ADR-0009 遗留项，未做。
- 下一个 loop：Loop F（Seam 模板/实例分离 + Harness 组合 + Agent 绑定模型，需调研 + ADR-0011）或 Loop E（delta 快照抽象）。
