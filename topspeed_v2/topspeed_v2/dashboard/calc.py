"""
dashboard/calc.py — 실손익 계산

SKU 하나의 하루치 원본 데이터(주문 데이터 + 광고 데이터 + 원가·수수료 설정)를
입력받아 순이익·ROAS·CTR·CVR을 계산한다.

이 함수는 coupang_client / coupang_ads_client 어느 쪽도 import하지 않는다.
순수 계산 로직만 담당 — 테스트하기 쉽고, 나중에 로직을 고쳐도
데이터 수집 코드는 안 건드려도 된다.
"""
from __future__ import annotations


def calc_daily_metric(
    date: str,
    sku: str,
    sales_qty: int,
    revenue: int,
    ad_impressions: int,
    ad_clicks: int,
    ad_spend: int,
    unit_cost: int,
    coupang_fee_rate: float = 0.10,
) -> dict:
    """
    unit_cost: SKU 1개당 원가 (사용자가 설정 파일 또는 대시보드에서 입력)
    coupang_fee_rate: 쿠팡 판매 수수료율 (기본 10%, 카테고리별로 다르면 인자로 조정)
    """
    cost_of_goods = sales_qty * unit_cost
    coupang_fee = int(revenue * coupang_fee_rate)
    net_profit = revenue - cost_of_goods - coupang_fee - ad_spend

    roas = (revenue / ad_spend) if ad_spend > 0 else 0.0
    ctr = (ad_clicks / ad_impressions) if ad_impressions > 0 else 0.0
    cvr = (sales_qty / ad_clicks) if ad_clicks > 0 else 0.0

    return {
        "date": date,
        "sku": sku,
        "sales_qty": sales_qty,
        "revenue": revenue,
        "ad_impressions": ad_impressions,
        "ad_clicks": ad_clicks,
        "ad_spend": ad_spend,
        "cost_of_goods": cost_of_goods,
        "coupang_fee": coupang_fee,
        "net_profit": net_profit,
        "roas": round(roas, 2),
        "ctr": round(ctr, 4),
        "cvr": round(cvr, 4),
    }


def detect_ad_issue(metric: dict, roas_floor: float = 1.5, ctr_floor: float = 0.005) -> str | None:
    """
    광고 이슈 알람 조건. 두 가지만 우선 체크 (필요시 조건 추가):
      - ROAS가 기준치 미만 → 광고비 대비 매출이 안 나옴
      - CTR이 기준치 미만인데 노출은 충분함 → 소재/타겟 문제 가능성
    """
    if metric["ad_spend"] == 0:
        return None

    if metric["roas"] < roas_floor:
        return f"⚠️ {metric['sku']} ROAS {metric['roas']} (기준 {roas_floor} 미만) — 광고비 대비 매출 저조"

    if metric["ad_impressions"] >= 1000 and metric["ctr"] < ctr_floor:
        return f"⚠️ {metric['sku']} CTR {metric['ctr']*100:.2f}% (기준 {ctr_floor*100:.1f}% 미만) — 소재/타겟 점검 필요"

    return None
