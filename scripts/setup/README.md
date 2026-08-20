# scripts/setup — dependency install scripts（依赖安装脚本）

Each script installs one external dependency the whirlwind test matrix can
use, with **explicit OS requirements in its header** (per AGENTS.md §7:
detect, don't assume). All scripts are idempotent-safe to re-run and verify
their own result at the end.

每个脚本安装测试矩阵可用的一个外部依赖，**OS 依赖在脚本头部显式标注**
（对应 AGENTS.md §7：先检测，不预设）。脚本可重复执行，结束时自验。

| Script | Installs | OS support | Unblocks |
| --- | --- | --- | --- |
| `install-runsc.sh` | gVisor `runsc` (latest release, sha512-verified) + static busybox | **Linux only**, x86_64/aarch64（macOS 需在 Linux VM 内执行，如 colima） | `tests/integration/test_runsc_driver.py`（真实 gVisor 底座） |
| `install-postgres.sh` | PostgreSQL server + client + libpq headers | Ubuntu/Debian (apt) · RHEL/Fedora (dnf) · macOS (brew) | `whirlwind[postgres]` 集成测试 |
| `install-redis.sh` | Redis server | Ubuntu/Debian (apt) · RHEL/Fedora (dnf) · macOS (brew) | `whirlwind[redis]` 集成测试 |

Notes / 注记：

- **runsc on restricted containers**: 无 `CAP_SYS_ADMIN` 的容器里，whirlwind 的
  runsc driver 自动降级 rootless + `--network=none`（rootless 不支持 restore，
  runsc 上游限制——集成测试会如实 skip 对应用例，不伪造）。
- **/dev/vsock** 是内核/设备属性，不可安装——vsock 传输测试按真实探测门控。
- 安装后验证：`runsc --version`、`pg_isready`、`redis-cli ping`，再经 runner
  跑对应集成测试（`uv run python scripts/run_tests.py tests/integration/... -q`）。
- PG 测试库：`sudo -u postgres createdb whirlwind_test`，DSN 经
  `WHIRLWIND_POSTGRES_DSN` 或 `whirlwind.toml` 的 `[storage]` 提供。
