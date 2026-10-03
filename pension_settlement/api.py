"""对外接口：PensionSettlement。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import store
from .calc import Month, iter_months, month_next, monthly_due_schedule
from .errors import CutoffError, SettlementError, StateError

_PHASE_CLEAN = "clean"
_PHASE_PREPARED = "prepared"


@dataclass(frozen=True)
class Adjustment:
    emp_id: str
    year: int
    month: int
    delta: int


@dataclass
class Batch:
    """一次 prepare 的结果，可传给 commit。items 只含非零补差。"""
    batch_id: int
    cutoff: Month
    items: list[Adjustment] = field(default_factory=list)


class PensionSettlement:
    """雇主养老缴费追溯结算库（SQLite 持久化）。

    状态机：clean --prepare--> prepared --commit--> clean
    amend 仅在 clean 状态可用；prepared 状态只能 commit 或关闭重开。
    """

    def __init__(self, db_path: str) -> None:
        self._conn = store.connect(db_path)

    # ---- 内部工具 ----

    def _meta_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _meta_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _require_initialized(self) -> None:
        if self._meta_get("phase") is None:
            raise StateError("尚未 initialize")

    def _phase(self) -> str:
        self._require_initialized()
        return str(self._meta_get("phase"))

    def cutoff(self) -> Month:
        """当前结算截止月。"""
        self._require_initialized()
        return (int(self._meta_get("cutoff_year")), int(self._meta_get("cutoff_month")))

    def _years(self) -> tuple[int, int]:
        return (int(self._meta_get("year1")), int(self._meta_get("year2")))

    def _country_of(self, emp_id: str) -> str:
        row = self._conn.execute(
            "SELECT country FROM employees WHERE emp_id = ?", (emp_id,)
        ).fetchone()
        if row is None:
            raise SettlementError(f"未知员工: {emp_id!r}")
        return str(row["country"])

    def _current_schedule(self, emp_id: str, year: int) -> list[int]:
        """按当前输入计算某员工某年的逐月应缴额。"""
        country = self._country_of(emp_id)
        rows = self._conn.execute(
            "SELECT month, amount FROM wages WHERE emp_id = ? AND year = ? ORDER BY month",
            (emp_id, year),
        ).fetchall()
        wages = [int(r["amount"]) for r in rows]
        cap_row = self._conn.execute(
            "SELECT cap FROM caps WHERE country = ? AND year = ?", (country, year)
        ).fetchone()
        if cap_row is None:
            raise SettlementError(f"缺少封顶额: {country!r} {year}")
        return monthly_due_schedule(wages, int(cap_row["cap"]))

    def _booked_so_far(self) -> dict[tuple[str, int, int], int]:
        """初始账面额 + 全部已提交补差，按 (emp, year, month) 汇总。"""
        totals: dict[tuple[str, int, int], int] = {}
        for r in self._conn.execute("SELECT emp_id, year, month, amount FROM booked"):
            totals[(r["emp_id"], r["year"], r["month"])] = int(r["amount"])
        for r in self._conn.execute("SELECT emp_id, year, month, delta FROM adjustments"):
            key = (r["emp_id"], r["year"], r["month"])
            totals[key] = totals.get(key, 0) + int(r["delta"])
        return totals

    # ---- 操作 ----

    def initialize(
        self,
        employees: list[tuple[str, str]],
        wages: list[tuple[str, int, int, int]],
        caps: list[tuple[str, int, int]],
        cutoff: Month,
    ) -> None:
        """导入员工、两个完整年度的月工资、国别年度封顶额与初始截止月。

        employees: [(emp_id, country)]
        wages:     [(emp_id, year, month, amount)]，须覆盖每员工每年 12 个月
        caps:      [(country, year, cap)]，须覆盖每个国家两个年度
        cutoff:    初始结算截止月，截止月以内视为已按初值结清
        """
        if self._meta_get("phase") is not None:
            raise StateError("已初始化，不能重复 initialize")
        if not employees:
            raise SettlementError("员工列表不能为空")
        emp_ids = [e[0] for e in employees]
        if len(set(emp_ids)) != len(emp_ids):
            raise SettlementError("员工编号重复")
        years = sorted({w[1] for w in wages})
        if len(years) != 2 or years[1] != years[0] + 1:
            raise SettlementError("工资必须恰好覆盖连续两个年度")
        year1, year2 = years
        emp_set = set(emp_ids)
        countries = {e[1] for e in employees}
        seen: set[tuple[str, int, int]] = set()
        for emp_id, year, month, amount in wages:
            if emp_id not in emp_set:
                raise SettlementError(f"工资记录指向未知员工: {emp_id!r}")
            if year not in (year1, year2) or not 1 <= month <= 12:
                raise SettlementError(f"工资月份超出范围: {year}-{month}")
            if not isinstance(amount, int) or amount < 0:
                raise SettlementError("工资金额必须为非负整数分")
            key = (emp_id, year, month)
            if key in seen:
                raise SettlementError(f"工资记录重复: {key}")
            seen.add(key)
        if len(seen) != len(emp_ids) * 24:
            raise SettlementError("每名员工须恰好有 24 个月工资")
        cap_keys: set[tuple[str, int]] = set()
        for country, year, cap in caps:
            if year not in (year1, year2):
                raise SettlementError(f"封顶额年度超出范围: {year}")
            if not isinstance(cap, int) or cap < 0:
                raise SettlementError("封顶额必须为非负整数分")
            cap_keys.add((country, year))
        missing = {(c, y) for c in countries for y in (year1, year2)} - cap_keys
        if missing:
            raise SettlementError(f"缺少封顶额: {sorted(missing)}")
        cy, cm = cutoff
        if cy not in (year1, year2) or not 1 <= cm <= 12:
            raise CutoffError(f"初始截止月超出范围: {cutoff}")

        with self._conn:
            self._conn.executemany(
                "INSERT INTO employees(emp_id, country) VALUES(?, ?)", employees
            )
            self._conn.executemany(
                "INSERT INTO wages(emp_id, year, month, amount) VALUES(?, ?, ?, ?)",
                wages,
            )
            self._conn.executemany(
                "INSERT INTO caps(country, year, cap) VALUES(?, ?, ?)", caps
            )
            self._meta_set("year1", str(year1))
            self._meta_set("year2", str(year2))
            self._meta_set("cutoff_year", str(cy))
            self._meta_set("cutoff_month", str(cm))
            # 初始账面额：按初值计算截止月以内的逐月应缴
            for emp_id, _country in employees:
                for year in (year1, year2):
                    schedule = self._current_schedule(emp_id, year)
                    for month in range(1, 13):
                        if (year, month) <= cutoff:
                            self._conn.execute(
                                "INSERT INTO booked(emp_id, year, month, amount) "
                                "VALUES(?, ?, ?, ?)",
                                (emp_id, year, month, schedule[month - 1]),
                            )
            self._meta_set("phase", _PHASE_CLEAN)

    def amend_wage(self, emp_id: str, year: int, month: int, amount: int) -> None:
        """替换指定员工指定月份的工资。一轮可多次修改，以最后的值为准。"""
        if self._phase() != _PHASE_CLEAN:
            raise StateError("已 prepare 未 commit，不能再修改")
        year1, year2 = self._years()
        if year not in (year1, year2) or not 1 <= month <= 12:
            raise SettlementError(f"月份超出范围: {year}-{month}")
        if not isinstance(amount, int) or amount < 0:
            raise SettlementError("工资金额必须为非负整数分")
        self._country_of(emp_id)
        with self._conn:
            self._conn.execute(
                "UPDATE wages SET amount = ? WHERE emp_id = ? AND year = ? AND month = ?",
                (amount, emp_id, year, month),
            )

    def amend_cap(self, country: str, year: int, cap: int) -> None:
        """替换指定国家指定年度的封顶额。"""
        if self._phase() != _PHASE_CLEAN:
            raise StateError("已 prepare 未 commit，不能再修改")
        year1, year2 = self._years()
        if year not in (year1, year2):
            raise SettlementError(f"年度超出范围: {year}")
        if not isinstance(cap, int) or cap < 0:
            raise SettlementError("封顶额必须为非负整数分")
        with self._conn:
            cur = self._conn.execute(
                "UPDATE caps SET cap = ? WHERE country = ? AND year = ?",
                (cap, country, year),
            )
            if cur.rowcount == 0:
                raise SettlementError(f"未知国家: {country!r}")

    def prepare(self, cutoff: Month) -> Batch:
        """准备补差批次：截止月只能保持不变或前进一个月，不产生入账。"""
        if self._phase() != _PHASE_CLEAN:
            raise StateError("已有未提交的 prepare 结果")
        current = self.cutoff()
        year1, year2 = self._years()
        last = (year2, 12)
        if cutoff != current and cutoff != month_next(current):
            raise CutoffError(f"截止月只能保持不变或前进一个月: {current} -> {cutoff}")
        if cutoff > last:
            raise CutoffError(f"截止月超出最后月份: {cutoff}")

        booked = self._booked_so_far()
        items: list[Adjustment] = []
        emp_ids = [r["emp_id"] for r in self._conn.execute("SELECT emp_id FROM employees")]
        schedules: dict[tuple[str, int], list[int]] = {}
        for emp_id in emp_ids:
            for year in (year1, year2):
                schedules[(emp_id, year)] = self._current_schedule(emp_id, year)
        for year, month in iter_months((year1, 1), cutoff):
            for emp_id in emp_ids:
                due = schedules[(emp_id, year)][month - 1]
                settled = booked.get((emp_id, year, month), 0)
                delta = due - settled
                if delta != 0:
                    items.append(Adjustment(emp_id, year, month, delta))

        row = self._conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS nid FROM batches").fetchone()
        batch_id = int(row["nid"])
        with self._conn:
            self._conn.execute(
                "INSERT INTO prepared(id, batch_id, cutoff_year, cutoff_month) "
                "VALUES(1, ?, ?, ?)",
                (batch_id, cutoff[0], cutoff[1]),
            )
            self._conn.executemany(
                "INSERT INTO prepared_items(emp_id, year, month, delta) VALUES(?, ?, ?, ?)",
                [(a.emp_id, a.year, a.month, a.delta) for a in items],
            )
            self._meta_set("phase", _PHASE_PREPARED)
        return Batch(batch_id=batch_id, cutoff=cutoff, items=items)

    def pending_batch(self) -> Batch | None:
        """关闭重开后取回尚未提交的 prepare 结果。"""
        if self._meta_get("phase") != _PHASE_PREPARED:
            return None
        row = self._conn.execute("SELECT * FROM prepared WHERE id = 1").fetchone()
        items = [
            Adjustment(r["emp_id"], r["year"], r["month"], r["delta"])
            for r in self._conn.execute(
                "SELECT emp_id, year, month, delta FROM prepared_items "
                "ORDER BY year, month, emp_id"
            )
        ]
        return Batch(
            batch_id=int(row["batch_id"]),
            cutoff=(int(row["cutoff_year"]), int(row["cutoff_month"])),
            items=items,
        )

    def commit(self, batch: Batch) -> None:
        """提交准备结果：只追加补差明细并推进截止月。空批次也能提交。"""
        if self._phase() != _PHASE_PREPARED:
            raise StateError("没有待提交的 prepare 结果")
        pending = self.pending_batch()
        assert pending is not None
        if batch.batch_id != pending.batch_id:
            raise SettlementError(
                f"批次不匹配: 待提交 {pending.batch_id}, 收到 {batch.batch_id}"
            )
        with self._conn:
            self._conn.execute(
                "INSERT INTO batches(id, cutoff_year, cutoff_month) VALUES(?, ?, ?)",
                (pending.batch_id, pending.cutoff[0], pending.cutoff[1]),
            )
            self._conn.executemany(
                "INSERT INTO adjustments(batch_id, emp_id, year, month, delta) "
                "VALUES(?, ?, ?, ?, ?)",
                [
                    (pending.batch_id, a.emp_id, a.year, a.month, a.delta)
                    for a in pending.items
                ],
            )
            self._conn.execute("DELETE FROM prepared_items")
            self._conn.execute("DELETE FROM prepared")
            self._meta_set("cutoff_year", str(pending.cutoff[0]))
            self._meta_set("cutoff_month", str(pending.cutoff[1]))
            self._meta_set("phase", _PHASE_CLEAN)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PensionSettlement":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
