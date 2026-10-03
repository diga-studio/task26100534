"""公共数据类型。"""

from __future__ import annotations

from dataclasses import dataclass, field

# 月份用 (year, month) 二元组表示，month 取值 1..12。
Month = tuple[int, int]


@dataclass(frozen=True)
class DeltaEntry:
    """一条补差明细：某员工某月需要补记的金额（分，可正可负）。"""

    employee_id: str
    year: int
    month: int
    amount: int


@dataclass(frozen=True)
class Batch:
    """prepare 产生的待提交批次。"""

    batch_id: int
    cutoff: Month
    entries: tuple[DeltaEntry, ...] = field(default_factory=tuple)

    def totals(self) -> dict[str, dict[Month, int]]:
        """按员工 -> 月份 聚合的非零补差，便于断言。"""
        result: dict[str, dict[Month, int]] = {}
        for entry in self.entries:
            result.setdefault(entry.employee_id, {})[(entry.year, entry.month)] = entry.amount
        return result
