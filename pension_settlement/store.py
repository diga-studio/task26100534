"""SQLite 持久化层：schema 与连接管理。"""
from __future__ import annotations

import sqlite3

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employees (
    emp_id  TEXT PRIMARY KEY,
    country TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wages (
    emp_id TEXT NOT NULL,
    year   INTEGER NOT NULL,
    month  INTEGER NOT NULL,
    amount INTEGER NOT NULL CHECK (amount >= 0),
    PRIMARY KEY (emp_id, year, month)
);

CREATE TABLE IF NOT EXISTS caps (
    country TEXT NOT NULL,
    year    INTEGER NOT NULL,
    cap     INTEGER NOT NULL CHECK (cap >= 0),
    PRIMARY KEY (country, year)
);

-- 初始结算时已按初值结清的账面额（initialize 时写入，之后不变）
CREATE TABLE IF NOT EXISTS booked (
    emp_id TEXT NOT NULL,
    year   INTEGER NOT NULL,
    month  INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    PRIMARY KEY (emp_id, year, month)
);

-- 已提交批次
CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cutoff_year  INTEGER NOT NULL,
    cutoff_month INTEGER NOT NULL
);

-- 补差明细（只追加，不修改）
CREATE TABLE IF NOT EXISTS adjustments (
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    emp_id   TEXT NOT NULL,
    year     INTEGER NOT NULL,
    month    INTEGER NOT NULL,
    delta    INTEGER NOT NULL
);

-- 已 prepare 未 commit 的结果（单行）
CREATE TABLE IF NOT EXISTS prepared (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    batch_id     INTEGER NOT NULL,
    cutoff_year  INTEGER NOT NULL,
    cutoff_month INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS prepared_items (
    emp_id TEXT NOT NULL,
    year   INTEGER NOT NULL,
    month  INTEGER NOT NULL,
    delta  INTEGER NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """打开（必要时创建）数据库并初始化 schema。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
