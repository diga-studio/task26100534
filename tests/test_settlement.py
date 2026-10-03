"""端到端测试：与独立参考模型对账。"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from pension_settlement import CutoffError, PensionSettlement, StateError
from tests.reference import Reference

Y1, Y2 = 2024, 2025


def make_inputs():
    employees = [("E1", "CN"), ("E2", "CN"), ("E3", "US")]
    wages = []
    for emp, _c in employees:
        for y in (Y1, Y2):
            for m in range(1, 13):
                wages.append((emp, y, m, 10000))
    caps = [("CN", Y1, 50000), ("CN", Y2, 60000),
            ("US", Y1, 40000), ("US", Y2, 45000)]
    return employees, wages, caps


def batch_dict(batch):
    return {(a.emp_id, a.year, a.month): a.delta for a in batch.items}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "settle.db")
        employees, wages, caps = make_inputs()
        self.employees, self.wages, self.caps = employees, wages, caps
        self.init_cutoff = (Y1, 3)
        self.s = PensionSettlement(self.db)
        self.s.initialize(employees, wages, caps, self.init_cutoff)
        self.ref = Reference(employees, wages, caps, self.init_cutoff)

    def tearDown(self):
        try:
            self.s.close()
        except Exception:
            pass

    def reopen(self):
        self.s.close()
        self.s = PensionSettlement(self.db)

    def prepare_commit(self, cutoff):
        """库与参考模型同步 prepare+commit，并核对补差逐月一致。"""
        batch = self.s.prepare(cutoff)
        expected = self.ref.prepare(cutoff)
        self.assertEqual(batch_dict(batch), expected)
        self.s.commit(batch)
        self.ref.commit()
        self.assertEqual(self.s.cutoff(), self.ref.cutoff)
        self.assert_ledger_invariant()
        return batch

    def assert_ledger_invariant(self):
        """截止月以内：初始账面额 + 全部历史补差 == 当前输入算出的月应缴额。"""
        conn = sqlite3.connect(self.db)
        try:
            settled = {}
            for emp, y, m, amt in conn.execute(
                "SELECT emp_id, year, month, amount FROM booked"
            ):
                settled[(emp, y, m)] = amt
            for emp, y, m, d in conn.execute(
                "SELECT emp_id, year, month, delta FROM adjustments"
            ):
                settled[(emp, y, m)] = settled.get((emp, y, m), 0) + d
        finally:
            conn.close()
        cutoff = self.s.cutoff()
        for emp, _c in self.employees:
            for y in (Y1, Y2):
                for m in range(1, 13):
                    if (y, m) > cutoff:
                        continue
                    self.assertEqual(
                        settled.get((emp, y, m), 0),
                        self.ref.due(emp, y, m),
                        f"ledger mismatch at {emp} {y}-{m}",
                    )


class CapMonthShiftTest(Base):
    """反复更正同一员工早期工资：封顶月前移、后移、再恢复。"""

    def test_shift_forward_backward_restore(self):
        # 初始：月缴 1000，5 月封顶（cap 50000）
        self.assertEqual(self.ref.due("E1", Y1, 5), 1000)
        self.assertEqual(self.ref.due("E1", Y1, 6), 0)

        # 前移：1 月工资改为 40000 -> 2 月即封顶
        self.s.amend_wage("E1", Y1, 1, 40000)
        self.ref.amend_wage("E1", Y1, 1, 40000)
        b = self.prepare_commit((Y1, 3))
        d = batch_dict(b)
        self.assertEqual(d[("E1", Y1, 1)], 3000)   # 1000 -> 4000
        self.assertEqual(d.get(("E1", Y1, 2)), None)  # 1000 -> 1000，无补差
        self.assertEqual(d[("E1", Y1, 3)], -1000)  # 1000 -> 0，封顶月前移

        # 后移：1 月工资改为 5000 -> 封顶推迟到 6 月
        self.s.amend_wage("E1", Y1, 1, 5000)
        self.ref.amend_wage("E1", Y1, 1, 5000)
        b = self.prepare_commit((Y1, 3))
        d = batch_dict(b)
        self.assertEqual(d[("E1", Y1, 1)], -3500)  # 4000 -> 500
        self.assertEqual(d.get(("E1", Y1, 2)), None)
        self.assertEqual(d[("E1", Y1, 3)], 1000)   # 0 -> 1000，封顶月后移

        # 恢复：1 月工资改回 10000 -> 补差与首次前移完全对冲
        self.s.amend_wage("E1", Y1, 1, 10000)
        self.ref.amend_wage("E1", Y1, 1, 10000)
        b = self.prepare_commit((Y1, 3))
        d = batch_dict(b)
        self.assertEqual(d[("E1", Y1, 1)], 500)
        self.assertEqual(d.get(("E1", Y1, 3)), None)

        # 年度总额未变但缴费月份改变：同一轮把 2、3 月工资互换式调整
        # 2 月 +5000、3 月 -5000，全年总额不变，但 2 月多缴 3 月少缴
        self.s.amend_wage("E1", Y1, 2, 15000)
        self.s.amend_wage("E1", Y1, 3, 5000)
        self.ref.amend_wage("E1", Y1, 2, 15000)
        self.ref.amend_wage("E1", Y1, 3, 5000)
        b = self.prepare_commit((Y1, 3))
        d = batch_dict(b)
        self.assertEqual(d[("E1", Y1, 2)], 500)   # 正补差
        self.assertEqual(d[("E1", Y1, 3)], -500)  # 负补差
        self.assertEqual(sum(d.values()), 0)      # 年度总额不变

        # 推进到年底并跨年，持续对账
        for m in range(4, 13):
            self.prepare_commit((Y1, m))
        self.prepare_commit((Y2, 1))

    def test_cap_amendment(self):
        # 国别年度封顶额调整影响该国全部员工
        self.s.amend_cap("CN", Y1, 25000)
        self.ref.amend_cap("CN", Y1, 25000)
        b = self.prepare_commit((Y1, 3))
        d = batch_dict(b)
        self.assertNotIn(("E3", Y1, 1), d)  # 他国员工不受影响
        self.assertEqual(d[("E1", Y1, 3)], -500)  # 3 月触及新封顶
        self.assertEqual(d[("E2", Y1, 3)], -500)
        # 一轮多次修改，以最后的值为准
        self.s.amend_cap("CN", Y1, 10000)
        self.s.amend_cap("CN", Y1, 55000)
        self.ref.amend_cap("CN", Y1, 55000)
        self.prepare_commit((Y1, 3))


class ReopenPersistenceTest(Base):
    """修改后与准备后分别关闭重开，再提交并扩展截止月，覆盖跨年及空批次。"""

    def test_reopen_after_amend_and_after_prepare(self):
        other_db = os.path.join(self.dir, "uninterrupted.db")
        t = PensionSettlement(other_db)
        t.initialize(self.employees, self.wages, self.caps, self.init_cutoff)
        try:
            # 修改后关闭重开
            self.s.amend_wage("E1", Y1, 1, 45000)
            t.amend_wage("E1", Y1, 1, 45000)
            self.ref.amend_wage("E1", Y1, 1, 45000)
            self.reopen()
            self.assertEqual(self.s.cutoff(), self.init_cutoff)

            # 准备后关闭重开，再提交
            b1 = self.s.prepare((Y1, 4))
            b2 = t.prepare((Y1, 4))
            self.assertEqual(batch_dict(b1), batch_dict(b2))
            self.assertEqual(batch_dict(b1), self.ref.prepare((Y1, 4)))
            self.reopen()
            pending = self.s.pending_batch()
            self.assertIsNotNone(pending)
            self.assertEqual(batch_dict(pending), batch_dict(b2))
            self.s.commit(pending)
            t.commit(b2)
            self.ref.commit()
            self.assert_ledger_invariant()

            # 继续扩展截止月：跨年 + 空批次
            for m in range(5, 13):
                self.assertEqual(
                    batch_dict(self.s.prepare((Y1, m))),
                    batch_dict(t.prepare((Y1, m))),
                )
                self.s.commit(self.s.pending_batch())
                t.commit(t.pending_batch())
                self.ref.prepare((Y1, m))
                self.ref.commit()
            # 跨年
            b1 = self.s.prepare((Y2, 1))
            b2 = t.prepare((Y2, 1))
            self.assertEqual(batch_dict(b1), batch_dict(b2))
            self.assertEqual(batch_dict(b1), self.ref.prepare((Y2, 1)))
            self.reopen()
            self.s.commit(self.s.pending_batch())
            t.commit(b2)
            self.ref.commit()
            # 空批次：无修改直接准备同截止月
            b1 = self.s.prepare((Y2, 1))
            b2 = t.prepare((Y2, 1))
            self.assertEqual(b1.items, [])
            self.assertEqual(b2.items, [])
            self.s.commit(b1)
            t.commit(b2)
            self.ref.prepare((Y2, 1))
            self.ref.commit()
            self.assert_ledger_invariant()
            # 与不中断执行的参考结果一致：最终账面完全相等
            conn = sqlite3.connect(self.db)
            mine = sorted(conn.execute(
                "SELECT emp_id, year, month, delta FROM adjustments").fetchall())
            conn.close()
            conn = sqlite3.connect(other_db)
            theirs = sorted(conn.execute(
                "SELECT emp_id, year, month, delta FROM adjustments").fetchall())
            conn.close()
            self.assertEqual(mine, theirs)
        finally:
            t.close()


class RuleTest(Base):
    def test_prepare_without_amend_is_empty_and_idempotent(self):
        b = self.s.prepare(self.init_cutoff)
        self.assertEqual(b.items, [])
        self.s.commit(b)  # 空批次也能提交
        b = self.s.prepare(self.init_cutoff)
        self.assertEqual(b.items, [])
        self.s.commit(b)

    def test_cutoff_rules(self):
        with self.assertRaises(CutoffError):
            self.s.prepare((Y1, 5))  # 一次前进两个月
        with self.assertRaises(CutoffError):
            self.s.prepare((Y1, 2))  # 倒退
        # 推进到最后一月后不能继续前进
        s2 = PensionSettlement(os.path.join(self.dir, "full.db"))
        s2.initialize(self.employees, self.wages, self.caps, (Y2, 12))
        with self.assertRaises(CutoffError):
            s2.prepare((Y2 + 1, 1))
        s2.close()

    def test_state_rules(self):
        with self.assertRaises(StateError):
            self.s.commit(None)  # 无待提交批次
        self.s.amend_wage("E1", Y1, 1, 20000)
        self.s.prepare((Y1, 3))
        with self.assertRaises(StateError):
            self.s.amend_wage("E1", Y1, 2, 30000)  # 准备后不能再修改
        with self.assertRaises(StateError):
            self.s.prepare((Y1, 3))  # 一轮只能准备一次
        self.s.commit(self.s.pending_batch())
        self.s.amend_wage("E1", Y1, 2, 30000)  # 提交后可再次修改
        with self.assertRaises(StateError):
            self.s.initialize(self.employees, self.wages, self.caps, (Y1, 1))

    def test_validation(self):
        s2 = PensionSettlement(os.path.join(self.dir, "bad.db"))
        with self.assertRaises(Exception):
            s2.initialize(self.employees, self.wages[:-1], self.caps, (Y1, 1))
        with self.assertRaises(Exception):
            s2.initialize(self.employees, self.wages, self.caps[:-1], (Y1, 1))
        with self.assertRaises(Exception):
            self.s.amend_wage("E1", Y1, 1, -5)
        s2.close()


if __name__ == "__main__":
    unittest.main()
