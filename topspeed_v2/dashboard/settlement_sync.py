"""
dashboard/settlement_sync.py — 정산(매출인식) 동기화

sync_job.py의 매일 동기화(target_date 하루치)와는 성격이 달라서 별도 파일로 뺐다:
  - 매출인식은 recognitionDate 기준 최근 25일치를 통째로 긁어와야 의미가 있고
    (판매일보다 ~9일 늦게 확정되므로 하루치만 봐서는 거의 항상 비어 있다),
    쿠팡 쪽 조회 기간 제약(1개월 미만)도 있어서 매일 실행할 필요는 없다.
  - assistant/는 이 파일을 절대 import하지 않는다 — shared/db.py에 쌓인
    결과만 읽는다 (dashboard/assistant 분리 규칙 동일 적용).

사용법:
    python dashboard/settlement_sync.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard.coupang_client import CoupangClient, CoupangClientUnavailable
from shared.db import init_db, upsert_settlements

KST = ZoneInfo("Asia/Seoul")


def run_settlement_sync(
    days_back: int = 25,
    access_key: str | None = None,
    secret_key: str | None = None,
    vendor_id: str | None = None,
) -> str:
    """
    최근 days_back일치 매출인식 내역을 수집해 settlements 테이블에 저장한다.
    반환값: 사람이 읽는 결과 요약 문자열 (예외를 던지지 않음).
    """
    init_db()

    access_key = access_key or os.getenv("COUPANG_ACCESS_KEY", "")
    secret_key = secret_key or os.getenv("COUPANG_SECRET_KEY", "")
    vendor_id = vendor_id or os.getenv("COUPANG_VENDOR_ID", "")

    try:
        client = CoupangClient(access_key, secret_key, vendor_id)
    except CoupangClientUnavailable as exc:
        return f"❌ 쿠팡 오픈API 미연결: {exc}"

    rows, errors = client.list_revenue_history_range(days_back=days_back)
    if errors:
        return "\n".join(f"⚠️ {e}" for e in errors)

    count = upsert_settlements(rows, datetime.now(KST).isoformat())
    return f"✅ 정산 동기화 완료 — 주문 {len(rows)}건, SKU 레코드 {count}건 저장"


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:
        pass

    print(run_settlement_sync())
