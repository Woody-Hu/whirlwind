"""Five-field cron expression parser and next-fire computation.

Standard semantics: minute hour day-of-month month day-of-week (DOW 0 and 7 both
mean Sunday). Supports `*`, step `*/n`, ranges `a-b`, and comma lists. `?` is
accepted as `*` (common in cloud cron dialects).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta

_BOUNDS = [
    (0, 59),  # minute
    (0, 23),  # hour
    (1, 31),  # day of month
    (1, 12),  # month
    (0, 6),  # day of week
]


class CronParseError(ValueError):
    pass


@dataclass(frozen=True)
class CronExpr:
    minute: frozenset[int]
    hour: frozenset[int]
    dom: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]

    @classmethod
    def parse(cls, expr: str) -> "CronExpr":
        fields = expr.strip().split()
        if len(fields) != 5:
            raise CronParseError(f"expected 5 fields, got {len(fields)}: {expr!r}")
        sets = []
        for i, field in enumerate(fields):
            lo, hi = _BOUNDS[i]
            if i == 4:
                hi = 7  # both 0 and 7 mean Sunday; normalized below
            sets.append(cls._parse_field(field, lo, hi))
        return cls(*sets)

    @staticmethod
    def _parse_field(field: str, lo: int, hi: int) -> frozenset[int]:
        values: set[int] = set()
        for part in field.split(","):
            step = 1
            if "/" in part:
                part, step_s = part.split("/", 1)
                if not step_s.isdigit() or int(step_s) < 1:
                    raise CronParseError(f"bad step in {field!r}")
                step = int(step_s)
            if part in ("*", "?"):
                start, end = lo, hi
            elif "-" in part:
                a, b = part.split("-", 1)
                if not (a.isdigit() and b.isdigit()):
                    raise CronParseError(f"bad range in {field!r}")
                start, end = int(a), int(b)
            else:
                if not part.isdigit():
                    raise CronParseError(f"bad value in {field!r}")
                start = end = int(part)
                if step != 1 or start == end:
                    step = max(step, hi + 1)  # single value with step: fire once
            if start < lo or end > hi or start > end:
                raise CronParseError(f"value out of range in {field!r}")
            values.update(range(start, end + 1, step))
        if hi == 7 and 7 in values:  # DOW: normalize 7 -> 0 (Sunday)
            values.discard(7)
            values.add(0)
        if not values:
            raise CronParseError(f"empty field {field!r}")
        return frozenset(values)

    def next_after(self, after: datetime) -> datetime:
        """Smallest fire time strictly after `after`, searching at minute granularity."""
        t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(366 * 24 * 60 + 1):  # bounded: at most one year of minutes
            if self._matches(t):
                return t
            t += timedelta(minutes=1)
        raise CronParseError("no next fire time within one year")

    def _matches(self, t: datetime) -> bool:
        if t.minute not in self.minute or t.hour not in self.hour or t.month not in self.month:
            return False
        dom_match = t.day in self.dom
        dow_match = (t.weekday() + 1) % 7 in self.dow  # monday=0 -> cron sunday=0
        # POSIX cron: if both dom and dow are restricted, either may match
        dom_restricted = self.dom != frozenset(range(1, 32))
        dow_restricted = self.dow != frozenset(range(0, 7))
        if dom_restricted and dow_restricted:
            return dom_match or dow_match
        return dom_match and dow_match

    def describe(self) -> str:
        return f"m{sorted(self.minute)} h{sorted(self.hour)} dom{sorted(self.dom)} mon{sorted(self.month)} dow{sorted(self.dow)}"


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]
