"""
shared/db.py — 공유 데이터 레이어

역할: 웹 대시보드(정산·재고·광고분석)와 AI 비서봇(Slack)이 함께 읽고 쓰는
단 하나의 SQLite DB. 이 파일 하나만 두 시스템이 공유하고,
나머지 로직은 절대 서로의 코드를 import 하지 않는다.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "topspeed.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_metrics (
    date            TEXT NOT NULL,
    sku             TEXT NOT NULL,
    sales_qty       INTEGER DEFAULT 0,
    revenue         INTEGER DEFAULT 0,
    ad_impressions  INTEGER DEFAULT 0,
    ad_clicks       INTEGER DEFAULT 0,
    ad_spend        INTEGER DEFAULT 0,
    cost_of_goods   INTEGER DEFAULT 0,
    coupang_fee     INTEGER DEFAULT 0,
    net_profit      INTEGER DEFAULT 0,
    roas            REAL DEFAULT 0,
    ctr             REAL DEFAULT 0,
    cvr             REAL DEFAULT 0,
    PRIMARY KEY (date, sku)
);

CREATE TABLE IF NOT EXISTS inventory (
    sku             TEXT PRIMARY KEY,
    product_name    TEXT,
    coupang_qty     INTEGER DEFAULT 0,
    office_qty      INTEGER DEFAULT 0,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    sku             TEXT,
    message         TEXT NOT NULL,
    sent_to_slack   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sku             TEXT NOT NULL,
    hypothesis      TEXT NOT NULL,
    start_date      TEXT NOT NULL,
    review_date     TEXT NOT NULL,
    metric_type     TEXT NOT NULL,
    target_value    REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',
    created_at      TEXT NOT NULL
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def upsert_daily_metric(row: dict) -> None:
    cols = list(row.keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("date", "sku"))
    sql = f"""
        INSERT INTO daily_metrics ({", ".join(cols)}) VALUES ({placeholders})
        ON CONFLICT(date, sku) DO UPDATE SET {updates}
    """
    with get_conn() as conn:
        conn.execute(sql, row)


def upsert_inventory(sku: str, product_name: str, coupang_qty: int, office_qty: int, updated_at: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO inventory (sku, product_name, coupang_qty, office_qty, updated_at)
            VALUES (:sku, :product_name, :coupang_qty, :office_qty, :updated_at)
            ON CONFLICT(sku) DO UPDATE SET
                product_name=excluded.product_name,
                coupang_qty=excluded.coupang_qty,
                office_qty=excluded.office_qty,
                updated_at=excluded.updated_at
            """,
            {
                "sku": sku,
                "product_name": product_name,
                "coupang_qty": coupang_qty,
                "office_qty": office_qty,
                "updated_at": updated_at,
            },
        )


def add_alert(kind: str, message: str, created_at: str, sku: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO alerts (created_at, kind, sku, message) VALUES (?, ?, ?, ?)",
            (created_at, kind, sku, message),
        )


def get_unsent_alerts() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM alerts WHERE sent_to_slack = 0 ORDER BY id"
        ).fetchall()


def mark_alert_sent(alert_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE alerts SET sent_to_slack = 1 WHERE id = ?", (alert_id,))


def get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def add_experiment(
    sku: str,
    hypothesis: str,
    start_date: str,
    review_date: str,
    metric_type: str,
    target_value: float,
    created_at: str,
) -> int:
    """실험을 하나 등록하고 새로 생성된 id를 반환한다. status는 항상 'running'으로 시작."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO experiments
                (sku, hypothesis, start_date, review_date, metric_type, target_value, status, created_at)
            VALUES (:sku, :hypothesis, :start_date, :review_date, :metric_type, :target_value, 'running', :created_at)
            """,
            {
                "sku": sku,
                "hypothesis": hypothesis,
                "start_date": start_date,
                "review_date": review_date,
                "metric_type": metric_type,
                "target_value": target_value,
                "created_at": created_at,
            },
        )
        return int(cur.lastrowid)


def get_due_experiments() -> list[sqlite3.Row]:
    """판정 예정일이 오늘이거나 지났는데 아직 status='running'인 실험 목록."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM experiments "
            "WHERE status = 'running' AND review_date <= date('now', 'localtime') "
            "ORDER BY review_date"
        ).fetchall()


def update_experiment_status(experiment_id: int, status: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE experiments SET status = ? WHERE id = ?",
            (status, experiment_id),
        )


if __name__ == "__main__":
    init_db()
    print(f"DB 초기화 완료: {DB_PATH}")
