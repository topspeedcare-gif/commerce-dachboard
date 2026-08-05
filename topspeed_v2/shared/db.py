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
    sku                 TEXT PRIMARY KEY,
    product_name        TEXT,
    coupang_qty         INTEGER DEFAULT 0,
    office_qty          INTEGER DEFAULT 0,
    updated_at          TEXT,
    sales_velocity_30d  REAL DEFAULT 0,
    unit_price          INTEGER DEFAULT 0
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

CREATE TABLE IF NOT EXISTS inventory_moves (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sku             TEXT NOT NULL,
    move_type       TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    channel         TEXT,
    note            TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settlements (
    order_id            TEXT NOT NULL,
    sku                 TEXT NOT NULL,
    sale_date           TEXT NOT NULL,
    recognition_date    TEXT NOT NULL,
    settlement_date     TEXT,
    sale_type           TEXT,
    product_name        TEXT,
    quantity            REAL DEFAULT 0,
    sale_amount         REAL DEFAULT 0,
    fees                REAL DEFAULT 0,
    settlement_amount   REAL DEFAULT 0,
    synced_at           TEXT NOT NULL,
    PRIMARY KEY (order_id, sku)
);

CREATE TABLE IF NOT EXISTS channel_inventory (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    product_name            TEXT NOT NULL,
    wing_qty                INTEGER,
    rocket_qty              INTEGER,
    rocket_sales_last_30d   INTEGER,
    synced_at               TEXT NOT NULL
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
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """
    SCHEMA의 CREATE TABLE IF NOT EXISTS는 이미 존재하는 테이블에 새 컬럼을
    추가해주지 않는다 — 여기서 없는 컬럼만 골라 ALTER TABLE로 보강한다.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(inventory)").fetchall()}
    if "sales_velocity_30d" not in cols:
        conn.execute("ALTER TABLE inventory ADD COLUMN sales_velocity_30d REAL DEFAULT 0")
    if "unit_price" not in cols:
        conn.execute("ALTER TABLE inventory ADD COLUMN unit_price INTEGER DEFAULT 0")

    dm_cols = {row["name"] for row in conn.execute("PRAGMA table_info(daily_metrics)").fetchall()}
    if "channel" not in dm_cols:
        # 'wing'(판매자배송) | 'rocket'(로켓그로스) | 'unknown' — 매출인식(revenue-history)
        # 기준으로 채워지며, 주문(ordersheets) API는 로켓그로스가 전혀 안 잡혀서
        # channel 구분 없이는 두 채널 매출이 뒤섞여 보였다.
        conn.execute("ALTER TABLE daily_metrics ADD COLUMN channel TEXT DEFAULT 'unknown'")


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


def upsert_inventory(
    sku: str,
    product_name: str,
    coupang_qty: int,
    office_qty: int,
    updated_at: str,
    sales_velocity_30d: float | None = None,
    unit_price: int | None = None,
) -> None:
    """
    sales_velocity_30d/unit_price를 안 주면(None) 기존 값을 그대로 유지한다 —
    예를 들어 대시보드의 "재고 수동 조정" 폼은 이 값들을 모르므로, 안 건드리고
    싶을 때 그냥 생략하면 된다.
    """
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO inventory
                (sku, product_name, coupang_qty, office_qty, updated_at, sales_velocity_30d, unit_price)
            VALUES
                (:sku, :product_name, :coupang_qty, :office_qty, :updated_at,
                 COALESCE(:sales_velocity_30d, 0), COALESCE(:unit_price, 0))
            ON CONFLICT(sku) DO UPDATE SET
                product_name=excluded.product_name,
                coupang_qty=excluded.coupang_qty,
                office_qty=excluded.office_qty,
                updated_at=excluded.updated_at,
                sales_velocity_30d=COALESCE(:sales_velocity_30d, inventory.sales_velocity_30d),
                unit_price=COALESCE(:unit_price, inventory.unit_price)
            """,
            {
                "sku": sku,
                "product_name": product_name,
                "coupang_qty": coupang_qty,
                "office_qty": office_qty,
                "updated_at": updated_at,
                "sales_velocity_30d": sales_velocity_30d,
                "unit_price": unit_price,
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


class InsufficientStockError(Exception):
    """사무실 재고가 부족해서 이관/출고 이동을 처리할 수 없을 때."""


_INVENTORY_MOVE_TYPES = {"inbound", "transfer_to_coupang", "outbound_other", "adjustment"}
LOW_OFFICE_STOCK_FLOOR_DEFAULT = 10


def add_inventory_move(
    sku: str,
    move_type: str,
    quantity: int,
    created_at: str,
    channel: str | None = None,
    note: str | None = None,
) -> None:
    """
    재고 이동 1건을 기록하고, move_type에 따라 inventory 테이블의
    office_qty/coupang_qty를 함께 갱신한다.

      - inbound:              office_qty += quantity
      - transfer_to_coupang:  office_qty -= quantity, coupang_qty += quantity
      - outbound_other:       office_qty -= quantity  (channel에 출고 채널 기록)
      - adjustment:           office_qty += quantity  (음수 가능 — 실사 보정용)

    transfer_to_coupang / outbound_other는 office_qty가 quantity보다 적으면
    InsufficientStockError를 던지고 아무것도 반영하지 않는다.
    office_qty가 낮은 재고 임계치(low_office_stock_floor 설정값, 기본 10) 이하로
    떨어지면 'low_office_stock' 알람을 함께 남긴다.
    """
    if move_type not in _INVENTORY_MOVE_TYPES:
        raise ValueError(f"알 수 없는 move_type: {move_type!r} (허용: {sorted(_INVENTORY_MOVE_TYPES)})")

    with get_conn() as conn:
        row = conn.execute(
            "SELECT product_name, coupang_qty, office_qty FROM inventory WHERE sku = ?", (sku,)
        ).fetchone()
        product_name = row["product_name"] if row else ""
        coupang_qty = row["coupang_qty"] if row else 0
        office_qty = row["office_qty"] if row else 0

        if move_type == "inbound":
            office_qty += quantity
        elif move_type == "transfer_to_coupang":
            if office_qty < quantity:
                raise InsufficientStockError(
                    f"{sku} 사무실 재고 부족: 현재 {office_qty}개, 쿠팡 이관 요청 {quantity}개"
                )
            office_qty -= quantity
            coupang_qty += quantity
        elif move_type == "outbound_other":
            if office_qty < quantity:
                raise InsufficientStockError(
                    f"{sku} 사무실 재고 부족: 현재 {office_qty}개, 출고 요청 {quantity}개"
                )
            office_qty -= quantity
        elif move_type == "adjustment":
            office_qty += quantity  # quantity는 음수도 허용 (실사 보정)

        conn.execute(
            """
            INSERT INTO inventory (sku, product_name, coupang_qty, office_qty, updated_at)
            VALUES (:sku, :product_name, :coupang_qty, :office_qty, :updated_at)
            ON CONFLICT(sku) DO UPDATE SET
                coupang_qty=excluded.coupang_qty,
                office_qty=excluded.office_qty,
                updated_at=excluded.updated_at
            """,
            {
                "sku": sku,
                "product_name": product_name,
                "coupang_qty": coupang_qty,
                "office_qty": office_qty,
                "updated_at": created_at,
            },
        )

        conn.execute(
            "INSERT INTO inventory_moves (sku, move_type, quantity, channel, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sku, move_type, quantity, channel, note, created_at),
        )

        if move_type in {"transfer_to_coupang", "outbound_other"}:
            floor_setting = conn.execute(
                "SELECT value FROM settings WHERE key = 'low_office_stock_floor'"
            ).fetchone()
            floor = int(floor_setting["value"]) if floor_setting else LOW_OFFICE_STOCK_FLOOR_DEFAULT
            if office_qty <= floor:
                conn.execute(
                    "INSERT INTO alerts (created_at, kind, sku, message) VALUES (?, ?, ?, ?)",
                    (
                        created_at,
                        "low_office_stock",
                        sku,
                        f"🔴 {product_name or sku} 사무실 재고 {office_qty}개 — 입고 필요",
                    ),
                )


def get_inventory_moves(sku: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
    """재고 이동 이력 조회. sku를 안 주면 전체 SKU의 최근 이동 이력."""
    with get_conn() as conn:
        if sku:
            return conn.execute(
                "SELECT * FROM inventory_moves WHERE sku = ? ORDER BY id DESC LIMIT ?",
                (sku, limit),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM inventory_moves ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


REORDER_LEAD_TIME_DAYS_DEFAULT = 14
REORDER_SAFETY_DAYS_DEFAULT = 7
ROCKET_STOCK_ALERT_DAYS_DEFAULT = 7


def get_rocket_growth_low_stock(days_threshold: int | None = None) -> list[dict]:
    """
    로켓그로스 재고(coupang_qty)만을 기준으로, 판매속도(sales_velocity_30d) 대비
    예상 품절일수가 days_threshold 이하인 SKU를 뽑는다 — 사무실 재고(office_qty)는
    별개로 관리되는 값이라 여기 포함하지 않는다.

    days_threshold를 안 주면 settings의 'rocket_stock_alert_days'(기본 7일) 사용.
    판매 이력이 없는(velocity=0) SKU는 품절일수를 계산할 수 없으므로 제외한다 —
    그런 SKU는 sync_job.py가 고정 수량 기준으로 별도 처리한다.

    product_name이 없는 SKU도 제외한다 — 상품 상세 조회로 이름이 안 붙었다는
    건 현재 판매 중인 상품 목록에서 빠졌다는 뜻이라(판매중단), 실측 결과
    재고 0·판매속도 0에 가까운 죽은 재고가 대부분이었다. 이런 것들이 "품절
    임박" 알람 맨 위를 채워서 진짜 신경 써야 할 상품이 묻히는 문제가 있었다.
    """
    if days_threshold is None:
        days_threshold = int(get_setting("rocket_stock_alert_days", str(ROCKET_STOCK_ALERT_DAYS_DEFAULT)))

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT sku, product_name, coupang_qty, sales_velocity_30d FROM inventory "
            "WHERE sales_velocity_30d > 0 AND product_name IS NOT NULL AND product_name != ''"
        ).fetchall()

    results = []
    for r in rows:
        stock_days = round(r["coupang_qty"] / r["sales_velocity_30d"], 1)
        if stock_days <= days_threshold:
            results.append({
                "sku": r["sku"],
                "product_name": r["product_name"],
                "coupang_qty": r["coupang_qty"],
                "daily_velocity": round(r["sales_velocity_30d"], 2),
                "stock_days": stock_days,
            })

    results.sort(key=lambda x: x["stock_days"])
    return results


def get_stock_predictions(velocity_days: int = 14) -> list[dict]:
    """
    SKU별 일일 판매속도와 현재 재고(쿠팡+사무실 합계)로 예상 품절일수(stock_days)를
    계산한다. 판매 이력이 없는 SKU는 stock_days=None (예측 불가 — 무한대로
    취급하지 않고 "판단할 데이터가 없다"는 뜻으로 명확히 구분한다).

    판매속도 우선순위:
      1) inventory.sales_velocity_30d — 로켓그로스 전용 API(salesCountMap)에서
         나온 실제 30일 판매량/30. 로켓그로스 상품은 주문서(ordersheets) API에
         거의 안 잡혀서 daily_metrics만으로는 판매속도를 알 수 없기 때문에,
         이 값이 있으면(>0) 이걸 우선 쓴다.
      2) 없으면(0) daily_metrics에서 최근 velocity_days일 평균 판매량 —
         판매자배송 SKU는 이 경로로 계산된다.

    반환: stock_days가 가장 임박한(작은) 순으로 정렬된 리스트.
    각 항목: sku, product_name, total_qty, daily_velocity, stock_days
    """
    with get_conn() as conn:
        velocity_rows = conn.execute(
            "SELECT sku, AVG(sales_qty) AS daily_velocity FROM daily_metrics "
            "WHERE date >= date('now', ?, 'localtime') GROUP BY sku",
            (f"-{int(velocity_days)} days",),
        ).fetchall()
        inventory_rows = conn.execute(
            "SELECT sku, product_name, coupang_qty, office_qty, sales_velocity_30d FROM inventory"
        ).fetchall()

    order_velocity_by_sku = {r["sku"]: (r["daily_velocity"] or 0.0) for r in velocity_rows}

    results = []
    for inv in inventory_rows:
        sku = inv["sku"]
        total_qty = inv["coupang_qty"] + inv["office_qty"]
        rocket_velocity = inv["sales_velocity_30d"] or 0.0
        velocity = rocket_velocity if rocket_velocity > 0 else order_velocity_by_sku.get(sku, 0.0)
        stock_days = round(total_qty / velocity, 1) if velocity > 0 else None
        results.append({
            "sku": sku,
            "product_name": inv["product_name"],
            "total_qty": total_qty,
            "daily_velocity": round(velocity, 2),
            "stock_days": stock_days,
        })

    results.sort(key=lambda r: (r["stock_days"] is None, r["stock_days"] if r["stock_days"] is not None else 0))
    return results


def get_reorder_suggestions(
    lead_time_days: int | None = None,
    safety_days: int = REORDER_SAFETY_DAYS_DEFAULT,
    velocity_days: int = 14,
) -> list[dict]:
    """
    예상 품절일수가 (발주 리드타임 + 안전재고일수) 이내로 임박한 SKU에 대해
    발주 제안 수량을 계산한다. lead_time_days를 안 주면 settings의
    'reorder_lead_time_days'(기본 14일)를 사용한다.

    suggested_order_qty = round(daily_velocity * (lead_time_days + safety_days)) - total_qty
    (이 개수만큼 발주하면 리드타임 동안 판매속도를 버틸 수 있는 재고 + 안전 여유분이 확보됨)
    """
    if lead_time_days is None:
        lead_time_days = int(get_setting("reorder_lead_time_days", str(REORDER_LEAD_TIME_DAYS_DEFAULT)))

    threshold_days = lead_time_days + safety_days
    suggestions = []
    for pred in get_stock_predictions(velocity_days=velocity_days):
        if pred["daily_velocity"] <= 0 or pred["stock_days"] is None:
            continue
        if pred["stock_days"] > threshold_days:
            continue
        target_qty = round(pred["daily_velocity"] * threshold_days)
        suggested_qty = max(0, target_qty - pred["total_qty"])
        if suggested_qty <= 0:
            continue
        suggestions.append({
            **pred,
            "lead_time_days": lead_time_days,
            "safety_days": safety_days,
            "suggested_order_qty": suggested_qty,
        })
    return suggestions


def get_rocket_growth_revenue_estimate() -> dict:
    """
    inventory.sales_velocity_30d(최근 30일 평균 일일 판매량) x unit_price로
    SKU별 "추정 일평균 매출"을 계산한다.

    로켓그로스 매출은 쿠팡 공식 API로 정확한 "어제 매출"을 못 받아온다
    (주문서 API엔 거의 안 잡히고, 매출인식 API는 확정까지 ~9일 지연됨 —
    2026-07-25 실측 확인). 이건 그 공백을 메우는 대체 추정치이므로,
    호출하는 쪽(카톡 요약/대시보드)에서 반드시 "추정치"라고 표시해야 한다.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT sku, product_name, sales_velocity_30d, unit_price FROM inventory "
            "WHERE sales_velocity_30d > 0 AND unit_price > 0"
        ).fetchall()

    items = []
    total_revenue = 0.0
    total_qty = 0.0
    for r in rows:
        est_revenue = r["sales_velocity_30d"] * r["unit_price"]
        total_revenue += est_revenue
        total_qty += r["sales_velocity_30d"]
        items.append({
            "sku": r["sku"],
            "product_name": r["product_name"],
            "daily_velocity": round(r["sales_velocity_30d"], 2),
            "unit_price": r["unit_price"],
            "estimated_daily_revenue": round(est_revenue),
        })
    items.sort(key=lambda x: x["estimated_daily_revenue"], reverse=True)

    return {
        "total_estimated_daily_revenue": round(total_revenue),
        "total_estimated_daily_qty": round(total_qty, 1),
        "items": items,
    }


def upsert_settlements(raw_rows: list[dict], synced_at: str) -> int:
    """
    coupang_client.list_revenue_history_range()가 돌려주는 원본 레코드
    (주문 1건 = items 여러 개)를 SKU 단위로 펼쳐서 저장한다.
    (order_id, sku)가 기본키라 같은 주문을 여러 번 동기화해도 중복 없이 갱신된다.

    saleType이 REFUND면 금액 부호를 뒤집어 저장한다 — 그래야 날짜별로
    그냥 합산만 해도 "환불 반영된 순매출"이 나온다.
    """
    count = 0
    with get_conn() as conn:
        for row in raw_rows:
            order_id = str(row.get("orderId") or "")
            if not order_id:
                continue
            sign = -1 if str(row.get("saleType", "SALE")).upper() == "REFUND" else 1
            for item in row.get("items") or []:
                sku = str(item.get("vendorItemId") or "")
                if not sku:
                    continue
                conn.execute(
                    """
                    INSERT INTO settlements
                        (order_id, sku, sale_date, recognition_date, settlement_date,
                         sale_type, product_name, quantity, sale_amount, fees,
                         settlement_amount, synced_at)
                    VALUES
                        (:order_id, :sku, :sale_date, :recognition_date, :settlement_date,
                         :sale_type, :product_name, :quantity, :sale_amount, :fees,
                         :settlement_amount, :synced_at)
                    ON CONFLICT(order_id, sku) DO UPDATE SET
                        sale_date=excluded.sale_date,
                        recognition_date=excluded.recognition_date,
                        settlement_date=excluded.settlement_date,
                        sale_type=excluded.sale_type,
                        product_name=excluded.product_name,
                        quantity=excluded.quantity,
                        sale_amount=excluded.sale_amount,
                        fees=excluded.fees,
                        settlement_amount=excluded.settlement_amount,
                        synced_at=excluded.synced_at
                    """,
                    {
                        "order_id": order_id,
                        "sku": sku,
                        "sale_date": row.get("saleDate", ""),
                        "recognition_date": row.get("recognitionDate", ""),
                        "settlement_date": row.get("settlementDate"),
                        "sale_type": row.get("saleType", "SALE"),
                        "product_name": item.get("vendorItemName") or item.get("productName") or "",
                        "quantity": (item.get("quantity") or 0) * sign,
                        "sale_amount": (item.get("saleAmount") or 0) * sign,
                        "fees": ((item.get("serviceFee") or 0) + (item.get("serviceFeeVat") or 0)) * sign,
                        "settlement_amount": (item.get("settlementAmount") or 0) * sign,
                        "synced_at": synced_at,
                    },
                )
                count += 1
    return count


def get_settlement_summary(date_from: str, date_to: str) -> list[dict]:
    """
    saleDate 기준으로 날짜별 정산 현황을 집계한다.
    각 항목: sale_date, sale_amount, fees, settlement_amount, order_count
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                sale_date,
                SUM(sale_amount) AS sale_amount,
                SUM(fees) AS fees,
                SUM(settlement_amount) AS settlement_amount,
                COUNT(DISTINCT order_id) AS order_count
            FROM settlements
            WHERE sale_date BETWEEN ? AND ?
            GROUP BY sale_date
            ORDER BY sale_date
            """,
            (date_from, date_to),
        ).fetchall()
    return [dict(r) for r in rows]


def get_settlement_vs_expected(date_from: str, date_to: str) -> list[dict]:
    """
    날짜별로 daily_metrics(시스템이 계산한 예상 매출)와 settlements(쿠팡이
    실제로 확정한 매출)를 나란히 비교한다.

    주의: daily_metrics는 판매자배송 위주(주문서 API 기반)라 로켓그로스가
    거의 안 잡히고, settlements는 판매유형 구분 없이 전부 잡힌다 — 그래서
    settlement_amount가 daily_metrics.revenue보다 훨씬 큰 게 정상이다.
    이 비교는 "완전히 같아야 한다"가 아니라 "시스템이 놓치고 있는 매출이
    얼마나 되는지" 감을 잡기 위한 것이다.
    """
    with get_conn() as conn:
        expected_rows = conn.execute(
            "SELECT date, SUM(revenue) AS expected_revenue, SUM(net_profit) AS expected_net_profit "
            "FROM daily_metrics WHERE date BETWEEN ? AND ? GROUP BY date",
            (date_from, date_to),
        ).fetchall()
    expected_by_date = {r["date"]: dict(r) for r in expected_rows}

    actual_rows = get_settlement_summary(date_from, date_to)
    results = []
    for row in actual_rows:
        d = row["sale_date"]
        expected = expected_by_date.get(d, {})
        results.append({
            "date": d,
            "expected_revenue": expected.get("expected_revenue", 0) or 0,
            "actual_sale_amount": round(row["sale_amount"] or 0),
            "actual_settlement_amount": round(row["settlement_amount"] or 0),
            "order_count": row["order_count"],
        })
    results.sort(key=lambda r: r["date"], reverse=True)
    return results


def save_channel_inventory(rows: list[dict], synced_at: str) -> int:
    """
    coupang_client.get_full_inventory_report()가 돌려주는 상품별
    (윙 재고, 로켓그로스 재고) 리스트를 통째로 저장한다. 안정적인 자연키가
    없어서(같은 상품명이라도 옵션마다 새 행) 매번 전체를 지우고 새로
    채우는 "최신 스냅샷" 방식으로 저장한다.
    """
    with get_conn() as conn:
        conn.execute("DELETE FROM channel_inventory")
        for r in rows:
            conn.execute(
                """
                INSERT INTO channel_inventory
                    (product_name, wing_qty, rocket_qty, rocket_sales_last_30d, synced_at)
                VALUES (:product_name, :wing_qty, :rocket_qty, :rocket_sales_last_30d, :synced_at)
                """,
                {
                    "product_name": r.get("product_name", ""),
                    "wing_qty": r.get("wing_qty"),
                    "rocket_qty": r.get("rocket_qty"),
                    "rocket_sales_last_30d": r.get("rocket_sales_last_30d"),
                    "synced_at": synced_at,
                },
            )
    return len(rows)


def get_channel_inventory() -> list[dict]:
    """가장 최근에 저장된 (윙 재고, 로켓그로스 재고) 스냅샷을 상품명 순으로 반환."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT product_name, wing_qty, rocket_qty, rocket_sales_last_30d, synced_at "
            "FROM channel_inventory ORDER BY product_name"
        ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print(f"DB 초기화 완료: {DB_PATH}")
