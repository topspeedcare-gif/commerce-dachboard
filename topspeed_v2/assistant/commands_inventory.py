"""
assistant/commands_inventory.py — 재고 관련 명령어

commands_coupang.py와 마찬가지로 shared/db.py만 읽는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant.dispatcher import command
from shared.db import (
    get_conn,
    get_setting,
    LOW_OFFICE_STOCK_FLOOR_DEFAULT,
    get_reorder_suggestions,
    get_channel_inventory,
)


@command("재고 확인", "재고 현황", "재고")
def handle_inventory_check(ctx: dict) -> str:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT sku, product_name, coupang_qty, office_qty FROM inventory "
            "ORDER BY coupang_qty ASC"
        ).fetchall()

    if not rows:
        return "📭 재고 데이터가 없습니다. 대시보드에서 동기화를 먼저 실행하세요."

    lines = ["📦 재고 현황 (쿠팡 재고 적은 순)"]
    for r in rows[:15]:
        name = r["product_name"] or r["sku"]
        total = r["coupang_qty"] + r["office_qty"]
        warn = " ⚠️" if r["coupang_qty"] <= 5 else ""
        lines.append(f"  · {name}: 쿠팡 {r['coupang_qty']} / 사무실 {r['office_qty']} (합 {total}){warn}")

    return "\n".join(lines)


@command("사무실 재고 부족", "입고 필요", "사무실 재고 확인")
def handle_low_office_stock(ctx: dict) -> str:
    """
    사무실 재고가 낮은 임계치(low_office_stock_floor 설정값, 기본 10개) 이하인
    SKU를 바로 조회한다. add_inventory_move()가 이관/출고로 재고가 임계치 아래로
    떨어질 때마다 alerts 테이블에도 low_office_stock 알람을 남기므로,
    '알람 확인' 명령으로도 같은 내용을 확인할 수 있다 — 이 명령어는 그와 별개로
    지금 이 순간의 상태를 바로 조회하는 용도.
    """
    floor = int(get_setting("low_office_stock_floor", str(LOW_OFFICE_STOCK_FLOOR_DEFAULT)))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT sku, product_name, office_qty FROM inventory "
            "WHERE office_qty <= ? ORDER BY office_qty ASC",
            (floor,),
        ).fetchall()

    if not rows:
        return f"✅ 사무실 재고 부족 SKU가 없습니다 (기준 {floor}개 이하)."

    lines = [f"🔴 사무실 재고 부족 {len(rows)}건 (기준 {floor}개 이하) — 입고 필요"]
    for r in rows:
        name = r["product_name"] or r["sku"]
        lines.append(f"  · {name}: 사무실 재고 {r['office_qty']}개")

    return "\n".join(lines)


@command("발주 제안", "품절 예측", "발주 필요")
def handle_reorder_suggestions(ctx: dict) -> str:
    """
    최근 판매속도 기준으로 예상 품절일수가 (발주 리드타임 + 안전재고 7일) 이내인
    SKU에 대해 발주 제안 수량을 보여준다. shared/db.py의 get_reorder_suggestions()가
    dashboard/web_app.py의 발주 제안 표와 완전히 같은 계산 로직을 쓴다.
    """
    suggestions = get_reorder_suggestions()

    if not suggestions:
        return "✅ 지금 발주가 필요한 SKU가 없습니다."

    first = suggestions[0]
    lines = [
        f"🚨 발주 제안 {len(suggestions)}건 "
        f"(리드타임 {first['lead_time_days']}일 + 안전재고 {first['safety_days']}일 기준)"
    ]
    for s in suggestions:
        name = s["product_name"] or s["sku"]
        lines.append(
            f"  · {name}: 재고 {s['total_qty']}개 · 일 평균 {s['daily_velocity']}개 판매 "
            f"· 예상 품절 {s['stock_days']}일 후 → {s['suggested_order_qty']}개 발주 제안"
        )

    return "\n".join(lines)


@command("통합 재고", "윙 그로스 재고", "재고 비교")
def handle_channel_inventory(ctx: dict) -> str:
    """
    상품명 기준으로 윙(판매자배송) 재고와 로켓그로스 재고를 나란히 보여준다.
    대시보드 "통합 재고 동기화" 버튼(또는 python dashboard/channel_inventory_sync.py)을
    먼저 실행해야 데이터가 쌓인다 — 상품 옵션마다 API를 개별 조회해야 해서
    실행에 시간이 좀 걸려(약 40초) 자동 동기화엔 안 들어있다.
    """
    rows = get_channel_inventory()
    if not rows:
        return "📭 통합 재고 데이터가 없습니다. 대시보드에서 '통합 재고 동기화'를 먼저 실행하세요."

    lines = [f"📦 통합 재고 현황 (윙 x 로켓그로스, {len(rows)}개 상품)"]
    for r in rows[:15]:
        wing = r["wing_qty"] if r["wing_qty"] is not None else "-"
        rocket = r["rocket_qty"] if r["rocket_qty"] is not None else "-"
        lines.append(f"  · {r['product_name']}: 윙 {wing} / 그로스 {rocket}")
    if len(rows) > 15:
        lines.append(f"  · 외 {len(rows) - 15}건 (대시보드에서 전체 확인)")

    return "\n".join(lines)
