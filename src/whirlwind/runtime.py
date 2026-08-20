"""All-in-one runtime assembly (single-process M1 deployment).

Injects provider implementations into every module and wires the layers
bottom-up: storage -> imaging -> hostlet -> control -> gateway. Backend
selection (memory / PostgreSQL / Redis) is a RuntimeConfig concern resolved
here at the composition root — business modules keep receiving pure
Protocols (ADR-0004 D4). Nothing here knows about HTTP serving;
`whirlwind serve` (cli) owns the uvicorn process, tests may embed the app.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from whirlwind.bus import InProcessEventBus
from whirlwind.control import LifecycleManager, SessionManager, WarmPool, WarmPoolConfig
from whirlwind.drivers import ProcessDriver, Resources
from whirlwind.gateway.app import GatewayDeps, create_app
from whirlwind.gateway.cron import CronScheduler
from whirlwind.gateway.mcp import McpGateway, WorkspaceToolExecutor
from whirlwind.harness.adapter import default_registry
from whirlwind.harness.bundles import HarnessBundles
from whirlwind.hostlet import Hostlet, HostletConfig
from whirlwind.imaging import LocalRegistry
from whirlwind.seam.catalog import SeamCatalog
from whirlwind.seam.model import SeamRenderer
from whirlwind.secrets import SecretBox
from whirlwind.storage.local import LocalFileSecretStore
from whirlwind.storage.wal_eventlog import WALEventLog
from whirlwind.storage.memory import MemoryKVStore, MemoryMetadataStore
from whirlwind.timer.wheel import HierarchicalTimer


@dataclass
class RuntimeConfig:
    data_dir: Path
    repo_root: Path | None = None  # where image builds install whirlwind from; default: repo containing this package
    api_key_env: str = "DEEPSEEK_API_KEY"
    secret_key_env: str = "WHIRLWIND_SECRET_KEY"  # agent-env master key holder (ADR-0010 D3)
    llm_upstream: str = "https://api.deepseek.com"
    wheel_tick_ms: int = 20
    warm_pool: dict[str, int] | None = None  # agent_version_id -> min_warm; off when empty
    metadata_backend: str = "memory"  # "memory" | "postgres" (ADR-0004 D4)
    postgres_dsn: str | None = None
    kv_backend: str = "memory"  # "memory" | "redis" (ADR-0004 D4)
    redis_url: str | None = None
    sandbox_resources: Resources | None = None  # per-sandbox ceilings (ADR-0005 D1)
    max_live_sessions: int | None = None  # admission gate (ADR-0005 D2); None = uncapped
    driver: str = "process"  # substrate pinning: "process" | "runsc" (ADR-0012 D5)
    snapshot_mode: str = "full"  # "full" | "delta" (ADR-0012 D4/D6)
    snapshot_chain_max: int = 16  # delta compaction bound (ADR-0012 D4)

    def resolved_repo_root(self) -> Path:
        if self.repo_root is not None:
            return self.repo_root
        guess = Path(__file__).resolve().parents[2]  # src/whirlwind/runtime.py -> repo root (editable install)
        if (guess / "pyproject.toml").is_file():
            return guess
        return Path.cwd()


def build_driver(config: RuntimeConfig) -> Any:
    """Named-constructor registry for sandbox substrates (ADR-0012 D5).

    The composition root is where selection belongs: a named-but-unavailable
    substrate fails loudly (no silent fallback — a cluster that thinks it runs
    gVisor while running process groups is a security regression)."""
    snapshots_root = config.data_dir / "snapshots"
    if config.driver == "process":
        return ProcessDriver(snapshots_root=snapshots_root)
    if config.driver == "runsc":
        import shutil as _shutil

        from whirlwind.core.platform import current_facts
        from whirlwind.drivers import RunscDriver

        # ADR-0007: platform identity via facts, binary via a real probe —
        # neither is overridable, so a simulated identity cannot fake a
        # substrate into existence (honest-edge rule).
        if current_facts().system != "linux" or not _shutil.which("runsc"):
            raise ValueError(
                "driver='runsc' requires a real Linux host with the runsc "
                "binary on PATH (probe facts honored: no binary, no substrate "
                "— no silent fallback to process)"
            )
        return RunscDriver(
            state_root=config.data_dir / "runsc-state",
            snapshots_root=snapshots_root,
            work_root=config.data_dir / "runsc-work",
        )
    raise ValueError(
        f"unknown driver {config.driver!r} (expected 'process' or 'runsc')"
    )


def _fail_backend(backend: str, setting: str, extra: str) -> NoReturn:
    raise ValueError(
        f"{setting}={backend!r}: driver not installed — install the extra: pip install whirlwind[{extra}]"
    )


def _build_metadata_store(config: RuntimeConfig) -> Any:
    """Backend factory; the composition root is where selection belongs."""
    if config.metadata_backend == "memory":
        return MemoryMetadataStore(skills_dir=config.data_dir / "skills")
    if config.metadata_backend == "postgres":
        if not config.postgres_dsn:
            raise ValueError("metadata_backend='postgres' requires postgres_dsn")
        try:
            from whirlwind.storage.postgres import PostgresMetadataStore
        except ImportError:
            _fail_backend(config.metadata_backend, "metadata_backend", "postgres")
        return PostgresMetadataStore(config.postgres_dsn, skills_dir=config.data_dir / "skills")
    raise ValueError(f"unknown metadata_backend {config.metadata_backend!r} (expected 'memory' or 'postgres')")


def _build_kv_store(config: RuntimeConfig) -> Any:
    if config.kv_backend == "memory":
        return MemoryKVStore()
    if config.kv_backend == "redis":
        if not config.redis_url:
            raise ValueError("kv_backend='redis' requires redis_url")
        try:
            from whirlwind.storage.redis import RedisKVStore
        except ImportError:
            _fail_backend(config.kv_backend, "kv_backend", "redis")
        return RedisKVStore(config.redis_url)
    raise ValueError(f"unknown kv_backend {config.kv_backend!r} (expected 'memory' or 'redis')")


class WhirlwindRuntime:
    """Owns every module instance and their start/stop order."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        data_dir = config.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)

        # storage & comms providers
        self.store = _build_metadata_store(config)
        self.kv = _build_kv_store(config)
        self.event_log = WALEventLog(data_dir / "events")
        self.bus = InProcessEventBus()

        # agent-env secrets (ADR-0010): one box (key from env, dev fallback file),
        # one envelope store beside — never inside — the metadata.
        self.secret_box = SecretBox.from_env_or_file(config.secret_key_env, data_dir)
        self.secrets = LocalFileSecretStore(data_dir)

        # catalog faces (ADR-0011): seam templates/instances + harness bundles
        # over the SAME metadata store — one wiring, consumed by hostlet
        # (provision-time resolution) and gateway (CRUD + admission checks).
        self.seam_catalog = SeamCatalog(self.store)
        self.bundles = HarnessBundles(self.store)

        # timer + imaging + data plane
        self.wheel = HierarchicalTimer(tick_ms=config.wheel_tick_ms)
        self.images = LocalRegistry(data_dir / "images")
        self.driver = build_driver(config)
        if config.snapshot_mode == "delta" and not self.driver.capabilities().delta_snapshots:
            # boot-time honesty gate (ADR-0012 D4): an incapable substrate
            # must fail loudly at composition, not per-suspend.
            raise ValueError(
                f"snapshot_mode='delta' is not supported by driver {config.driver!r} "
                "(caps.delta_snapshots=False)"
            )
        self.hostlet = Hostlet(
            driver=self.driver,
            images=self.images,
            adapters=default_registry(),
            renderer=SeamRenderer(),
            store=self.store,
            event_log=self.event_log,
            bus=self.bus,
            secrets=self.secrets,
            secret_box=self.secret_box,
            seam_catalog=self.seam_catalog,
            bundles=self.bundles,
            config=HostletConfig(
                data_dir=data_dir,
                api_key_env=config.api_key_env,
                llm_upstream=config.llm_upstream,
                sandbox_resources=config.sandbox_resources,
                snapshot_mode=config.snapshot_mode,
                snapshot_chain_max=config.snapshot_chain_max,
            ),
        )

        # control plane
        self.lifecycle = LifecycleManager(self.wheel)
        self.pool = WarmPool(self.store, self.hostlet, self.kv, WarmPoolConfig(versions=config.warm_pool or {}))
        self.manager = SessionManager(
            self.store, self.hostlet, self.bus, self.lifecycle,
            pool=self.pool, max_live_sessions=config.max_live_sessions,
        )

        # gateway faces
        self.renderer = SeamRenderer()
        self.cron = CronScheduler(self.store, self.wheel, self.manager)
        self.mcp_executor = WorkspaceToolExecutor(self.store, data_dir)
        self.mcp = McpGateway(self.store, self.renderer, executor=self.mcp_executor)
        self._started = False
        self._stopped = False
        self.app = create_app(
            GatewayDeps(
                manager=self.manager,
                store=self.store,
                event_log=self.event_log,
                bus=self.bus,
                images=self.images,
                renderer=self.renderer,
                cron=self.cron,
                mcp=self.mcp,
                repo_root=config.resolved_repo_root(),
                kv=self.kv,
                secret_box=self.secret_box,
                secret_store=self.secrets,
                seam_catalog=self.seam_catalog,
                bundles=self.bundles,
            ),
            on_startup=self.start,
            on_shutdown=self.stop,
        )

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        await self.store.start()  # pool + DDL (PG) / connect + ping (Redis) — fail fast
        await self.kv.start()
        await self.hostlet.start()
        self.wheel.start()
        await self.manager.start()
        await self.cron.start()
        await self.pool.start()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if not self._started:
            return
        await self.pool.stop()
        await self.cron.stop()
        await self.manager.stop()
        await self.wheel.stop()
        await self.hostlet.aclose()
        await self.mcp_executor.aclose()
        self.event_log.close()
        await self.kv.aclose()
        await self.store.aclose()
