"""Scheduler: picks the sandbox substrate for a placement request.

Capabilities are the only decision input (architecture 4.4): a request names
required Caps bits, the scheduler returns the drivers that truthfully satisfy
them. M1 ships cold starts on the single registered driver; warm-pool CAS
claiming joins here in M2 without touching callers.
"""

from __future__ import annotations

from argus.core.errors import NotFound
from argus.drivers import Caps, Density, SandboxDriver


class Scheduler:
    def __init__(self) -> None:
        self._drivers: list[tuple[SandboxDriver, int]] = []

    def register(self, driver: SandboxDriver, priority: int = 0) -> None:
        self._drivers.append((driver, priority))

    def drivers(self) -> list[SandboxDriver]:
        return [d for d, _ in self._drivers]

    def select(
        self,
        *,
        need_snapshot_data: bool = False,
        need_snapshot_full: bool = False,
        min_density: Density = Density.HIGH,
    ) -> SandboxDriver:
        def satisfies(caps: Caps) -> bool:
            if need_snapshot_data and not caps.snapshot_data:
                return False
            if need_snapshot_full and not caps.snapshot_full:
                return False
            return _density_rank(caps.density) >= _density_rank(min_density)

        ranked = sorted(self._drivers, key=lambda pair: (-pair[1],))
        for driver, _ in ranked:
            if satisfies(driver.capabilities()):
                return driver
        raise NotFound("no registered driver satisfies the requested capabilities")


_DENSITY_RANK = {Density.LOW: 0, Density.MEDIUM: 1, Density.HIGH: 2}


def _density_rank(density: Density) -> int:
    return _DENSITY_RANK[density]
