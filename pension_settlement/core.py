"""雇主养老缴费追溯结算：对外 API 与业务规则。"""

from __future__ import annotations

from pathlib import Path

from .errors import CutoffError, NotInitializedError, SettlementError, StateError
from .store import Store
from .types import Batch, DeltaEntry, Month


class Settlement:
    """单一缴费类型（雇主养老）的追溯结算引擎。

    状态机：initialize -> (amend* -> prepare -> commit)*。
    prepare 后存在待提交批次，此时只能 commit 或 close；关闭重开不丢状态。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._store = Store(db_path)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "Settlement":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 初始化 ----

    def initialize(
        self,
        employees: dict[str, str],
        wages: dict[str, dict[Month, int]],
        caps: dict[str, dict[int, int]],
        cutoff: Month | None,
    ) -> None:
        """导入员工、两个完整年度的月工资、国别年度封顶额与初始结算截止月。

        employees: 员工号 -> 国家；wages: 员工号 -> {(年, 月): 工资分}；
        caps: 国家 -> {年: 封顶额分}；cutoff 以内视为已按初值结清。
        """
        if self._store.is_initialized():
            raise StateError("已经初始化过，不能重复 initialize")
        years = _validate_inputs(employees, wages, caps, cutoff)
        initial_book: dict[tuple[str, int, int], int] = {}
        for eid, country in employees.items():
            dues = _monthly_dues(
                {m: wages[eid][m] for m in wages[eid]},
                {y: caps[country][y] for y in years},
                years,
            )
            for (year, month), amount in dues.items():
                if cutoff is not None and (year, month) <= cutoff:
                    initial_book[(eid, year, month)] = amount
        self._store.load_initial(employees, wages, caps, cutoff, initial_book)

    # ---- 修改 ----

    def amend_wage(self, employee_id: str, year: int, month: int, amount: int) -> None:
        """替换指定员工指定月份的工资（分，非负整数）。一轮可多次，以最后值为准。"""
        self._require_amendable()
        _require_non_negative(amount)
        wages = self._store.all_wages()
        if (employee_id, year, month) not in wages:
            raise SettlementError(f"未知的员工或月份: {employee_id} {year}-{month}")
        self._store.set_wage(employee_id, year, month, amount)

    def amend_cap(self, country: str, year: int, amount: int) -> None:
        """替换指定国家指定年度的封顶额（分，非负整数）。"""
        self._require_amendable()
        _require_non_negative(amount)
        if (country, year) not in self._store.all_caps():
            raise SettlementError(f"未知的国家或年度: {country} {year}")
        self._store.set_cap(country, year, amount)

    # ---- 准备与提交 ----

    def prepare(self, cutoff: Month) -> Batch:
        """计算截止月以内逐员工逐月的非零补差，不产生入账。

        截止月只能保持不变或前进一个月；存在未提交批次时不得再次准备。
        """
        self._require_initialized()
        if self._store.load_pending() is not None:
            raise StateError("存在未提交的准备结果，只能 commit 或关闭")
        current = self._store.get_cutoff()
        self._validate_cutoff_step(current, cutoff)
        book = self._store.book_amounts()
        entries: list[DeltaEntry] = []
        employees = self._store.all_employees()
        wages = self._store.all_wages()
        caps = self._store.all_caps()
        years = sorted({year for (_, year) in caps})
        for eid, country in employees.items():
            emp_wages = {(y, m): a for (e, y, m), a in wages.items() if e == eid}
            emp_caps = {y: caps[(country, y)] for y in years}
            for (year, month), due in _monthly_dues(emp_wages, emp_caps, years).items():
                if (year, month) > cutoff:
                    continue
                delta = due - book.get((eid, year, month), 0)
                if delta != 0:
                    entries.append(
                        DeltaEntry(employee_id=eid, year=year, month=month, amount=delta)
                    )
        batch = Batch(
            batch_id=self._store.next_batch_id(),
            cutoff=cutoff,
            entries=tuple(entries),
        )
        self._store.save_pending(batch)
        return batch

    def commit(self, batch: Batch) -> None:
        """提交准备结果：只追加补差明细并推进结算截止月。空批次也能提交。"""
        self._require_initialized()
        pending = self._store.load_pending()
        if pending is None:
            raise StateError("没有待提交的准备结果")
        if pending != batch:
            raise StateError("提交的批次与待提交的准备结果不一致")
        self._store.commit_batch(pending)

    # ---- 查询 ----

    def pending_batch(self) -> Batch | None:
        """关闭重开后取回尚未提交的准备结果。"""
        self._require_initialized()
        return self._store.load_pending()

    def current_cutoff(self) -> Month | None:
        self._require_initialized()
        return self._store.get_cutoff()

    # ---- 内部 ----

    def _require_initialized(self) -> None:
        if not self._store.is_initialized():
            raise NotInitializedError("尚未 initialize")

    def _require_amendable(self) -> None:
        self._require_initialized()
        if self._store.load_pending() is not None:
            raise StateError("存在未提交的准备结果，不能修改，只能 commit 或关闭")

    def _validate_cutoff_step(self, current: Month | None, target: Month) -> None:
        years = sorted({year for (_, year) in self._store.all_caps()})
        valid_months = {(y, m) for y in years for m in range(1, 13)}
        if target not in valid_months:
            raise CutoffError(f"截止月超出覆盖年度: {target}")
        if current is None:
            if target != (years[0], 1):
                raise CutoffError("首次准备只能前进到第一个月")
            return
        if target == current:
            return
        year, month = current
        nxt = (year + 1, 1) if month == 12 else (year, month + 1)
        if target != nxt:
            raise CutoffError(f"截止月只能保持不变或前进一个月: {current} -> {target}")


def _monthly_dues(
    wages: dict[Month, int], caps: dict[int, int], years: list[int]
) -> dict[Month, int]:
    """每年单独计算：月应缴额 = 相邻两月累计应缴额之差，年初以前为零。"""
    dues: dict[Month, int] = {}
    for year in years:
        cap = caps[year]
        cumulative_wage = 0
        previous_due = 0
        for month in range(1, 13):
            cumulative_wage += wages[(year, month)]
            due_cumulative = (min(cumulative_wage, cap) + 5) // 10
            dues[(year, month)] = due_cumulative - previous_due
            previous_due = due_cumulative
    return dues


def _require_non_negative(amount: int) -> None:
    if not isinstance(amount, int) or amount < 0:
        raise SettlementError(f"金额必须是非负整数分: {amount!r}")


def _validate_inputs(
    employees: dict[str, str],
    wages: dict[str, dict[Month, int]],
    caps: dict[str, dict[int, int]],
    cutoff: Month | None,
) -> list[int]:
    if not employees:
        raise SettlementError("员工列表不能为空")
    year_sets = {
        tuple(sorted({year for (year, _) in months})) for months in wages.values()
    }
    if len(year_sets) != 1:
        raise SettlementError("所有员工的工资必须覆盖相同的两个年度")
    years = sorted(year_sets.pop())
    if len(years) != 2 or years[1] != years[0] + 1:
        raise SettlementError("工资必须覆盖连续两个完整年度")
    full = {(y, m) for y in years for m in range(1, 13)}
    for eid in employees:
        if eid not in wages:
            raise SettlementError(f"员工缺少工资数据: {eid}")
        if set(wages[eid]) != full:
            raise SettlementError(f"员工工资未覆盖两个完整年度: {eid}")
        for amount in wages[eid].values():
            _require_non_negative(amount)
    for country in set(employees.values()):
        if country not in caps or set(caps[country]) != set(years):
            raise SettlementError(f"国家缺少两个年度的封顶额: {country}")
        for amount in caps[country].values():
            _require_non_negative(amount)
    if cutoff is not None and cutoff not in full:
        raise CutoffError(f"初始截止月超出覆盖年度: {cutoff}")
    return years
