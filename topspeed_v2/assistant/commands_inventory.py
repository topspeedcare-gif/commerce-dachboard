"""
assistant/commands_inventory.py — 재고 관련 명령어

commands_coupang.py와 마찬가지로 shared/db.py만 읽는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant.dispatcher import command
from shared.db import get_conn


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
