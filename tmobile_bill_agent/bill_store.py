# SQLite storage for historical T-Mobile bills (one row per bill month, one row per line).
# Developer :  Devraj Gupta
import os
import sqlite3
from contextlib import closing

import pandas as pd

DB_PATH = os.environ.get("BILL_DB_PATH", "bill_history.db")
MIN_MONTHS_FOR_CHART = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS bills (
    month TEXT PRIMARY KEY,              -- 'YYYY-MM'
    total REAL NOT NULL,
    saved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bill_lines (
    month TEXT NOT NULL REFERENCES bills(month) ON DELETE CASCADE,
    phone TEXT NOT NULL,
    line_type TEXT,
    plans REAL, equipment REAL, services REAL, one_time REAL, line_total REAL,
    individual_amount REAL,              -- final per person amount (after adjustments)
    PRIMARY KEY (month, phone)
);
"""


def _connect(path=None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def save_bill(month, total, bill_df, final_df, path=None):
    """Insert or replace the bill for `month`. bill_df = extracted table, final_df = per person amounts."""
    individual = dict(zip(final_df["Person"], final_df["Individual amount"]))
    with closing(_connect(path)) as conn, conn:
        conn.execute("DELETE FROM bills WHERE month = ?", (month,))
        conn.execute("INSERT INTO bills (month, total) VALUES (?, ?)", (month, float(total)))
        conn.executemany(
            "INSERT INTO bill_lines VALUES (?,?,?,?,?,?,?,?,?)",
            [(month, r["Phone Number"], r["Line Type"], r["Plans"], r["Equipment"], r["Services"],
              r["One-time"], r["Total"], individual.get(r["Phone Number"]))
             for _, r in bill_df.iterrows()])


def month_exists(month, path=None):
    with closing(_connect(path)) as conn:
        return conn.execute("SELECT 1 FROM bills WHERE month = ?", (month,)).fetchone() is not None


def list_totals(path=None):
    with closing(_connect(path)) as conn:
        return pd.read_sql_query("SELECT month AS Month, total AS Total FROM bills ORDER BY month", conn)


def load_lines(month, path=None):
    with closing(_connect(path)) as conn:
        return pd.read_sql_query(
            "SELECT phone AS 'Phone Number', line_type AS 'Line Type', plans AS Plans, "
            "equipment AS Equipment, services AS Services, one_time AS 'One-time', "
            "line_total AS Total, individual_amount AS 'Individual amount' "
            "FROM bill_lines WHERE month = ? ORDER BY rowid", conn, params=(month,))


def per_line_history(path=None):
    """Wide table: one row per month, one column per phone, values = line total."""
    with closing(_connect(path)) as conn:
        df = pd.read_sql_query("SELECT month, phone, line_total FROM bill_lines", conn)
    if df.empty:
        return df
    return df.pivot_table(index="month", columns="phone", values="line_total", aggfunc="sum").sort_index()


def delete_month(month, path=None):
    with closing(_connect(path)) as conn, conn:
        conn.execute("DELETE FROM bills WHERE month = ?", (month,))
