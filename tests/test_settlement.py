"""全量参考计算驱动的端到端测试。

参考模型独立实现计算规则（用 Decimal 做四舍五入），
测试脚本对 Settlement 与参考模型执行同一串操作并逐步比对。
"""

from __future__ import annotations

import sqlite3
from decimal import ROUND_HALF_UP, Decimal

import pytest

from pension_settlement import (
    CutoffError,
    NotInitializedError,
    Settlement,
    SettlementError,
    StateError,
)

YEARS = (2024, 2025)


def ref_monthly_dues(wages: dict[tuple[int, int], int], caps: dict[int, int]):
    """独立参考实现：月应缴额 = 相邻两月累计应缴额之差。"""
    dues = {}
    for year in YEARS:
        cap = caps[year]
        cum = 0
        prev = Decimal(0)
        for month in range(1, 13):
            cum += wages[(year, month)]
            due_cum = (Decimal(min(cum, cap)) / 10).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
            dues[(year, month)] = int(due_cum - prev)
            prev = due_cum
    return dues


class Reference:
    """独立维护"初始账面 + 全部已提交补差"的参考账本。"""

    def __init__(self, employees, wages, caps, cutoff):
        self.employees = dict(employees)
        self.wages = {eid: dict(m) for eid, m in wages.items()}
        self.caps = {c: dict(y) for c, y in caps.items()}
        self.cutoff = cutoff
        self.book = {}
        for eid in employees:
            for month, amount in self.dues(eid).items():
                if cutoff is not None and month <= cutoff:
                    self.book[(eid, *month)] = amount

    def dues(self, eid):
        return ref_monthly_dues(self.wages[eid], self.caps[self.employees[eid]])

    def amend_wage(self, eid, year, month, amount):
        self.wages[eid][(year, month)] = amount

    def amend_cap(self, country, year, amount):
        self.caps[country][year] = amount

    def expected_deltas(self, cutoff):
        """逐员工逐月的非零补差：{(员工, 年, 月): 金额}。"""
        out = {}
        for eid in self.employees:
            for month, due in self.dues(eid).items():
                if month <= cutoff:
                    delta = due - self.book.get((eid, *month), 0)
                    if delta:
                        out[(eid, *month)] = delta
        return out

    def commit(self, cutoff):
        for (eid, year, month), amount in self.expected_deltas(cutoff).items():
            key = (eid, year, month)
            self.book[key] = self.book.get(key, 0) + amount
        self.cutoff = cutoff


def make_inputs():
    employees = {"E1": "CN", "E2": "CN", "E3": "US"}
    wages = {
        "E1": {(y, m): 100000 for y in YEARS for m in range(1, 13)},
        "E2": {(y, m): 80000 for y in YEARS for m in range(1, 13)},
        "E3": {(y, m): 50000 for y in YEARS for m in range(1, 13)},
    }
    caps = {"CN": {2024: 550000, 2025: 550000}, "US": {2024: 700000, 2025: 700000}}
    return employees, wages, caps


def batch_deltas(batch):
    return {(e.employee_id, e.year, e.month): e.amount for e in batch.entries}


def db_book(db_path):
    """直接从 SQLite 读出 初始账面 + 全部历史补差 的逐月累计入账。"""
    conn = sqlite3.connect(str(db_path))
    book = {}
    for eid, y, m, a in conn.execute(
        "SELECT employee_id, year, month, amount FROM initial_book"
    ):
        book[(eid, y, m)] = a
    for eid, y, m, a in conn.execute(
        "SELECT employee_id, year, month, SUM(amount) FROM deltas"
        " GROUP BY employee_id, year, month"
    ):
        book[(eid, y, m)] = book.get((eid, y, m), 0) + a
    conn.close()
    return book


def check_book_matches_reference(db_path, ref):
    """截止月以内，每月累计入账必须恰好等于参考模型的月应缴额。"""
    book = db_book(db_path)
    for eid in ref.employees:
        for month, due in ref.dues(eid).items():
            if ref.cutoff is not None and month <= ref.cutoff:
                assert book.get((eid, *month), 0) == due, (eid, month)


def run_round(st, ref, cutoff, path):
    """准备一次并提交，逐步比对补差；提交后同截止月再准备应为空。"""
    batch = st.prepare(cutoff)
    assert batch_deltas(batch) == ref.expected_deltas(cutoff)
    st.commit(batch)
    ref.commit(cutoff)
    assert st.current_cutoff() == ref.cutoff
    again = st.prepare(cutoff)
    assert again.entries == ()
    st.commit(again)  # 空批次也能提交
    check_book_matches_reference(path, ref)


employees, wages, caps = make_inputs()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "settlement.db"


def test_cap_month_shifts_back_and_forth(db_path):
    """反复更正同一员工早期工资：封顶月份前移、后移再恢复。"""
    ref = Reference(employees, wages, caps, (2024, 3))
    st = Settlement(db_path)
    st.initialize(employees, wages, caps, (2024, 3))

    run_round(st, ref, (2024, 3), db_path)  # 无修改直接准备，应为空批次

    # 前移：1月工资 100000 -> 200000，累计更早触顶
    st.amend_wage("E1", 2024, 1, 200000)
    ref.amend_wage("E1", 2024, 1, 200000)
    run_round(st, ref, (2024, 3), db_path)
    st.amend_wage("E1", 2024, 1, 200000)  # 同值再改一次，不产生新补差
    run_round(st, ref, (2024, 4), db_path)

    # 后移：1月工资 200000 -> 50000
    st.amend_wage("E1", 2024, 1, 50000)
    ref.amend_wage("E1", 2024, 1, 50000)
    run_round(st, ref, (2024, 4), db_path)

    # 恢复：50000 -> 100000
    st.amend_wage("E1", 2024, 1, 100000)
    ref.amend_wage("E1", 2024, 1, 100000)
    run_round(st, ref, (2024, 4), db_path)

    # 一轮多次修改，以最后的值为准
    st.amend_wage("E1", 2024, 2, 300000)
    st.amend_wage("E1", 2024, 2, 111111)
    ref.amend_wage("E1", 2024, 2, 111111)
    run_round(st, ref, (2024, 5), db_path)

    # 调整国别年度封顶额
    st.amend_cap("CN", 2024, 480000)
    ref.amend_cap("CN", 2024, 480000)
    run_round(st, ref, (2024, 6), db_path)
    st.close()


def test_annual_total_unchanged_generates_offsetting_deltas(db_path):
    """年度总额未变但缴费月份改变：对应月份生成正负补差。"""
    ref = Reference(employees, wages, caps, (2024, 6))
    st = Settlement(db_path)
    st.initialize(employees, wages, caps, (2024, 6))

    # E3 不触顶：把 2 月的 20000 挪到 5 月，年度总额不变
    st.amend_wage("E3", 2024, 2, 70000)
    st.amend_wage("E3", 2024, 5, 30000)
    ref.amend_wage("E3", 2024, 2, 70000)
    ref.amend_wage("E3", 2024, 5, 30000)
    batch = st.prepare((2024, 6))
    deltas = batch_deltas(batch)
    e3 = {m: a for (e, y, m), a in deltas.items() if e == "E3"}
    # 2月多缴 2000，5月少缴 2000 冲回，年度总额不变
    assert e3 == {2: 2000, 5: -2000}
    assert sum(e3.values()) == 0
    st.commit(batch)
    ref.commit((2024, 6))
    check_book_matches_reference(db_path, ref)
    st.close()


def test_close_reopen_across_amend_and_prepare(db_path):
    """修改后与准备后分别关闭重开，再提交并跨年扩展截止月。"""
    ref = Reference(employees, wages, caps, (2024, 11))
    st = Settlement(db_path)
    st.initialize(employees, wages, caps, (2024, 11))
    st.amend_wage("E2", 2024, 1, 180000)
    st.amend_cap("US", 2024, 300000)
    st.close()  # 修改后关闭

    st = Settlement(db_path)  # 重开不丢修改
    ref.amend_wage("E2", 2024, 1, 180000)
    ref.amend_cap("US", 2024, 300000)
    batch = st.prepare((2024, 12))
    assert batch_deltas(batch) == ref.expected_deltas((2024, 12))
    st.close()  # 准备后关闭

    st = Settlement(db_path)  # 重开不丢准备结果
    pending = st.pending_batch()
    assert pending == batch
    st.commit(pending)
    ref.commit((2024, 12))
    assert st.current_cutoff() == (2024, 12)

    # 跨年扩展截止月，含空批次
    for cutoff in [(2025, 1), (2025, 2), (2025, 3)]:
        run_round(st, ref, cutoff, db_path)
    st.amend_wage("E1", 2025, 1, 90000)
    ref.amend_wage("E1", 2025, 1, 90000)
    st.close()
    st = Settlement(db_path)
    run_round(st, ref, (2025, 4), db_path)
    st.close()

    # 与不中断执行的参考结果一致
    check_book_matches_reference(db_path, ref)


def test_state_machine_rules(db_path):
    st = Settlement(db_path)
    st.initialize(employees, wages, caps, (2024, 3))

    with pytest.raises(StateError):
        st.initialize(employees, wages, caps, (2024, 3))

    with pytest.raises(CutoffError):
        st.prepare((2024, 5))  # 一次前进超过一个月
    with pytest.raises(CutoffError):
        st.prepare((2023, 12))  # 倒退

    batch = st.prepare((2024, 4))
    with pytest.raises(StateError):
        st.amend_wage("E1", 2024, 1, 1)  # 准备后不能修改
    with pytest.raises(StateError):
        st.prepare((2024, 4))  # 一轮只能准备一次
    with pytest.raises(StateError):
        st.amend_cap("CN", 2024, 1)

    from pension_settlement import Batch

    with pytest.raises(StateError):
        st.commit(Batch(batch_id=999, cutoff=(2024, 4)))  # 批次不一致

    st.commit(batch)
    with pytest.raises(StateError):
        st.commit(batch)  # 不能重复提交

    with pytest.raises(CutoffError):
        st.prepare((2024, 3))  # 倒退
    st.close()


def test_validation_and_rounding(db_path):
    st = Settlement(db_path)
    with pytest.raises(NotInitializedError):
        st.prepare((2024, 1))  # 未初始化

    bad_wages = {e: dict(m) for e, m in wages.items()}
    bad_wages["E1"][(2024, 1)] = -1
    with pytest.raises(SettlementError):
        st.initialize(employees, bad_wages, caps, None)

    # 四舍五入：月工资 105 分，累计 105 -> 11，210 -> 21，315 -> 32
    emps = {"E9": "CN"}
    w = {"E9": {(y, m): 105 for y in YEARS for m in range(1, 13)}}
    c = {"CN": {2024: 10**9, 2025: 10**9}}
    st.initialize(emps, w, c, None)
    batch = st.prepare((2024, 1))
    assert batch_deltas(batch) == {("E9", 2024, 1): 11}
    st.commit(batch)
    batch = st.prepare((2024, 2))
    assert batch_deltas(batch) == {("E9", 2024, 2): 10}
    st.commit(batch)
    batch = st.prepare((2024, 3))
    assert batch_deltas(batch) == {("E9", 2024, 3): 11}
    st.commit(batch)
    st.close()
