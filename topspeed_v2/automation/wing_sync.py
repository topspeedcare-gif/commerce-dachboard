"""
automation/wing_sync.py — 윙 판매자센터의 정확한(취소·반품 반영된) 판매 통계를 가져온다.

배경: 쿠팡 공식 오픈API(로켓그로스 주문 API)는 "결제된" 주문만 알려주고 취소·반품을
반영 안 해서, 취소율이 높은 날은 실제 매출보다 최대 49%까지 부풀려 보이는 문제가
있었다(2026-08-05 확인, dashboard/coupang_client.py의 list_rg_orders_range 주석 참고).
공식 API로는 이 문제를 해결할 방법이 없다 — 로켓그로스 전용 취소/반품 조회 API 자체가
쿠팡 공식 API 카탈로그에 존재하지 않는다.

대신 윙 판매자센터 "비즈니스 인사이트 > 판매분석" 페이지가 내부적으로 호출하는
아래 엔드포인트가 이미 취소·반품까지 반영된 정확한 일별 매출/판매량을 준다
(2026-08-05, 실제 개발자도구 없이 Playwright 네트워크 로그로 발견 — 공식 문서에
없는 비공개 내부 API다):

    GET https://wing.coupang.com/tenants/rfm-ss/api/nudging-card/sale-statistics/data
    응답: {"cardData": [{"date": "YYYY-MM-DD", "gmv": ..., "unitsSold": ...}, ...]}
    (파라미터 없이 항상 최근 ~2주 정도의 롤링 윈도우를 돌려주는 것으로 보임)

이건 공식 API가 아니라 로그인 세션(쿠키)을 재사용하는 방식이라 — 반드시
automation/wing_login.py로 먼저 로그인해서 세션을 저장해둬야 한다. 세션이 만료되면
이 스크립트가 조용히 실패하지 않고 명확히 "다시 로그인하라"고 알려준다.

사용법:
    python automation\\wing_sync.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SESSION_PATH = ROOT / "automation" / ".wing_session.json"
DATA_URL = "https://wing.coupang.com/tenants/rfm-ss/api/nudging-card/sale-statistics/data"
KST = ZoneInfo("Asia/Seoul")


class WingSessionExpired(RuntimeError):
    pass


def _load_cookie_header() -> str:
    if not SESSION_PATH.exists():
        raise WingSessionExpired(
            "윙 로그인 세션이 없습니다. 먼저 'python automation\\wing_login.py'로 로그인해주세요."
        )
    session = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    cookies = session.get("cookies", [])
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies if "coupang.com" in c.get("domain", ""))


def fetch_sale_statistics() -> list[dict]:
    """wing_sales_summary에 upsert할 수 있는 형태({"date", "gmv", "unitsSold"} 리스트)로 반환한다."""
    cookie_header = _load_cookie_header()
    req = urllib.request.Request(
        DATA_URL, headers={"Cookie": cookie_header, "User-Agent": "Mozilla/5.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise WingSessionExpired(
                f"윙 세션이 만료된 것 같습니다(HTTP {exc.code}). "
                "'python automation\\wing_login.py'로 다시 로그인해주세요."
            ) from exc
        raise RuntimeError(f"윙 판매통계 조회 실패: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"윙 판매통계 조회 실패(연결 오류): {exc}") from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        # 세션이 만료되면 JSON 대신 로그인 페이지 HTML이 내려온다.
        raise WingSessionExpired(
            "응답이 JSON이 아닙니다 — 세션이 만료되어 로그인 페이지가 내려온 것으로 보입니다. "
            "'python automation\\wing_login.py'로 다시 로그인해주세요."
        )

    return data.get("cardData") or []


def run_wing_sync() -> str:
    """반환값: 사람이 읽는 결과 요약 문자열 (예외를 던지지 않음)."""
    from shared.db import init_db, upsert_wing_sales_summary

    init_db()
    try:
        rows = fetch_sale_statistics()
    except (WingSessionExpired, RuntimeError) as exc:
        return f"❌ {exc}"

    if not rows:
        return "⚠️ 윙 판매통계 응답이 비어있습니다."

    count = upsert_wing_sales_summary(rows, datetime.now(KST).isoformat())
    dates = sorted(r["date"] for r in rows if r.get("date"))
    date_range = f"{dates[0]} ~ {dates[-1]}" if dates else "?"
    return f"✅ 윙 판매통계 동기화 완료 — {count}일치 저장 ({date_range})"


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(run_wing_sync())
