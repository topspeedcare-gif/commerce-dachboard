"""
assistant/commands_inventory.py — 재고 관련 명령어

commands_coupang.py와 마찬가지로 shared/db.py만 읽는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant.dispatcher import command
from shared.db import get_conn, get_setting, LOW_OFFICE_STOCK_FLOOR_DEFAULT


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
