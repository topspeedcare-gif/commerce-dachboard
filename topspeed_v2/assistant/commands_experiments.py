"""
assistant/commands_experiments.py — 실험 판정 관련 명령어

commands_coupang.py / commands_inventory.py와 마찬가지로 shared/db.py만 읽는다.
dashboard/ 안의 그 어떤 파일도 import하지 않는다 — 지표 비교 계산은
web_app.py의 실험 관리 탭과 별개로 여기서 다시 직접 계산한다.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant.dispatcher import command
from shared.db import get_due_experiments, get_conn

# 비율 지표는 기간 평균, 누적 지표는 기간 합산 (web_app.py 실험 관리 탭과 동일한 규칙)
_RATE_METRICS = {"roas", "ctr", "cvr"}
_METRIC_COLUMNS = {"roas", "ctr", "cvr", "revenue", "sales_qty", "net_profit", "ad_spend"}


def _actual_metric_value(sku: str, start_date: str, metric_type: str) -> float | None:
    if metric_type not in _METRIC_COLUMNS:
        return None
    agg = "AVG" if metric_type in _RATE_METRICS else "SUM"
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {agg}({metric_type}) AS v FROM daily_metrics "
            "WHERE sku = ? AND date >= ? AND date <= date('now', 'localtime')",
            (sku, start_date),
        ).fetchone()
    return row["v"] if row and row["v"] is not None else None


@command("판정 대기 실험", "실험 판정", "실험 판정 대기")
def handle_due_experiments(ctx: dict) -> str:
    due = get_due_experiments()
    if not due:
        return "✅ 판정 대기 중인 실험이 없습니다."

    today = date.today()
    lines = [f"🧪 판정 대기 실험 {len(due)}건"]
    for exp in due:
        elapsed = (today - date.fromisoformat(exp["start_date"])).days
        actual = _actual_metric_value(exp["sku"], exp["start_date"], exp["metric_type"])
        metric_label = exp["metric_type"].upper()
        target = exp["target_value"]

        if actual is None:
            result = "실적 데이터 없음"
        else:
            verdict = "달성" if actual >= target else "미달"
            result = f"{metric_label} {actual:.2f} (목표 {target} {verdict})"

        lines.append(f"  · {exp['sku']}, {elapsed}일 경과, {result} — 판정 필요")
        lines.append(f"    가설: {exp['hypothesis']}")

    return "\n".join(lines)
