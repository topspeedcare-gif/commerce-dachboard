"""
dashboard/channel_inventory_sync.py — 윙(판매자배송) x 로켓그로스 통합 재고 동기화

상품 상세를 하나하나 조회하면서 옵션별 윙 재고까지 개별 API 호출로 확인해야 해서
(상품 59개 기준 약 40초) 매일 도는 sync_job.py에는 안 넣고, 필요할 때 따로
실행하는 함수로 뺐다.

사용법:
    python dashboard/channel_inventory_sync.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard.coupang_client import CoupangClient, CoupangClientUnavailable
from shared.db import init_db, save_channel_inventory

KST = ZoneInfo("Asia/Seoul")


def run_channel_inventory_sync(
    access_key: str | None = None,
    secret_key: str | None = None,
    vendor_id: str | None = None,
) -> str:
    """상품명 기준 윙 재고 x 로켓그로스 재고 통합 리포트를 가져와 저장한다."""
    init_db()

    access_key = access_key or os.getenv("COUPANG_ACCESS_KEY", "")
    secret_key = secret_key or os.getenv("COUPANG_SECRET_KEY", "")
    vendor_id = vendor_id or os.getenv("COUPANG_VENDOR_ID", "")

    try:
        client = CoupangClient(access_key, secret_key, vendor_id)
    except CoupangClientUnavailable as exc:
        return f"❌ 쿠팡 오픈API 미연결: {exc}"

    try:
        report = client.get_full_inventory_report()
    except CoupangClientUnavailable as exc:
        return f"❌ 통합 재고 조회 실패: {exc}"

    count = save_channel_inventory(report, datetime.now(KST).isoformat())
    return f"✅ 통합 재고 동기화 완료 — {count}개 상품(옵션) 저장"


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

    print(run_channel_inventory_sync())
