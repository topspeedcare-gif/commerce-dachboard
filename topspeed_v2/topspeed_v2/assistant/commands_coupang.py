"""
assistant/commands_coupang.py — 쿠팡 관련 명령어 (예시 2개)

중요: 여기서는 shared/db.py만 읽는다. dashboard/ 안의 그 어떤 파일도
import하지 않는다 — 계산 로직은 대시보드 쪽 소유, 여기는 결과를 슬랙 문장으로
바꾸는 역할만 한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant.dispatcher import command
from shared.db import get_conn, get_unsent_alerts, mark_alert_sent


@command("오늘 매출", "쿠팡 오늘 매출", "매출 확인")
def handle_today_revenue(ctx: dict) -> str:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT sku, revenue, net_profit, roas FROM daily_metrics "
            "WHERE date = date('now', 'localtime') ORDER BY revenue DESC"
        ).fetchall()

    if not rows:
        return "📭 오늘 집계된 데이터가 아직 없습니다. (대시보드 동기화가 실행됐는지 확인하세요)"

    total_revenue = sum(r["revenue"] for r in rows)
    total_profit = sum(r["net_profit"] for r in rows)
    lines = [f"📦 오늘 매출 {total_revenue:,}원 · 순이익 {total_profit:,}원"]
    for r in rows[:5]:
        lines.append(f"  · {r['sku']}: {r['revenue']:,}원 (ROAS {r['roas']})")
    return "\n".join(lines)


@command("알람 확인", "미확인 알람", "이슈 확인")
def handle_pending_alerts(ctx: dict) -> str:
    """대시보드가 감지해 둔 알람(광고 이슈·재고 부족)을 슬랙으로 보여주고 발송 처리."""
    alerts = get_unsent_alerts()
    if not alerts:
        return "✅ 새로운 알람이 없습니다."

    lines = [f"🔔 미확인 알람 {len(alerts)}건"]
    for a in alerts:
        lines.append(f"  · {a['message']}")
        mark_alert_sent(a["id"])
    return "\n".join(lines)
