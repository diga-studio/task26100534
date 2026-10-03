"""SQLite 持久层：负责所有状态（输入、初始账面、补差、待提交批次）的落盘。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .types import Batch, DeltaEntry, Month

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS employees (
    employee_id TEXT PRIMARY KEY,
    country TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wages (
    employee_id TEXT NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    PRIMARY KEY (employee_id, year, month)
);
CREATE TABLE IF NOT EXISTS caps (
    country TEXT NOT NULL,
    year INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    PRIMARY KEY (country, year)
);
CREATE TABLE IF NOT EXISTS initial_book (
    employee_id TEXT NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    PRIMARY KEY (employee_id, year, month)
);
CREATE TABLE IF NOT EXISTS deltas (
    batch_id INTEGER NOT NULL,
    employee_id TEXT NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    amount INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_batch (
    batch_id INTEGER NOT NULL,
    cutoff_year INTEGER NOT NULL,
    cutoff_month INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_entries (
    batch_id INTEGER NOT NULL,
    employee_id TEXT NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    amount INTEGER NOT NULL
);
"""


class Store:
    """对单个 SQLite 文件的薄封装。方法均为数据读写，不含业务规则。"""

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- meta ----

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ---- 初始化导入 ----

    def is_initialized(self) -> bool:
        return self.get_meta("initialized") == "1"

    def load_initial(
        self,
        employees: dict[str, str],
        wages: dict[str, dict[Month, int]],
        caps: dict[str, dict[int, int]],
        cutoff: Month | None,
        initial_book: dict[tuple[str, int, int], int],
    ) -> None:
        conn = self._conn
        conn.executemany(
            "INSERT INTO employees (employee_id, country) VALUES (?, ?)",
            [(eid, country) for eid, country in employees.items()],
        )
        conn.executemany(
            "INSERT INTO wages (employee_id, year, month, amount) VALUES (?, ?, ?, ?)",
            [
                (eid, year, month, amount)
                for eid, months in wages.items()
                for (year, month), amount in months.items()
            ],
        )
        conn.executemany(
            "INSERT INTO caps (country, year, amount) VALUES (?, ?, ?)",
            [
                (country, year, amount)
                for country, years in caps.items()
                for year, amount in years.items()
            ],
        )
        conn.executemany(
            "INSERT INTO initial_book (employee_id, year, month, amount) VALUES (?, ?, ?, ?)",
            [
                (eid, year, month, amount)
                for (eid, year, month), amount in initial_book.items()
            ],
        )
        self.set_meta("initialized", "1")
        self.set_meta("cutoff", _encode_cutoff(cutoff))
        conn.commit()

    # ---- 读取当前输入 ----

    def all_employees(self) -> dict[str, str]:
        return {
            eid: country
            for eid, country in self._conn.execute(
                "SELECT employee_id, country FROM employees"
            )
        }

    def all_wages(self) -> dict[tuple[str, int, int], int]:
        return {
            (eid, year, month): amount
            for eid, year, month, amount in self._conn.execute(
                "SELECT employee_id, year, month, amount FROM wages"
            )
        }

    def all_caps(self) -> dict[tuple[str, int], int]:
        return {
            (country, year): amount
            for country, year, amount in self._conn.execute(
                "SELECT country, year, amount FROM caps"
            )
        }

    def set_wage(self, employee_id: str, year: int, month: int, amount: int) -> None:
        self._conn.execute(
            "UPDATE wages SET amount = ? WHERE employee_id = ? AND year = ? AND month = ?",
            (amount, employee_id, year, month),
        )
        self._conn.commit()

    def set_cap(self, country: str, year: int, amount: int) -> None:
        self._conn.execute(
            "UPDATE caps SET amount = ? WHERE country = ? AND year = ?",
            (amount, country, year),
        )
        self._conn.commit()

    # ---- 截止月 ----

    def get_cutoff(self) -> Month | None:
        return _decode_cutoff(self.get_meta("cutoff"))

    # ---- 账面（初始账面 + 已提交补差） ----

    def book_amounts(self) -> dict[tuple[str, int, int], int]:
        """每个员工每月的账面额 = 初始账面 + 全部已提交补差之和。"""
        book: dict[tuple[str, int, int], int] = {}
        for eid, year, month, amount in self._conn.execute(
            "SELECT employee_id, year, month, amount FROM initial_book"
        ):
            book[(eid, year, month)] = amount
        for eid, year, month, amount in self._conn.execute(
            "SELECT employee_id, year, month, SUM(amount) FROM deltas"
            " GROUP BY employee_id, year, month"
        ):
            key = (eid, year, month)
            book[key] = book.get(key, 0) + amount
        return book

    # ---- 待提交批次 ----

    def save_pending(self, batch: Batch) -> None:
        conn = self._conn
        conn.execute("DELETE FROM pending_batch")
        conn.execute("DELETE FROM pending_entries")
        conn.execute(
            "INSERT INTO pending_batch (batch_id, cutoff_year, cutoff_month) VALUES (?, ?, ?)",
            (batch.batch_id, batch.cutoff[0], batch.cutoff[1]),
        )
        conn.executemany(
            "INSERT INTO pending_entries (batch_id, employee_id, year, month, amount)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (batch.batch_id, e.employee_id, e.year, e.month, e.amount)
                for e in batch.entries
            ],
        )
        conn.commit()

    def load_pending(self) -> Batch | None:
        row = self._conn.execute(
            "SELECT batch_id, cutoff_year, cutoff_month FROM pending_batch"
        ).fetchone()
        if row is None:
            return None
        batch_id, year, month = row
        entries = tuple(
            DeltaEntry(employee_id=eid, year=y, month=m, amount=amount)
            for eid, y, m, amount in self._conn.execute(
                "SELECT employee_id, year, month, amount FROM pending_entries"
                " WHERE batch_id = ? ORDER BY employee_id, year, month",
                (batch_id,),
            )
        )
        return Batch(batch_id=batch_id, cutoff=(year, month), entries=entries)

    def clear_pending(self) -> None:
        self._conn.execute("DELETE FROM pending_batch")
        self._conn.execute("DELETE FROM pending_entries")

    # ---- 提交 ----

    def next_batch_id(self) -> int:
        value = self.get_meta("next_batch_id")
        return int(value) if value is not None else 1

    def commit_batch(self, batch: Batch) -> None:
        """追加补差明细、推进截止月、清除待提交批次，单事务完成。"""
        conn = self._conn
        conn.executemany(
            "INSERT INTO deltas (batch_id, employee_id, year, month, amount)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (batch.batch_id, e.employee_id, e.year, e.month, e.amount)
                for e in batch.entries
            ],
        )
        self.set_meta("next_batch_id", str(batch.batch_id + 1))
        self.set_meta("cutoff", _encode_cutoff(batch.cutoff))
        self.clear_pending()
        conn.commit()


def _encode_cutoff(cutoff: Month | None) -> str:
    return "none" if cutoff is None else f"{cutoff[0]}-{cutoff[1]}"


def _decode_cutoff(value: str | None) -> Month | None:
    if value is None or value == "none":
        return None
    year, month = value.split("-")
    return (int(year), int(month))
