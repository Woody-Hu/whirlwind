# Session: 统一配置机制——单一 TOML 分层注入（2026-08-20）

## 目标

用户任务清单第 4 项：「整体系统不可以有强制的写死配置，所有的配置统一到一个配置文件中通过配置机制注入（这条可以加入 agents.md）」。落地为 ADR-0009 + `config.py` + CLI 集成 + AGENTS.md §3.5 规则，并以 k3s manifest 切换到 ConfigMap 挂载 TOML 作为部署侧验证。

## 前置

- 阅读 ADR-0004 D4（后端选择是 RuntimeConfig 关注点）、ADR-0005 D1/D2（资源上限/会话配额配置项）、ADR-0007 D2（`WHIRLWIND_PLATFORM` 保持 env-only）；
- 盘点三处重复的默认值：`cli.py` argparse 默认、`RuntimeConfig`/`HostletConfig` dataclass 默认、k3s manifest 经 ConfigMap→env→CLI 的间接管道；
- 确认 Python 3.12 下限 ⇒ stdlib `tomllib` 零新增依赖（§3.3 最小依赖原则）。

## 变更清单

- **ADR-0009**（新增，Accepted）：D1 优先级阶梯（代码默认 < TOML < `WHIRLWIND_*` env < CLI）/ D2 TOML + tomllib / D3 文件发现顺序 / D4 schema 镜像组合根 / D5 扁平 env 覆盖名 / D6 CLI 参数变纯覆盖（None 哨兵）/ D7 `config show` 自省 / D8 刻意不进配置文件的六类（密钥值、沙箱内部注入变量、WHIRLWIND_PLATFORM、WHIRLWIND_URL、测试旋钮、WHIRLWIND_DSH_REPO）/ D9 规范落 AGENTS.md §3.5。
- **src/whirlwind/config.py**（新增）：`load_settings` 分层解析 + schema 强校验（未知分区/键硬错误，`whirlwind/config` 错误码）+ `render_toml`（生效配置 + 来源头）；`RuntimeConfig` 签名不变。
- **src/whirlwind/cli.py**：`serve` 全部旗标默认值改 `None` 哨兵、新增 `--config`，经 `load_settings` 装配运行时；新增 `config show` 子命令；`ConfigError` 走 `_die`（exit 1，稳定 CLI 错误契约）。
- **deploy/k3s/manifest.yaml**：ConfigMap 从 key/value env 管道改为挂载 `whirlwind.toml`（`serve --config /etc/whirlwind/whirlwind.toml`）；容器唯一 env 变量剩凭证本身（D5：值永不进文件）。
- **deploy/whirlwind.example.toml**（新增）：带注释的示例配置（镜像默认值），单测保证它永远通过真实 loader。
- **AGENTS.md**：新增 §3.5 统一配置规范；目录索引补 config.py / ADR-0009；§7 环境速查更新启动/后端切换条目。
- **tests/unit/test_config.py**（新增 29 用例）：优先级阶梯逐层、发现顺序四档、schema 校验各失败形态、复合值（warm_pool/resources）round-trip、`render_toml` 经 loader 回程、示例文件加载。
- **tests/integration/test_config_cli.py**（新增 4 用例）：真实子进程驱动 `config show`（文件+env 覆盖生效）、无层默认、坏文件 exit 1、`serve` 坏配置快速失败。
- **tests/integration/test_k3s_manifest.py**：静态断言改为 ConfigMap 挂载形态；新增 manifest 内嵌 TOML 经真实 loader 校验（dogfood：manifest 改坏在 CI 就红，不是 pod 起不来才发现）。

## 关键决策与发现

1. **`None` = unset 的单点语义**：TOML 无 null、env 是字符串、CLI None 哨兵在 `load_settings` 已跳过，因此校验器见到的 `None` 只能来自代码默认层——`_validate` 顶部统一放行。首轮实现把 `sandbox.max_live_sessions`（默认 None 的可选整数）放进 `_INT_KEYS` 校验分支直接炸掉 16 个用例，正是单测钉出来的。
2. **CLI 默认值去重**：此前 `--api-key-env`、`--llm-upstream` 等默认值在 argparse 与 `RuntimeConfig` 两处重复；现在默认值只在 loader 的 `_DEFAULTS` 存在一处，argparse 只声明「默认 8410」这类帮助文本。
3. **k3s 配置形态**：ConfigMap 挂载 TOML 替代 key/value→env→`$(VAR)` CLI 展开的三跳管道——配置成为可部署单元（D1），`config show` 让「实际在跑什么」可回答。Dockerfile CMD 保留显式 CLI 旗标（优先级阶梯的合法一层），standalone `docker run` 仍绑 0.0.0.0。
4. **serve 坏配置必须快速失败**：typo 的配置文件绝不能静默回落默认值绑错端口——集成测试用真实子进程钉死 exit 1 行为。

## 验证证据

- `uv run python -m pytest tests/unit/test_config.py -q` → **29 passed**（首轮 16 failed 暴露 None-unset bug，修复后全绿）。
- `uv run python -m pytest tests/integration/test_config_cli.py tests/integration/test_k3s_manifest.py -q` → **15 passed, 4 skipped**（skip = live k8s 用例，无控制平面，诚实跳过）。
- `uv run python -m pytest tests/integration/test_cli.py -q` → **1 passed**（serve 旧行为不变）。
- 手动：`whirlwind config show` 无层时打印 `port = 8410` + provenance `<no config file>`；`WHIRLWIND_PORT=9101` 时打印 `port = 9101`（env 层生效）。
- 全量回归（经 runner，Linux/x86_64 容器 + 本机 PG/Redis）：`PASS · 248 passed · 12 skipped · exit=0`（log：`.test-logs/20260820-001736-tests_-q_-m_not_e2e.log`；上轮基线 214 passed，+34 = config 单测 29 + config CLI 集成 4 + manifest 内嵌 TOML dogfood 1）。

## 遗留与 handoff

- `config show --redact`（postgres_dsn/redis_url 可能带凭证）未做，ADR-0009 风险节已登记。
- `WHIRLWIND_DSH_REPO`（构建期）仍 env-only；imaging 配置长大时按加法收编。
- 下一个 loop：Loop E（delta 快照抽象）或 Loop F（Seam 模板/实例 + Harness 组合）——建议先 F/E 中较小者收口再进 microsandbox 评估。
