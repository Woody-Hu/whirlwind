"""All-in-one runtime assembly (single-process M1 deployment).

Injects the in-process/local provider implementations into every module and
wires the layers bottom-up: storage -> imaging -> hostlet -> control ->
gateway. Nothing here knows about HTTP serving; `whirlwind serve` (cli) owns the
uvicorn process, tests may embed the app.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from whirlwind.bus import InProcessEventBus
from whirlwind.control import LifecycleManager, SessionManager, WarmPool, WarmPoolConfig
from whirlwind.drivers import ProcessDriver
from whirlwind.gateway.app import GatewayDeps, create_app
from whirlwind.gateway.cron import CronScheduler
from whirlwind.gateway.mcp import McpGateway, WorkspaceToolExecutor
from whirlwind.harness.adapter import default_registry
from whirlwind.hostlet import Hostlet, HostletConfig
from whirlwind.imaging import LocalRegistry
from whirlwind.seam.model import SeamRenderer
from whirlwind.storage.wal_eventlog import WALEventLog
from whirlwind.storage.memory import MemoryKVStore, MemoryMetadataStore
from whirlwind.timer.wheel import HierarchicalTimer


@dataclass
class RuntimeConfig:
    data_dir: Path
    repo_root: Path | None = None  # where image builds install whirlwind from; default: repo containing this package
    api_key_env: str = "DEEPSEEK_API_KEY"
    llm_upstream: str = "https://api.deepseek.com"
    wheel_tick_ms: int = 20
    warm_pool: dict[str, int] | None = None  # agent_version_id -> min_warm; off when empty

    def resolved_repo_root(self) -> Path:
        if self.repo_root is not None:
            return self.repo_root
        guess = Path(__file__).resolve().parents[2]  # src/whirlwind/runtime.py -> repo root (editable install)
        if (guess / "pyproject.toml").is_file():
            return guess
        return Path.cwd()


class WhirlwindRuntime:
    """Owns every module instance and their start/stop order."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        data_dir = config.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)

        # storage & comms providers
        self.store = MemoryMetadataStore(skills_dir=data_dir / "skills")
        self.event_log = WALEventLog(data_dir / "events")
        self.bus = InProcessEventBus()

        # timer + imaging + data plane
        self.wheel = HierarchicalTimer(tick_ms=config.wheel_tick_ms)
        self.images = LocalRegistry(data_dir / "images")
        self.hostlet = Hostlet(
            driver=ProcessDriver(snapshots_root=data_dir / "snapshots"),
            images=self.images,
            adapters=default_registry(),
            renderer=SeamRenderer(),
            store=self.store,
            event_log=self.event_log,
            bus=self.bus,
            config=HostletConfig(
                data_dir=data_dir,
                api_key_env=config.api_key_env,
                llm_upstream=config.llm_upstream,
            ),
        )

        # control plane
        self.lifecycle = LifecycleManager(self.wheel)
        self.kv = MemoryKVStore()
        self.pool = WarmPool(self.store, self.hostlet, self.kv, WarmPoolConfig(versions=config.warm_pool or {}))
        self.manager = SessionManager(self.store, self.hostlet, self.bus, self.lifecycle, pool=self.pool)

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
            ),
            on_startup=self.start,
            on_shutdown=self.stop,
        )

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
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
