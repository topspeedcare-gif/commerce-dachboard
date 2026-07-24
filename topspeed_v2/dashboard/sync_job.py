"""
dashboard/sync_job.py — 일일 동기화 배치

흐름: 쿠팡 주문/광고 데이터 수집 → SKU별 실손익 계산 → 공유 DB 저장 → 알람 감지

하루 한 번(또는 원할 때) 이 스크립트만 실행하면 끝.
AI 비서봇(assistant/)은 이 파일을 절대 import하지 않는다 —
비서봇은 shared/db.py에 쌓인 결과만 읽는다.

사용법:
    python sync_job.py                 # 어제 하루치 동기화
    python sync_job.py --unit-cost-file unit_costs.json   # SKU별 원가 반영
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard.calc import calc_daily_metric, detect_ad_issue
from dashboard.coupang_client import CoupangClient, CoupangClientUnavailable
from dashboard.coupang_ads_client import CoupangAdsClient, CoupangAdsUnavailable
from shared.db import init_db, upsert_daily_metric, upsert_inventory, add_alert, get_setting

KST = ZoneInfo("Asia/Seoul")


def load_unit_costs(path: str | None) -> dict[str, int]:
    """SKU별 원가 설정. 없으면 빈 dict — 계산은 원가 0으로 처리."""
    if not path or not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run_sync(
    target_date: str,
    unit_costs: dict[str, int],
    access_key: str | None = None,
    secret_key: str | None = None,
    vendor_id: str | None = None,
    ads_access: str | None = None,
    ads_secret: str | None = None,
    ads_account: str | None = None,
) -> str:
    """
    쿠팡 API 키를 인자로 직접 받을 수도, 환경변수(.env)에서 읽을 수도 있음.
    → CLI(`python sync_job.py`)와 웹앱(web_app.py의 "지금 동기화" 버튼)이
      같은 함수를 공유해서 쓸 수 있게 하기 위함. 로직이 두 군데로 갈라지지 않음.

    반환값: 사람이 읽는 결과 요약 문자열 (성공/실패 모두 포함, 예외를 던지지 않음)
    """
    init_db()
    log: list[str] = []

    access_key = access_key or os.getenv("COUPANG_ACCESS_KEY", "")
    secret_key = secret_key or os.getenv("COUPANG_SECRET_KEY", "")
    vendor_id = vendor_id or os.getenv("COUPANG_VENDOR_ID", "")

    try:
        client = CoupangClient(access_key, secret_key, vendor_id)
    except CoupangClientUnavailable as exc:
        return f"❌ 쿠팡 오픈API 미연결: {exc}"

    # 1. 주문 데이터 수집 → SKU별 집계
    orders = client.list_orders_range(days_back=1)
    per_sku_sales: dict[str, dict] = defaultdict(lambda: {"qty": 0, "revenue": 0})
    for order in orders:
        for item in order.get("items", []):
            sku = str(item.get("vendorItemId", "unknown"))
            qty = int(item.get("shippingCount", 0) or 0)
            price = int(item.get("orderPrice", 0) or 0)
            per_sku_sales[sku]["qty"] += qty
            per_sku_sales[sku]["revenue"] += price

    # 2. 광고 데이터 수집 (키 없으면 0으로 처리하고 진행 — 대시보드는 항상 떠야 함)
    ad_by_sku: dict[str, dict] = defaultdict(lambda: {"impressions": 0, "clicks": 0, "spend": 0})
    ads_access = ads_access or os.getenv("COUPANG_ADS_ACCESS_KEY", "")
    ads_secret = ads_secret or os.getenv("COUPANG_ADS_SECRET_KEY", "")
    ads_account = ads_account or os.getenv("COUPANG_ADS_ACCOUNT_ID", "")
    if ads_access and ads_secret and ads_account:
        try:
            ads_client = CoupangAdsClient(ads_access, ads_secret, ads_account)
            ad_rows = ads_client.get_ad_performance(target_date, target_date)
            for row in ad_rows:
                sku = str(row.get("sku", "unknown"))
                ad_by_sku[sku]["impressions"] += int(row.get("impressions", 0))
                ad_by_sku[sku]["clicks"] += int(row.get("clicks", 0))
                ad_by_sku[sku]["spend"] += int(row.get("spend", 0))
        except CoupangAdsUnavailable as exc:
            log.append(f"⚠️ 광고 데이터 수집 실패 (매출 계산은 계속 진행): {exc}")
    else:
        log.append("ℹ️ 광고 API 키 미설정 — 광고 지표 없이 매출/재고만 동기화합니다.")

    # 3. SKU별 계산 + 저장 + 알람
    all_skus = set(per_sku_sales) | set(ad_by_sku)
    for sku in all_skus:
        sales = per_sku_sales.get(sku, {"qty": 0, "revenue": 0})
        ads = ad_by_sku.get(sku, {"impressions": 0, "clicks": 0, "spend": 0})

        # 원가: 대시보드 설정 화면에서 저장한 값(setting) 우선, 없으면 json 파일값
        db_cost = get_setting(f"unit_cost:{sku}", "")
        unit_cost = int(db_cost) if db_cost else unit_costs.get(sku, 0)

        metric = calc_daily_metric(
            date=target_date,
            sku=sku,
            sales_qty=sales["qty"],
            revenue=sales["revenue"],
            ad_impressions=ads["impressions"],
            ad_clicks=ads["clicks"],
            ad_spend=ads["spend"],
            unit_cost=unit_cost,
        )
        upsert_daily_metric(metric)

        roas_floor = float(get_setting("roas_floor", "1.5"))
        issue = detect_ad_issue(metric, roas_floor=roas_floor)
        if issue:
            add_alert(kind="ad_issue", message=issue, created_at=datetime.now(KST).isoformat(), sku=sku)

    # 4. 재고 수집·저장
    try:
        rocket_inv = client.list_rocket_growth_inventory()
        for item in rocket_inv:
            upsert_inventory(
                sku=item["vendorItemId"],
                product_name=item.get("sellerProductName", ""),
                coupang_qty=item.get("quantity", 0),
                office_qty=0,
                updated_at=datetime.now(KST).isoformat(),
            )
            low_stock_floor = int(get_setting("low_stock_floor", "5"))
            if item.get("quantity", 0) <= low_stock_floor:
                add_alert(
                    kind="low_stock",
                    message=f"🔴 {item.get('sellerProductName')} 재고 {item.get('quantity')}개 — 발주 필요",
                    created_at=datetime.now(KST).isoformat(),
                    sku=item["vendorItemId"],
                )
    except CoupangClientUnavailable as exc:
        log.append(f"⚠️ 재고 수집 실패: {exc}")

    log.append(f"✅ {target_date} 동기화 완료 — SKU {len(all_skus)}개 처리")
    return "\n".join(log)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="yyyy-MM-dd (기본: 어제)")
    parser.add_argument("--unit-cost-file", default=None, help="SKU별 원가 JSON 파일 경로")
    args = parser.parse_args()

    date = args.date or str(datetime.now(KST).date() - timedelta(days=1))
    costs = load_unit_costs(args.unit_cost_file)
    print(run_sync(date, costs))
