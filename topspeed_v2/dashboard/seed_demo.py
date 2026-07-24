"""
dashboard/seed_demo.py — 데모/베타 테스트용 샘플 데이터 채우기

실제 쿠팡 API 연동 전에, 대시보드가 어떻게 움직이는지 바로 보여주기 위한 용도.
최근 7일치 SKU별 매출·광고·재고 데이터를 실제 상품명 기준으로 채운다.

실서비스 전환 시: 이 파일은 지우거나 그냥 안 부르면 됨.
(web_app.py의 "샘플 데이터 채우기" 버튼에서만 호출됨 — 자동 실행 아님)
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from shared.db import upsert_daily_metric, upsert_inventory, add_alert

KST = ZoneInfo("Asia/Seoul")

PRODUCTS = {
    "허리보호대-M": {"name": "허리보호대 M", "unit_cost": 8000, "base_qty": 6, "coupang_qty": 31},
    "허리보호대-L": {"name": "허리보호대 L", "unit_cost": 8000, "base_qty": 9, "coupang_qty": 49},
    "허리보호대-XL": {"name": "허리보호대 XL", "unit_cost": 8500, "base_qty": 5, "coupang_qty": 80},
    "무릎보호대-화이트M": {"name": "무릎보호대 화이트 M", "unit_cost": 5000, "base_qty": 3, "coupang_qty": 3},
    "등산가방-화이트": {"name": "등산가방 화이트", "unit_cost": 22000, "base_qty": 2, "coupang_qty": 21},
    "보스턴백-블랙": {"name": "보스턴백 블랙", "unit_cost": 18000, "base_qty": 1, "coupang_qty": 24},
}


def seed_demo_data() -> str:
    random.seed(42)
    today = datetime.now(KST).date()

    for i in range(7):
        d = str(today - timedelta(days=i))
        for sku, info in PRODUCTS.items():
            qty = max(0, info["base_qty"] + random.randint(-2, 3))
            unit_price = info["unit_cost"] * random.uniform(2.2, 3.0)
            revenue = int(qty * unit_price)

            impressions = qty * random.randint(150, 400)
            clicks = max(1, int(impressions * random.uniform(0.005, 0.02)))
            spend = int(clicks * random.uniform(300, 900))

            cost_of_goods = qty * info["unit_cost"]
            coupang_fee = int(revenue * 0.10)
            net_profit = revenue - cost_of_goods - coupang_fee - spend
            roas = round(revenue / spend, 2) if spend else 0.0
            ctr = round(clicks / impressions, 4) if impressions else 0.0
            cvr = round(qty / clicks, 4) if clicks else 0.0

            upsert_daily_metric({
                "date": d,
                "sku": sku,
                "sales_qty": qty,
                "revenue": revenue,
                "ad_impressions": impressions,
                "ad_clicks": clicks,
                "ad_spend": spend,
                "cost_of_goods": cost_of_goods,
                "coupang_fee": coupang_fee,
                "net_profit": net_profit,
                "roas": roas,
                "ctr": ctr,
                "cvr": cvr,
            })

    for sku, info in PRODUCTS.items():
        upsert_inventory(
            sku=sku,
            product_name=info["name"],
            coupang_qty=info["coupang_qty"],
            office_qty=random.randint(0, 50),
            updated_at=datetime.now(KST).isoformat(),
        )

    add_alert(
        kind="low_stock",
        message="🔴 무릎보호대 화이트 M 재고 3개 — 발주 필요 (샘플 알람)",
        created_at=datetime.now(KST).isoformat(),
        sku="무릎보호대-화이트M",
    )
    add_alert(
        kind="ad_issue",
        message="⚠️ 등산가방 화이트 ROAS 1.3 — 광고비 대비 매출 저조 (샘플 알람)",
        created_at=datetime.now(KST).isoformat(),
        sku="등산가방-화이트",
    )

    return f"샘플 데이터 {len(PRODUCTS)}개 SKU × 7일치 생성 완료"
