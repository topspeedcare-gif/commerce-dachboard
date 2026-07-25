"""
automation/kakao_notify.py — 카카오톡 "나에게 보내기"로 대시보드 요약 발송

daily_sync.py가 동기화·git push까지 끝낸 뒤 send_daily_summary()를 호출해서,
shared/db.py의 daily_metrics에 쌓인 오늘 매출/광고비/로아스/순이익 요약을
카카오톡 "나에게 보내기"로 보낸다.

access_token은 발급 후 몇 시간이면 만료된다 — 여기서는 매번 보내기 전에
access_token으로 먼저 시도하고, 401(만료)이면 refresh_token으로 자동
갱신한 뒤 한 번 더 시도한다. 갱신된 토큰은 .env에 다시 저장한다.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv, set_key

from shared.db import (
    get_conn,
    get_due_experiments,
    get_setting,
    LOW_OFFICE_STOCK_FLOOR_DEFAULT,
    get_rocket_growth_revenue_estimate,
)

TOKEN_URL = "https://kauth.kakao.com/oauth/token"
MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
ENV_PATH = ROOT / ".env"


class KakaoNotifyError(RuntimeError):
    pass


def _post_form(url: str, headers: dict, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise KakaoNotifyError(f"HTTP {exc.code}: {err_body}") from exc
    except urllib.error.URLError as exc:
        raise KakaoNotifyError(f"카카오 서버 연결 실패: {exc}") from exc


def refresh_access_token() -> str:
    """refresh_token으로 access_token을 새로 받아 .env에 갱신하고 반환한다."""
    load_dotenv(ENV_PATH)
    rest_api_key = os.getenv("KAKAO_REST_API_KEY", "").strip()
    refresh_token = os.getenv("KAKAO_REFRESH_TOKEN", "").strip()
    client_secret = os.getenv("KAKAO_CLIENT_SECRET", "").strip()

    if not rest_api_key or not refresh_token:
        raise KakaoNotifyError("KAKAO_REST_API_KEY / KAKAO_REFRESH_TOKEN이 .env에 없습니다.")

    data = {
        "grant_type": "refresh_token",
        "client_id": rest_api_key,
        "refresh_token": refresh_token,
    }
    if client_secret:
        data["client_secret"] = client_secret

    result = _post_form(
        TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}, data
    )
    access_token = result.get("access_token", "")
    if not access_token:
        raise KakaoNotifyError(f"토큰 갱신 응답에 access_token이 없습니다: {result}")

    set_key(str(ENV_PATH), "KAKAO_ACCESS_TOKEN", access_token)
    # 카카오는 refresh_token도 새 만료기간과 함께 같이 내려줄 때가 있음 — 오면 같이 갱신.
    new_refresh_token = result.get("refresh_token")
    if new_refresh_token:
        set_key(str(ENV_PATH), "KAKAO_REFRESH_TOKEN", new_refresh_token)

    return access_token


def _send_memo(access_token: str, text: str) -> dict:
    template = {"object_type": "text", "text": text, "link": {"web_url": "", "mobile_web_url": ""}}
    data = {"template_object": json.dumps(template, ensure_ascii=False)}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
    }
    return _post_form(MEMO_URL, headers, data)


def send_kakao_message(text: str) -> dict:
    """access_token으로 먼저 보내고, 만료(401)면 refresh_token으로 갱신 후 한 번 더 시도한다."""
    load_dotenv(ENV_PATH)
    access_token = os.getenv("KAKAO_ACCESS_TOKEN", "").strip()
    if not access_token:
        access_token = refresh_access_token()

    try:
        return _send_memo(access_token, text)
    except KakaoNotifyError as exc:
        if "HTTP 401" not in str(exc):
            raise
        access_token = refresh_access_token()
        return _send_memo(access_token, text)


def build_daily_summary(target_date: str | None = None) -> str:
    """
    shared/db.py의 daily_metrics에서 지정한 날짜(기본 오늘) 실적을 집계하고,
    판정 대기 실험·사무실 재고 부족 SKU까지 한 번에 모아 요약 문자열을 만든다.
    """
    target_date = target_date or str(date.today())
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM daily_metrics WHERE date = ?", (target_date,)).fetchall()

    lines = [f"📊 TOPSPEED 대시보드 요약 ({target_date})"]

    if not rows:
        lines.append("집계된 데이터가 없습니다.")
    else:
        revenue = sum(r["revenue"] for r in rows)
        ad_spend = sum(r["ad_spend"] for r in rows)
        net_profit = sum(r["net_profit"] for r in rows)
        roas = round(revenue / ad_spend, 2) if ad_spend else 0.0
        lines += [
            f"매출: {revenue:,}원 (판매자배송 기준 — 로켓그로스는 아래 별도)",
            f"광고비: {ad_spend:,}원",
            f"ROAS: {roas}",
            f"순이익: {net_profit:,}원",
        ]

    rg_estimate = get_rocket_growth_revenue_estimate()
    if rg_estimate["items"]:
        lines.append("")
        lines.append(
            f"🚀 로켓그로스 추정 일평균 매출: 약 {rg_estimate['total_estimated_daily_revenue']:,}원 "
            f"(최근 30일 판매속도 x 판매가 기준 추정치, 정확한 당일 매출 아님)"
        )
        lines.append(f"  · 추정 일평균 판매량: 약 {rg_estimate['total_estimated_daily_qty']}개")

    due_experiments = get_due_experiments()
    if due_experiments:
        lines.append("")
        lines.append(f"🧪 판정 대기 실험 {len(due_experiments)}건")
        for exp in due_experiments[:5]:
            lines.append(f"  · {exp['sku']}: {exp['hypothesis'][:30]}")
        if len(due_experiments) > 5:
            lines.append(f"  · 외 {len(due_experiments) - 5}건 (슬랙 `판정 대기 실험`으로 전체 확인)")

    floor = int(get_setting("low_office_stock_floor", str(LOW_OFFICE_STOCK_FLOOR_DEFAULT)))
    with get_conn() as conn:
        low_stock_rows = conn.execute(
            "SELECT sku, product_name, office_qty FROM inventory WHERE office_qty <= ? ORDER BY office_qty ASC",
            (floor,),
        ).fetchall()
    if low_stock_rows:
        lines.append("")
        lines.append(f"🔴 사무실 재고 부족 {len(low_stock_rows)}건 (기준 {floor}개 이하)")
        for r in low_stock_rows[:5]:
            name = r["product_name"] or r["sku"]
            lines.append(f"  · {name}: {r['office_qty']}개")
        if len(low_stock_rows) > 5:
            lines.append(f"  · 외 {len(low_stock_rows) - 5}건 (슬랙 `입고 필요`로 전체 확인)")

    return "\n".join(lines)


def send_daily_summary(target_date: str | None = None) -> dict:
    text = build_daily_summary(target_date)
    return send_kakao_message(text)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    outcome = send_daily_summary()
    print("카카오톡 발송 결과:", outcome)
