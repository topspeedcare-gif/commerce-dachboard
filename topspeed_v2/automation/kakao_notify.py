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
    get_rocket_growth_low_stock,
    get_wing_sales_summary,
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

    # 매출은 윙 판매자센터 판매분석 기준(취소·반품 반영, wing_sync.py가 로그인 세션으로
    # 가져옴)을 우선 쓴다 — 로켓그로스 주문 API 기반 daily_metrics.revenue는 취소·반품이
    # 안 빠져서 최대 49%까지 부풀려 보이는 걸 확인했다(2026-08-05). 그 세션이 아직 그
    # 날짜를 못 가져왔으면(만료 등) 그 사실을 그대로 알려준다.
    wing_rows = get_wing_sales_summary(target_date, target_date)
    if wing_rows:
        w = wing_rows[0]
        lines.append(f"매출: {w['gmv']:,.0f}원 (취소·반품 반영, 정확 — 판매량 {w['units_sold']:,.0f}개)")
    else:
        lines.append("매출: 정확한 수치 없음 (윙 로그인 세션이 만료됐을 수 있어요 — PC에서 wing_login.py 재실행 필요)")

    if not rows:
        lines.append("(SKU별 광고비·순이익 추정 데이터도 없습니다.)")
    else:
        ad_spend = sum(r["ad_spend"] for r in rows)
        net_profit = sum(r["net_profit"] for r in rows)
        lines += [
            f"광고비: {ad_spend:,}원",
            f"순이익(추정): {net_profit:,}원 — SKU별 원가·수수료 추정치 기준, 위 정확 매출과 정확히 안 맞물릴 수 있음",
        ]

    # 로켓그로스 재고부족은 사무실 재고부족보다 먼저 보여준다 — 주력 판매 채널이라
    # 우선순위가 더 높고, 판매속도 대비 예상 품절일수라 더 실질적인 경고이기도 하다.
    rocket_low_stock = get_rocket_growth_low_stock()
    if rocket_low_stock:
        lines.append("")
        lines.append(f"🚀 로켓그로스 재고 부족 {len(rocket_low_stock)}건 (품절 임박순)")
        for r in rocket_low_stock[:5]:
            name = r["product_name"] or r["sku"]
            lines.append(
                f"  · {name}: {r['coupang_qty']}개 (약 {r['stock_days']}일 후 품절, "
                f"일 평균 {r['daily_velocity']}개 판매)"
            )
        if len(rocket_low_stock) > 5:
            lines.append(f"  · 외 {len(rocket_low_stock) - 5}건 (슬랙 `발주 제안`으로 전체 확인)")

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
