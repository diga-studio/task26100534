"""纯计算层：由工资与封顶额计算逐月应缴额（无持久化）。"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# 年月截止点，闭区间语义，月份 1..12
Month = tuple[int, int]

_RATE = Decimal("0.1")


def month_leq(a: Month, b: Month) -> bool:
    """a <= b。"""
    return a <= b


def month_next(a: Month) -> Month:
    """下一个月（可跨年）。"""
    year, month = a
    return (year + 1, 1) if month == 12 else (year, month + 1)


def iter_months(start: Month, end: Month) -> list[Month]:
    """[start, end] 闭区间逐月序列；start > end 时为空。"""
    months: list[Month] = []
    cur = start
    while month_leq(cur, end):
        months.append(cur)
        cur = month_next(cur)
    return months


def cumulative_due(cum_wage: int, cap: int) -> int:
    """累计应缴 = min(累计工资, 年度封顶额) * 10%，四舍五入到分。"""
    base = min(cum_wage, cap)
    return int((Decimal(base) * _RATE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def monthly_due_schedule(wages: list[int], cap: int) -> list[int]:
    """由 12 个月工资与年度封顶额，返回 12 个月逐月应缴额。

    月应缴额 = 当月累计应缴 - 上月累计应缴，年初以前累计为零。
    """
    if len(wages) != 12:
        raise ValueError("wages 必须恰好 12 个月")
    schedule: list[int] = []
    cum_wage = 0
    prev_due = 0
    for wage in wages:
        cum_wage += wage
        due = cumulative_due(cum_wage, cap)
        schedule.append(due - prev_due)
        prev_due = due
    return schedule
