"""独立的全量参考计算，仅用于测试。

与被测库的计算层不共享任何代码：用自己的取整与循环，
直接由原始输入全量重算逐月应缴额与应生成的补差。
"""
from __future__ import annotations

Month = tuple[int, int]


def _round_half_up_tenth(cents: int) -> int:
    """非负整数分的 10%，四舍五入到分（独立实现，不用 Decimal）。"""
    return (cents + 5) // 10


class Reference:
    """参考模型：保存原始输入，按需全量重算。"""

    def __init__(
        self,
        employees: list[tuple[str, str]],
        wages: list[tuple[str, int, int, int]],
        caps: list[tuple[str, int, int]],
        cutoff: Month,
    ) -> None:
        self.country = dict(employees)
        self.wages: dict[tuple[str, int, int], int] = {
            (e, y, m): a for e, y, m, a in wages
        }
        self.caps: dict[tuple[str, int], int] = {(c, y): v for c, y, v in caps}
        self.years = sorted({y for _, y, _, _ in wages})
        self.cutoff = cutoff
        # 已结清账面：初始账面额 + 历史补差累计
        self.settled: dict[tuple[str, int, int], int] = {}
        for emp, _ in employees:
            for y in self.years:
                for m in range(1, 13):
                    if (y, m) <= cutoff:
                        self.settled[(emp, y, m)] = self.due(emp, y, m)

    def due(self, emp: str, year: int, month: int) -> int:
        """当前输入下某员工某月的应缴额（每年独立累计）。"""
        cap = self.caps[(self.country[emp], year)]
        cum_wage = 0
        prev = 0
        result = 0
        for m in range(1, month + 1):
            cum_wage += self.wages[(emp, year, m)]
            cum_due = _round_half_up_tenth(min(cum_wage, cap))
            result = cum_due - prev
            prev = cum_due
        return result

    def amend_wage(self, emp: str, year: int, month: int, amount: int) -> None:
        self.wages[(emp, year, month)] = amount

    def amend_cap(self, country: str, year: int, cap: int) -> None:
        self.caps[(country, year)] = cap

    def prepare(self, cutoff: Month) -> dict[tuple[str, int, int], int]:
        """期望的非零补差：{(emp, year, month): delta}。"""
        deltas: dict[tuple[str, int, int], int] = {}
        for emp in self.country:
            for y in self.years:
                for m in range(1, 13):
                    if (y, m) > cutoff:
                        continue
                    delta = self.due(emp, y, m) - self.settled.get((emp, y, m), 0)
                    if delta != 0:
                        deltas[(emp, y, m)] = delta
        self._pending = (cutoff, deltas)
        return deltas

    def commit(self) -> None:
        cutoff, deltas = self._pending
        for key, delta in deltas.items():
            self.settled[key] = self.settled.get(key, 0) + delta
        self.cutoff = cutoff
        self._pending = None
