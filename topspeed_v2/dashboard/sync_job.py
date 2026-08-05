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
from shared.db import init_db, get_conn, upsert_daily_metric, upsert_inventory, add_alert, get_setting

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
    client: "CoupangClient | None" = None,
    channel_info: tuple[dict[str, str], dict[str, dict]] | None = None,
    skip_inventory: bool = False,
) -> str:
    """
    쿠팡 API 키를 인자로 직접 받을 수도, 환경변수(.env)에서 읽을 수도 있음.
    → CLI(`python sync_job.py`)와 웹앱(web_app.py의 "지금 동기화" 버튼)이
      같은 함수를 공유해서 쓸 수 있게 하기 위함. 로직이 두 군데로 갈라지지 않음.

    client/channel_info: 여러 날짜를 한 번에 동기화할 때(sync_date_range) 상품 상세
    순회를 날짜마다 반복하지 않도록 미리 만든 CoupangClient/채널맵을 재사용할 수 있게
    받는 선택 인자. 둘 다 없으면 이 함수 안에서 새로 만든다(기존 단일 날짜 동작 그대로).

    skip_inventory: 재고는 "지금 이 순간"의 스냅샷이라 과거 날짜별로 다시 받아올
    의미가 없다 — 기간 동기화(sync_date_range)에서 날짜마다 반복하면 하루당 약 10초씩
    낭비된다(실측: 로켓그로스 재고 229건 조회가 대부분을 차지). 과거 날짜 구간에서는
    True로 스킵하고, 가장 최근 날짜에서만 한 번 받는다.

    반환값: 사람이 읽는 결과 요약 문자열 (성공/실패 모두 포함, 예외를 던지지 않음)
    """
    init_db()
    log: list[str] = []

    if client is None:
        access_key = access_key or os.getenv("COUPANG_ACCESS_KEY", "")
        secret_key = secret_key or os.getenv("COUPANG_SECRET_KEY", "")
        vendor_id = vendor_id or os.getenv("COUPANG_VENDOR_ID", "")
        try:
            client = CoupangClient(access_key, secret_key, vendor_id)
        except CoupangClientUnavailable as exc:
            return f"❌ 쿠팡 오픈API 미연결: {exc}"

    # 1. 주문(ordersheets, createdAtFrom/To 기준) — daily_metrics.revenue/sales_qty의
    # 유일한 소스. 2026-07-28에 매출인식(revenue-history)으로 바꿨다가 되돌렸다 —
    # revenue-history는 "매출 인식일"(구매확정일 또는 배송완료 기준, 실제 판매일과는
    # 완전히 다른 개념) 기준이라 판매일 집계에 쓰면 안 되고, 실측으로도 한 달이 지나도
    # 실제 판매량의 극히 일부만 반영되는 걸 확인했다(쿠팡 셀러 인사이트 리포트 대조).
    # ordersheets는 createdAtFrom/To로 정확히 그 날짜에 "생성된" 주문만 조회되므로
    # 판매일 집계에 맞는 API다. 다만 윙(판매자배송)만 잡히고 로켓그로스는 전혀
    # 안 잡힌다(확인됨: 최근 주문 vendorItemId와 로켓그로스 vendorItemId 229개 사이
    # overlap 0건) — 로켓그로스 매출은 이 경로로는 아직 못 채운다.
    orders, order_errors = client.list_orders_range(single_date=target_date)
    for err in order_errors:
        log.append(f"⚠️ 주문 {err}")

    per_sku_sales: dict[str, dict] = defaultdict(lambda: {"qty": 0, "revenue": 0})
    for order in orders:
        # 쿠팡 API가 실제로 내려주는 필드명은 "orderItems"다 ("items"가 아님).
        for item in order.get("orderItems", []):
            sku = str(item.get("vendorItemId", "unknown"))
            qty = int(item.get("shippingCount", 0) or 0)
            price = int(item.get("orderPrice", 0) or 0)
            per_sku_sales[sku]["qty"] += qty
            per_sku_sales[sku]["revenue"] += price
    wing_qty = sum(v["qty"] for v in per_sku_sales.values())
    log.append(f"ℹ️ {target_date} 윙 주문(ordersheets, 판매일 기준): {len(orders)}건 · {wing_qty}개")

    # 1.5. 로켓그로스 주문 — 2026-07-29 확인된 전용 API(paidAt=결제일시 기준, 즉시성
    # 있음, revenue-history와 다름). 실제 쿠팡 윙 판매자센터에서 사용자가 직접 확인한
    # 7/28 로켓그로스 주문(42개·1,007,250원)과 이 API 결과(45개·1,046,250원)를
    # 대조해서 검증 완료 — 약 7% 오차(취소/반품 건이 섞였을 가능성, 이 API엔 상태
    # 필드가 없어 완전히 걸러내진 못함)로 실사용 가능한 수준임을 확인했다.
    #
    # ⚠️ paidDateFrom=paidDateTo=target_date로 정확히 하루만 조회하면 실제로 있는
    # 데이터의 상당수가 빠지는 걸 실측으로 확인했다(2026-07-29, 예: 7/28 45개 중
    # 12개만 잡힘) — paidDateTo가 그 날짜 시작 부근(UTC 자정 추정)에서 잘리는
    # 것으로 보인다. 그래서 항상 원하는 종료일보다 하루 뒤까지 넓게 조회한 뒤,
    # paidAt을 KST로 직접 변환해서 target_date와 정확히 일치하는 것만 걸러낸다.
    target_dt = datetime.strptime(target_date, "%Y-%m-%d").date()
    rg_query_from = (target_dt - timedelta(days=1)).isoformat()
    rg_query_to = (target_dt + timedelta(days=1)).isoformat()
    rg_orders, rg_errors = client.list_rg_orders_range(rg_query_from, rg_query_to)
    for err in rg_errors:
        log.append(f"⚠️ {err}")
    rg_qty_added = 0
    for order in rg_orders:
        paid_at_dt = datetime.fromtimestamp(int(order.get("paidAt", 0)) / 1000, tz=KST)
        if paid_at_dt.date().isoformat() != target_date:
            continue  # 앞뒤로 넓게 조회했으니 target_date 하루치만 다시 걸러낸다
        for item in order.get("orderItems") or []:
            sku = str(item.get("vendorItemId") or "unknown")
            qty = int(item.get("salesQuantity") or 0)
            unit_price = float(item.get("unitSalesPrice") or 0)
            per_sku_sales[sku]["qty"] += qty
            per_sku_sales[sku]["revenue"] += int(round(unit_price * qty))
            rg_qty_added += qty
    log.append(f"ℹ️ {target_date} 로켓그로스 주문(paidAt 기준): {len(rg_orders)}건 · {rg_qty_added}개")
    log.append(f"ℹ️ {target_date} 매출 SKU 합계(윙+로켓그로스): {len(per_sku_sales)}개")

    # 2. 채널 분류 — ordersheets 응답엔 채널 구분이 없어서, 상품 상세를 순회해
    # 두 채널의 vendorItemId를 미리 분류해둔다. 로켓그로스 상품명·판매가 매핑
    # (rg_product_info)도 같은 순회에서 같이 얻는다 — 상품 상세를 두 번 순회하면
    # API 호출이 배로 늘어 429(rate limit)에 걸리기 쉬웠고, 그 때문에 일부 SKU가
    # 채널 분류에서 누락되는 걸 실제로 확인했다.
    if channel_info is not None:
        channel_map, rg_product_info = channel_info
    else:
        channel_map, rg_product_info = {}, {}
        try:
            channel_map, rg_product_info = client.get_channel_and_product_info()
        except CoupangClientUnavailable as exc:
            log.append(f"⚠️ 채널/상품정보 조회 실패 (윙/로켓그로스 구분 없이 진행): {exc}")

    # 4. 광고 데이터: 쿠팡은 셀러가 쓸 수 있는 광고 성과 공개 API를 제공하지 않는다
    # (직접 확인함) — 광고관리센터에서 받은 엑셀 리포트를 등록하는 방식으로만 반영한다.
    ad_by_sku: dict[str, dict] = defaultdict(lambda: {"impressions": 0, "clicks": 0, "spend": 0})

    # 5. SKU별 계산 + 저장 + 알람
    all_skus = set(per_sku_sales) | set(ad_by_sku)
    for sku in all_skus:
        sales = per_sku_sales.get(sku, {"qty": 0, "revenue": 0, "fee": 0})
        ads = ad_by_sku.get(sku, {"impressions": 0, "clicks": 0, "spend": 0})

        # 원가: 대시보드 설정 화면에서 저장한 값(setting) 우선, 없으면 json 파일값
        db_cost = get_setting(f"unit_cost:{sku}", "")
        unit_cost = int(db_cost) if db_cost else unit_costs.get(sku, 0)
        # ordersheets에는 수수료 정보가 없어서 calc_daily_metric의 기본 추정치(10%)를 쓴다.

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
        metric["channel"] = channel_map.get(sku, "unknown")
        upsert_daily_metric(metric)

        roas_floor = float(get_setting("roas_floor", "1.5"))
        issue = detect_ad_issue(metric, roas_floor=roas_floor)
        if issue:
            add_alert(kind="ad_issue", message=issue, created_at=datetime.now(KST).isoformat(), sku=sku)

    # 6. 재고 수집·저장 — 로켓그로스 먼저 시도, 권한 없으면(403 등) 판매자배송으로 폴백.
    # 재고는 "지금 이 순간" 스냅샷이라 과거 날짜에는 의미가 없다 — skip_inventory=True면
    # 건너뛴다(기간 동기화에서 마지막 날짜에만 한 번 수집).
    if skip_inventory:
        log.append("ℹ️ 재고 수집 생략 (과거 날짜 — 기간 동기화의 최근 날짜에서만 수집됩니다)")
    else:
        # 상품명·판매가 매핑(rg_product_info)은 위 2번 단계에서 이미 받아온 걸 그대로 쓴다
        # (상품 상세를 여기서 또 순회하면 API 호출이 불필요하게 배로 늘어난다).
        inventory_items: list[dict] = []
        try:
            inventory_items = client.list_rocket_growth_inventory()
            log.append(f"ℹ️ 로켓그로스 재고 {len(inventory_items)}건 수집 (상품명·판매가 매핑 {len(rg_product_info)}건)")
        except CoupangClientUnavailable as exc:
            log.append(f"⚠️ 로켓그로스 재고 수집 실패 ({exc}) — 판매자배송 재고로 재시도합니다")
            try:
                seller_items = client.list_seller_inventory()
                inventory_items = [
                    {
                        "vendorItemId": it["vendorItemId"],
                        "sellerProductName": it.get("sellerProductName", ""),
                        "quantity": it.get("quantity", 0),
                    }
                    for it in seller_items
                ]
                log.append(f"ℹ️ 판매자배송 재고 {len(inventory_items)}건으로 대체 수집")
            except CoupangClientUnavailable as exc2:
                log.append(
                    f"❌ 판매자배송 재고도 실패: {exc2}\n"
                    "→ 점검 순서: ① 쿠팡윙 개발자센터에서 이 액세스키에 "
                    "'재고 조회' 권한이 켜져 있는지 ② 로켓그로스 상품이 아닌데 "
                    "로켓그로스 API를 호출한 건 아닌지 ③ VENDOR_ID 오타 여부"
                )

        low_stock_floor = int(get_setting("low_stock_floor", "5"))
        # 로켓그로스는 판매속도가 SKU마다 크게 달라서, 고정 수량 기준(예: 5개 이하)보다
        # "일 판매량 대비 며칠 치 남았는지"가 훨씬 실질적인 경고 기준이다.
        # 판매 이력이 없는 신상품은 velocity=0이라 이 계산이 불가능하므로,
        # 그 경우에만 예전 방식(고정 수량 기준)으로 대체 판단한다.
        rocket_stock_alert_days = int(get_setting("rocket_stock_alert_days", "7"))
        for item in inventory_items:
            sku = item["vendorItemId"]
            info = rg_product_info.get(sku, {})
            product_name = info.get("name") or item.get("sellerProductName", "")
            # 로켓그로스는 salesCountMap.SALES_COUNT_LAST_THIRTY_DAYS(최근 30일 판매량)를
            # 30으로 나눈 하루 평균을 판매속도로 쓴다 — ordersheets엔 로켓그로스 판매가
            # 거의 안 잡혀서, 이게 사실상 유일하게 신뢰할 수 있는 판매속도 소스다.
            sales_last_30d = item.get("sales_last_30d")
            velocity = round(sales_last_30d / 30, 3) if sales_last_30d is not None else None
            unit_price = info.get("price")

            # 쿠팡 동기화는 coupang_qty만 알고 있다 — office_qty는 2창고 기능(입고/이관/출고)이
            # 별도로 관리하는 값이라, 여기서 0으로 덮어쓰면 그 기록이 매 동기화마다 사라진다.
            # 기존 office_qty를 읽어와서 그대로 유지한다.
            with get_conn() as _c:
                existing = _c.execute("SELECT office_qty FROM inventory WHERE sku = ?", (sku,)).fetchone()
            existing_office_qty = existing["office_qty"] if existing else 0

            upsert_inventory(
                sku=sku,
                product_name=product_name,
                coupang_qty=item.get("quantity", 0),
                office_qty=existing_office_qty,
                updated_at=datetime.now(KST).isoformat(),
                sales_velocity_30d=velocity,
                unit_price=unit_price,
            )
            qty = item.get("quantity", 0)
            if velocity and velocity > 0:
                stock_days = round(qty / velocity, 1)
                if stock_days <= rocket_stock_alert_days:
                    add_alert(
                        kind="rocket_low_stock",
                        message=(
                            f"🚀 {product_name or sku} 재고 {qty}개 — 약 {stock_days}일 후 품절 예상 "
                            f"(일 평균 {velocity:.1f}개 판매) — 발주 필요"
                        ),
                        created_at=datetime.now(KST).isoformat(),
                        sku=sku,
                    )
            elif qty <= low_stock_floor:
                # 판매 이력이 없어 판매속도를 모르는 경우에만 예전 방식(고정 수량 기준)으로 판단.
                add_alert(
                    kind="low_stock",
                    message=f"🔴 {product_name or sku} 재고 {qty}개 — 발주 필요 (판매 이력 없음)",
                    created_at=datetime.now(KST).isoformat(),
                    sku=sku,
                )

    log.append(f"✅ {target_date} 동기화 완료 — SKU {len(all_skus)}개 처리")
    return "\n".join(log)


def sync_date_range(
    start_date: str,
    end_date: str,
    unit_costs: dict[str, int],
    access_key: str | None = None,
    secret_key: str | None = None,
    vendor_id: str | None = None,
) -> str:
    """start_date~end_date를 하루씩 run_sync()로 채운다(ordersheets 기준, 판매일=조회일).

    채널 분류(get_channel_and_product_info)는 날짜가 몇 개든 딱 한 번만 하면 되므로
    여기서 미리 만들어 각 날짜의 run_sync()에 재사용시킨다 — 안 그러면 날짜 수만큼
    상품 상세를 반복 순회해서 시간이 오래 걸리고 429(rate limit) 위험도 커진다.
    """
    access_key = access_key or os.getenv("COUPANG_ACCESS_KEY", "")
    secret_key = secret_key or os.getenv("COUPANG_SECRET_KEY", "")
    vendor_id = vendor_id or os.getenv("COUPANG_VENDOR_ID", "")
    try:
        client = CoupangClient(access_key, secret_key, vendor_id)
    except CoupangClientUnavailable as exc:
        return f"❌ 쿠팡 오픈API 미연결: {exc}"

    try:
        channel_info = client.get_channel_and_product_info()
    except CoupangClientUnavailable as exc:
        return f"❌ 채널 분류 실패: {exc}"

    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if start > end:
        start, end = end, start

    results = []
    day = start
    while day <= end:
        results.append(run_sync(
            str(day), unit_costs, client=client, channel_info=channel_info,
            skip_inventory=(day != end),  # 재고는 스냅샷이라 구간의 마지막 날짜에서만 수집
        ))
        day += timedelta(days=1)
    return "\n\n".join(results)


if __name__ == "__main__":
    # Windows 콘솔 기본 인코딩(cp949)은 이모지(✅❌⚠️)를 못 담아서 그냥 실행하면
    # UnicodeEncodeError로 죽는다 — 실제로 확인된 문제라 표준출력을 UTF-8로 강제한다.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:
        pass  # python-dotenv 없으면 시스템 환경변수만 사용

    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="yyyy-MM-dd (기본: 어제)")
    parser.add_argument("--unit-cost-file", default=None, help="SKU별 원가 JSON 파일 경로")
    args = parser.parse_args()

    date = args.date or str(datetime.now(KST).date() - timedelta(days=1))
    costs = load_unit_costs(args.unit_cost_file)
    print(run_sync(date, costs))
