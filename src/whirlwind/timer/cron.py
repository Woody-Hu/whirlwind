"""Five-field cron expression parsing and next-fire computation.

Schedule math is delegated to `croniter` (ADR-0003 D1) behind the historical
contract: exactly 5 fields, strict bounds, `?` accepted as `*` in any field,
and reversed numeric ranges rejected (croniter alone is lenient about the
last one). English names in the month / day-of-week fields (`JAN`, `MON-FRI`)
are a compatible extension contributed by croniter; Quartz 6-field syntax and
`@` macros stay outside the 5-field contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from croniter import croniter


class CronParseError(ValueError):
    pass


_NUMERIC_RANGE = re.compile(r"^(\d+)-(\d+)(?:/\d+)?$")


@dataclass(frozen=True)
class CronExpr:
    """A validated 5-field cron expression (`?` normalized to `*`)."""

    expr: str

    @classmethod
    def parse(cls, expr: str) -> "CronExpr":
        fields = expr.strip().split()
        if len(fields) != 5:
            raise CronParseError(f"expected 5 fields, got {len(fields)}: {expr!r}")
        for field in fields:
            for part in field.split(","):
                m = _NUMERIC_RANGE.match(part)
                if m is not None and int(m.group(1)) > int(m.group(2)):
                    raise CronParseError(f"reversed range in {field!r}")
        normalized = " ".join(f.replace("?", "*") for f in fields)
        try:
            croniter(normalized)
        except ValueError as exc:
            raise CronParseError(str(exc)) from exc
        return cls(normalized)

    def next_after(self, after: datetime) -> datetime:
        """Smallest fire time strictly after `after`, at minute granularity."""
        return croniter(self.expr, after).get_next(datetime)
