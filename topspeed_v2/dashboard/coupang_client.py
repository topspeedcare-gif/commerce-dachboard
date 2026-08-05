"""쿠팡 Open API 읽기 전용 클라이언트 — V3.7 Core Sync

커버리지:
  - 상품 목록 조회
  - 주문·매출 조회 (createdAt 범위)
  - 취소·반품 조회
  - 판매자배송 재고 조회
  - 로켓그로스 재고 조회
  - 정산 예정·확정 조회

쓰기 API (가격변경·재고변경·상품수정)는 의도적으로 구현하지 않는다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

BASE_URL = "https://api-gateway.coupang.com"
KST = ZoneInfo("Asia/Seoul")


class CoupangClientUnavailable(RuntimeError):
    pass


class CoupangClient:
    def __init__(self, access_key: str, secret_key: str, vendor_id: str) -> None:
        if not access_key or not secret_key or not vendor_id:
            raise CoupangClientUnavailable("쿠팡 API 키 또는 vendorId가 설정되지 않았습니다.")
        self.access_key = access_key.strip()
        self.secret_key = secret_key.strip()
        self.vendor_id = vendor_id.strip()

    # ── 인증 헤더 ─────────────────────────────────────────────
    def _headers(self, method: str, path: str, query: str = "") -> dict[str, str]:
        signed_date = datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")
        message = signed_date + method.upper() + path + query
        signature = hmac.new(
            self.secret_key.encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        auth = (
            "CEA algorithm=HmacSHA256, "
            f"access-key={self.access_key}, signed-date={signed_date}, signature={signature}"
        )
        return {"Authorization": auth, "Content-Type": "application/json;charset=UTF-8"}

    def _get(self, path: str, params: dict | None = None, retries: int = 3) -> dict:
        """
        429(Too Many Requests)는 쿠팡이 짧은 시간에 요청을 너무 많이 보냈을 때
        내려주는 응답이다 — 특히 get_rocket_growth_product_info()처럼 상품
        수십~수백 개를 순차 조회할 때 실제로 발생하는 걸 확인했다(2026-07-25).
        즉시 실패시키는 대신 지수 백오프로 몇 번 재시도한다.
        """
        query = urlencode(params or {})
        url = BASE_URL + path + ("?" + query if query else "")
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            req = Request(url, headers=self._headers("GET", path, query), method="GET")
            try:
                with urlopen(req, timeout=20) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < retries:
                    time.sleep(2 ** (attempt + 1))  # 2s, 4s, 8s
                    last_exc = exc
                    continue
                # 쿠팡이 응답 본문에 실제 사유를 담아 보내는 경우가 많음 (예: 잘못된 status 값,
                # 조회 기간 초과 등) — 예전엔 이 본문을 버리고 "400 Bad Request"만 보여줘서
                # 정확히 뭐가 문제인지 알 수 없었음. 이제 본문까지 그대로 보여준다.
                try:
                    body = exc.read().decode("utf-8")[:300]
                except Exception:
                    body = ""
                raise CoupangClientUnavailable(
                    f"쿠팡 API 호출 실패: HTTP {exc.code} {exc.reason} — {body}"
                ) from exc
            except Exception as exc:
                raise CoupangClientUnavailable(f"쿠팡 API 호출 실패: {exc}") from exc
        raise CoupangClientUnavailable(f"쿠팡 API 호출 실패: {last_exc}")

    # ── 날짜 헬퍼 ─────────────────────────────────────────────
    @staticmethod
    def _date_range(days_back: int) -> tuple[str, str]:
        """KST 기준 days_back일 전 ~ 오늘 날짜 문자열 반환 (yyyy-MM-dd)"""
        today = datetime.now(KST).date()
        start = today - timedelta(days=days_back)
        return str(start), str(today)

    # ── 상품 목록 ─────────────────────────────────────────────
    def list_products(
        self,
        max_per_page: int = 50,
        next_token: int | None = None,
        rocket_growth: bool = False,
    ) -> dict:
        path = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
        params: dict = {
            "vendorId": self.vendor_id,
            "maxPerPage": max(1, min(100, int(max_per_page))),
        }
        if next_token is not None:
            params["nextToken"] = int(next_token)
        if rocket_growth:
            params["businessTypes"] = "rocketGrowth"
        return self._get(path, params)

    def list_all_products(self) -> list[dict]:
        """전체 상품 목록 페이징 수집"""
        results, next_token = [], None
        while True:
            data = self.list_products(max_per_page=100, next_token=next_token)
            items = data.get("data") or []
            results.extend(items)
            next_token = data.get("nextToken")
            if not items or not next_token:
                break
        return results

    def get_product_detail(self, seller_product_id: str | int) -> dict:
        """
        상품 상세 조회 — list_products()의 목록 응답엔 "items"(옵션별 정보)가
        아예 없어서, 옵션별 vendorItemId·가격을 얻으려면 이 상세 API를 따로 호출해야 한다.
        """
        path = f"/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{seller_product_id}"
        return self._get(path)

    def get_rocket_growth_product_info(self) -> dict[str, dict]:
        """get_channel_and_product_info()의 rg_product_info만 필요할 때 쓰는 얇은 래퍼.
        상품 상세를 두 번 순회하지 않도록, 새 코드는 get_channel_and_product_info()를
        직접 쓰는 걸 권장한다."""
        _, rg_product_info = self.get_channel_and_product_info()
        return rg_product_info

    def get_channel_and_product_info(self) -> tuple[dict[str, str], dict[str, dict]]:
        """상품 상세를 한 번만 순회해서 두 가지를 같이 만든다:
          1) channel_map: vendorItemId -> 'wing'(판매자배송) | 'rocket'(로켓그로스)
          2) rg_product_info: 로켓그로스 vendorItemId -> {name, price} (get_rocket_growth_product_info()와 동일한 용도)

        예전엔 이 둘을 각각 별도 함수로 두어 상품 상세를 두 번 순회했다 — 상품이
        59개면 상세 조회가 118번 호출되어 429(rate limit)에 걸리기 쉬웠고,
        실제로 그 때문에 일부 vendorItemId가 채널 분류에서 누락되는 걸 확인했다
        (2026-07-28). 한 번의 순회로 합쳐서 API 호출을 절반으로 줄인다.
        """
        channel_map: dict[str, str] = {}
        rg_product_info: dict[str, dict] = {}
        for product in self.list_all_products():
            sp_id = product.get("sellerProductId")
            if not sp_id:
                continue
            try:
                detail = self.get_product_detail(sp_id)
            except CoupangClientUnavailable:
                continue
            data = detail.get("data") or {}
            product_name = data.get("sellerProductName") or data.get("displayProductName") or ""
            for item in data.get("items") or []:
                item_name = item.get("itemName") or ""
                full_name = f"{product_name} {item_name}".strip() or product_name
                mp = item.get("marketplaceItemData") or {}
                rg = item.get("rocketGrowthItemData") or {}
                if mp.get("vendorItemId"):
                    channel_map[str(mp["vendorItemId"])] = "wing"
                if rg.get("vendorItemId"):
                    vid = str(rg["vendorItemId"])
                    channel_map[vid] = "rocket"
                    price_data = rg.get("priceData") or {}
                    rg_product_info[vid] = {"name": full_name, "price": int(price_data.get("salePrice") or 0)}
        return channel_map, rg_product_info

    def get_full_inventory_report(self) -> list[dict]:
        """
        상품명 기준으로 윙(판매자배송) 재고와 로켓그로스 재고를 나란히 보여주는
        통합 리포트. 옵션 하나(같은 item)라도 판매자배송용 vendorItemId와
        로켓그로스용 vendorItemId가 서로 다르므로, 상품 상세 하나에서 둘 다
        꺼내 짝을 맞춘다.

        윙 재고는 get_vendor_item_inventory()로 옵션마다 개별 조회해야 해서
        상품 수가 많으면 API 호출이 꽤 많아진다(상품 상세 조회 + 옵션별 윙 재고
        조회) — 그래서 list_rocket_growth_inventory()처럼 자주 도는 일일
        동기화에는 안 넣고, 필요할 때 따로 부르는 함수로 뺐다.

        윙에도 로켓그로스에도 없는 옵션(비활성/판매중지)은 결과에서 제외한다.
        """
        rocket_inventory = {it["vendorItemId"]: it for it in self.list_rocket_growth_inventory()}

        report: list[dict] = []
        for product in self.list_all_products():
            sp_id = product.get("sellerProductId")
            if not sp_id:
                continue
            try:
                detail = self.get_product_detail(sp_id)
            except CoupangClientUnavailable:
                continue
            data = detail.get("data") or {}
            product_name = data.get("sellerProductName") or data.get("displayProductName") or ""

            for item in data.get("items") or []:
                item_name = item.get("itemName") or ""
                full_name = f"{product_name} {item_name}".strip() or product_name

                mp = item.get("marketplaceItemData") or {}
                rg = item.get("rocketGrowthItemData") or {}

                wing_vid = mp.get("vendorItemId")
                wing_qty = None
                if wing_vid:
                    try:
                        inv = self.get_vendor_item_inventory(wing_vid)
                        wing_qty = (inv.get("data") or {}).get("amountInStock")
                    except CoupangClientUnavailable:
                        wing_qty = None

                rocket_vid = rg.get("vendorItemId")
                rocket_row = rocket_inventory.get(str(rocket_vid)) if rocket_vid else None
                rocket_qty = rocket_row["quantity"] if rocket_row else None
                rocket_sales_30d = rocket_row["sales_last_30d"] if rocket_row else None

                if wing_vid is None and rocket_vid is None:
                    continue  # 이 옵션은 윙에도 로켓그로스에도 등록되어 있지 않음

                report.append({
                    "product_name": full_name,
                    "wing_qty": wing_qty,
                    "rocket_qty": rocket_qty,
                    "rocket_sales_last_30d": rocket_sales_30d,
                })
        return report

    # ── 주문·매출 ─────────────────────────────────────────────
    def list_orders(
        self,
        created_at_from: str,
        created_at_to: str,
        status: str = "ACCEPT",
        max_per_page: int = 50,
        next_token: str | int | None = None,
    ) -> dict:
        """
        발주서(주문) 목록 조회 — 공식 경로 확인 완료 (2026-07)
        https://developers.coupangcorp.com 문서 기준:
        GET /v2/providers/openapi/apis/api/v4/vendors/{vendorId}/ordersheets

        status: ACCEPT(결제완료) | INSTRUCT(상품준비중) | DEPARTURE(배송지시)
                DELIVERING(배송중) | FINAL_DELIVERY(배송완료) | NONE_TRACKING(배송없음)

        주의: 이 API는 maxPerPage가 최대 50까지만 허용된다 (100을 보내면
        "max page number param should between 1~50" 400 에러가 남 —
        실제로 매일 이 에러가 나서 매출이 하루도 안 쌓이고 있었음, 2026-07-24 확인).
        """
        path = f"/v2/providers/openapi/apis/api/v4/vendors/{self.vendor_id}/ordersheets"
        params = {
            "createdAtFrom": created_at_from,
            "createdAtTo": created_at_to,
            "status": status,
            "maxPerPage": max(1, min(50, max_per_page)),
        }
        if next_token is not None:
            params["nextToken"] = next_token
        return self._get(path, params)

    def list_orders_range(
        self, days_back: int = 7, single_date: str | None = None
    ) -> tuple[list[dict], list[str]]:
        """
        전체 주문 수집 (완료 포함, 상태별로 50건씩 페이징하며 전부 수집)

        single_date를 주면 그 날짜(yyyy-MM-dd) 하루치만, 안 주면 "오늘 기준"
        days_back일 전 ~ 오늘까지 수집한다.

        주의: single_date를 안 쓰면 항상 "지금 이 순간" 기준으로 날짜를 계산한다 —
        예전엔 sync_job.py가 target_date를 결과에 라벨로만 쓰고 실제 조회는 항상
        이 기본값(오늘 기준 어제)으로 했었음. 그래서 예전 날짜를 다시 동기화하면
        엉뚱한 날짜의 주문이 잘못된 날짜 라벨로 저장되는 버그가 있었음 —
        이제 target_date를 그대로 single_date로 넘기면 정확히 그 날짜만 조회된다.

        반환: (주문 리스트, 에러 메시지 리스트)
        이전엔 실패를 조용히 넘어가서 "주문 0건"이 진짜 0건인지
        인증 실패인지 구분이 안 됐음 — 이제 에러를 같이 반환한다.
        """
        if single_date:
            start, end = single_date, single_date
        else:
            start, end = self._date_range(days_back)
        all_orders: list[dict] = []
        errors: list[str] = []
        for status in ["ACCEPT", "INSTRUCT", "DEPARTURE", "DELIVERING", "FINAL_DELIVERY"]:
            next_token = None
            while True:
                try:
                    data = self.list_orders(start, end, status=status, max_per_page=50, next_token=next_token)
                except CoupangClientUnavailable as exc:
                    errors.append(f"{status} 상태 조회 실패: {exc}")
                    break
                orders = data.get("data") or []
                all_orders.extend(orders)
                next_token = data.get("nextToken")
                if not orders or not next_token:
                    break
        return all_orders, errors

    # ── 취소·반품 ─────────────────────────────────────────────
    def list_cancels(self, created_at_from: str, created_at_to: str) -> dict:
        path = f"/v2/providers/seller_api/apis/api/v1/vendor-items/orders/cancels"
        params = {
            "vendorId": self.vendor_id,
            "createdAtFrom": created_at_from,
            "createdAtTo": created_at_to,
            "maxPerPage": 100,
        }
        return self._get(path, params)

    def list_returns(self, received_at_from: str, received_at_to: str) -> dict:
        path = f"/v2/providers/seller_api/apis/api/v1/vendor-items/orders/returns"
        params = {
            "vendorId": self.vendor_id,
            "receivedAtFrom": received_at_from,
            "receivedAtTo": received_at_to,
            "maxPerPage": 100,
        }
        return self._get(path, params)

    def list_cancels_range(self, days_back: int = 7) -> list[dict]:
        start, end = self._date_range(days_back)
        try:
            data = self.list_cancels(start, end)
            return data.get("data") or []
        except CoupangClientUnavailable:
            return []

    def list_returns_range(self, days_back: int = 7) -> list[dict]:
        start, end = self._date_range(days_back)
        try:
            data = self.list_returns(start, end)
            return data.get("data") or []
        except CoupangClientUnavailable:
            return []

    # ── 판매자배송 재고 ───────────────────────────────────────
    def get_vendor_item_inventory(self, vendor_item_id: str | int) -> dict:
        path = (
            f"/v2/providers/seller_api/apis/api/v1/marketplace/"
            f"vendor-items/{vendor_item_id}/inventories"
        )
        return self._get(path)

    def list_seller_inventory(self) -> list[dict]:
        """전체 상품의 판매자배송 재고 수집"""
        products = self.list_all_products()
        result = []
        for product in products:
            for item in product.get("items", []):
                vid = item.get("vendorItemId")
                if not vid:
                    continue
                try:
                    inv = self.get_vendor_item_inventory(vid)
                    qty = (inv.get("data") or {}).get("quantity", 0)
                    result.append({
                        "vendorItemId": str(vid),
                        "itemName": item.get("itemName", ""),
                        "sellerProductName": product.get("sellerProductName", ""),
                        "quantity": int(qty or 0),
                        "type": "seller",
                    })
                except CoupangClientUnavailable:
                    continue
        return result

    # ── 로켓그로스 재고 ───────────────────────────────────────
    def list_rocket_growth_inventory(self) -> list[dict]:
        """
        로켓그로스 창고 재고 + 최근 30일 판매량 수집.

        예전엔 일반 상품 목록 API(businessTypes=rocketGrowth)를 억지로 썼는데,
        그 응답엔애초에 "items" 배열이 없어서(product.get("items", [product])가
        product 자신을 가짜 item으로 써버림) vendorItemId·수량이 전부 빈 값/0으로
        나왔다 — 그 결과 upsert_inventory()가 매번 sku=''로 덮어써져서
        inventory 테이블에 133개가 아니라 실제로는 딱 1줄만 남아있었다
        (2026-07-25 실제 DB에서 확인됨).

        로켓그로스 전용 API(rg_open_api)를 직접 써야 vendorItemId·재고량·
        salesCountMap(최근 30일 판매량)이 제대로 나온다.
        """
        path = f"/v2/providers/rg_open_api/apis/api/v1/vendors/{self.vendor_id}/rg/inventory/summaries"
        result: list[dict] = []
        next_token = None
        for _ in range(200):
            params = {"nextToken": next_token} if next_token else {}
            data = self._get(path, params)
            rows = data.get("data") or []
            for row in rows:
                vid = str(row.get("vendorItemId") or "")
                if not vid:
                    continue
                inv = row.get("inventoryDetails") or {}
                sales = row.get("salesCountMap") or {}
                result.append({
                    "vendorItemId": vid,
                    "quantity": int(inv.get("totalOrderableQuantity") or 0),
                    "sales_last_30d": int(sales.get("SALES_COUNT_LAST_THIRTY_DAYS") or 0),
                    "externalSkuId": row.get("externalSkuId"),
                    "type": "rocket",
                })
            next_token = data.get("nextToken")
            if not rows or not next_token:
                break
        return result

    def list_rg_orders_range(self, date_from: str, date_to: str) -> tuple[list[dict], list[str]]:
        """로켓그로스 주문 조회 — date_from~date_to(YYYY-MM-DD)를 "결제일시(paidAt)"
        기준으로 전부 모아온다 (페이징 포함, 최대 1개월 제약은 호출하는 쪽에서 지킬 것).

        공식 문서(2026-07-29 확인, developers.coupang.com "로켓그로스 주문 API(목록 쿼리)"):
        GET /v2/providers/rg_open_api/apis/api/v1/vendors/{vendorId}/rg/orders
        파라미터: paidDateFrom/paidDateTo (YYYYMMDD, 대시 없이), nextToken(페이징)
        "결제일시" 기준 — ordersheets의 createdAt과 같은 성격의 즉시성 있는 날짜이고,
        정산/매출인식(revenue-history)처럼 지연되는 개념이 아니다(실측 확인: 어제자
        데이터도 바로 조회됨). 응답의 paidAt은 밀리초 단위 유닉스 타임스탬프라
        KST로 직접 변환해야 한다 — 문서에 별도 안내가 없어 실제 호출로 확인함.
        최대 조회 기간 1개월, 분당 50회 호출 제한.

        ⚠️ paidDateTo는 실측상 그 날짜 시작 부근(UTC 자정 추정)에서 잘리는 것으로
        보인다 — paidDateTo=target_date로 조회하면 그날 데이터의 상당수가 빠진다
        (실측: 7/28 45건 중 12건만 잡힘). 항상 원하는 종료일보다 하루 뒤까지
        조회한 뒤 paidAt을 KST로 변환해서 원하는 날짜만 다시 걸러낼 것
        (sync_job.py run_sync()가 이렇게 하고 있음).

        검증: 사용자가 쿠팡 윙 판매자센터에서 직접 확인한 7/28 로켓그로스 주문
        (42개·1,007,250원)과 이 함수 결과(45개·1,046,250원)를 대조 — 약 7% 오차
        (취소/반품 가능성, 이 API엔 상태 필드가 없어 완전히 걸러내진 못함)로
        실사용 가능한 수준임을 확인했다(2026-07-29).
        """
        from_dt = datetime.strptime(date_from, "%Y-%m-%d").date()
        to_dt = datetime.strptime(date_to, "%Y-%m-%d").date()
        path = f"/v2/providers/rg_open_api/apis/api/v1/vendors/{self.vendor_id}/rg/orders"
        all_rows: list[dict] = []
        errors: list[str] = []
        token = None
        for _ in range(200):
            params = {"paidDateFrom": from_dt.strftime("%Y%m%d"), "paidDateTo": to_dt.strftime("%Y%m%d")}
            if token:
                params["nextToken"] = token
            try:
                data = self._get(path, params)
            except CoupangClientUnavailable as exc:
                errors.append(f"로켓그로스 주문 조회 실패: {exc}")
                break
            rows = data.get("data") or []
            all_rows.extend(rows)
            token = data.get("nextToken")
            if not rows or not token:
                break
        return all_rows, errors

    # ══════════════════════════════════════════════════════════
    # ⚠️ 정산 (revenue-history) — 판매일 기준 집계에 쓰지 말 것 (2026-07-28 확정)
    # ══════════════════════════════════════════════════════════
    # 이 API의 날짜 파라미터(recognitionDate)는 "매출 인식일"(구매확정일 또는
    # 배송완료 기준일)이지 "판매일"(saleDate)이 아니다 — 완전히 다른 개념이다.
    # 2026-07-28에 daily_metrics(일별 매출/판매량) 소스를 이 API로 바꿨다가,
    # 실측 검증 결과 문제가 발견되어 되돌렸다:
    #   - recognitionDate 7/20~26으로 조회하면 saleDate 7/13~14 판매만 섞여 나옴
    #     (날짜 자체가 다른 개념이라 "지연"이 아니라 "다른 집합"이었음)
    #   - 한 달이 지난 날짜(saleDate 6/29)도 쿠팡 셀러 인사이트 리포트 대비
    #     극히 일부(13,800원/854,250원, 약 1.6%)만 반영되어 있었음 — 지연이 아니라
    #     이 API 자체가 실제 판매량 대부분을 안 잡는 것으로 보임(원인 미확정)
    # 예전 list_settlements()(/v1/settlement-reports/{date})는 404로 한 번도 성공한
    # 적이 없었다(2026-07-25 확인) — 그래서 이 API로 대체했었지만, 위 문제로 인해
    # **일별 매출/판매량 집계에는 절대 쓰지 말 것.** 정산 대사(내가 예상한 매출과
    # 실제 정산 확정액을 비교하는 용도, dashboard/settlement_sync.py)에서만 사용한다.
    # 새 용도로 쓰기 전엔 반드시 공식 문서에서 날짜 필드 의미를 재확인하고 사용자
    # 승인을 받을 것 (다시 조용히 바꾸지 않기로 함).
    def list_revenue_history(
        self, recognition_date_from: str, recognition_date_to: str, token: str = ""
    ) -> dict:
        """
        매출인식 내역 조회 (단일 페이지). 쿠팡 제약: 조회 기간은 1개월 미만,
        recognitionDateTo는 어제 이전이어야 함 (오늘 지정 시 400 에러).
        """
        path = "/v2/providers/openapi/apis/api/v1/revenue-history"
        params = {
            "vendorId": self.vendor_id,
            "recognitionDateFrom": recognition_date_from,
            "recognitionDateTo": recognition_date_to,
            "maxPerPage": 50,
            "token": token,
        }
        return self._get(path, params)

    def list_revenue_history_between(self, date_from: str, date_to: str) -> tuple[list[dict], list[str]]:
        """recognitionDate 기준 date_from~date_to 구간을 1개월 미만 청크로 쪼개
        전부 모아온다 (페이징 포함). date_to가 오늘/미래면 어제로 당긴다.

        ⚠️ 파일 상단 경고 참고 — daily_metrics(일별 매출/판매량) 집계에는 쓰지 말 것.
        정산 대사 용도로 넓은 구간을 한 번에 긁어올 때만 쓴다. 현재 sync_job.py는
        이 함수를 호출하지 않는다(2026-07-28 되돌림) — 삭제하지 않고 남겨둔 상태.
        """
        from_dt = datetime.strptime(date_from, "%Y-%m-%d").date()
        to_dt = datetime.strptime(date_to, "%Y-%m-%d").date()
        yesterday = datetime.now(KST).date() - timedelta(days=1)
        if to_dt > yesterday:
            to_dt = yesterday
        if from_dt > to_dt:
            return [], []

        all_rows: list[dict] = []
        errors: list[str] = []
        chunk_start = from_dt
        while chunk_start <= to_dt:
            chunk_end = min(chunk_start + timedelta(days=27), to_dt)
            token = ""
            for _ in range(50):
                try:
                    data = self.list_revenue_history(str(chunk_start), str(chunk_end), token=token)
                except CoupangClientUnavailable as exc:
                    errors.append(f"매출인식 조회 실패({chunk_start}~{chunk_end}): {exc}")
                    break
                rows = data.get("data") or []
                all_rows.extend(rows)
                token = data.get("nextToken") or ""
                if not rows or not token:
                    break
            chunk_start = chunk_end + timedelta(days=1)
        return all_rows, errors

    def list_revenue_history_range(self, days_back: int = 25) -> tuple[list[dict], list[str]]:
        """
        recognitionDate 기준 최근 days_back일(기본 25일, 1개월 제약 안전 마진)치
        매출인식 내역을 전부 페이징 수집한다.

        반환: (레코드 리스트, 에러 메시지 리스트). 레코드 하나는 주문 1건 단위이며
        saleDate·recognitionDate·settlementDate·items(SKU별 판매/정산 금액)를 담고 있다.
        """
        today = datetime.now(KST).date()
        end = today - timedelta(days=1)  # recognitionDateTo는 어제 이전만 허용
        start = end - timedelta(days=max(1, min(27, days_back)))  # 1개월 제약 안전 마진

        all_rows: list[dict] = []
        errors: list[str] = []
        token = ""
        for _ in range(50):
            try:
                data = self.list_revenue_history(str(start), str(end), token=token)
            except CoupangClientUnavailable as exc:
                errors.append(f"매출인식 조회 실패: {exc}")
                break
            rows = data.get("data") or []
            all_rows.extend(rows)
            token = data.get("nextToken") or ""
            if not rows or not token:
                break
        return all_rows, errors

    # ── 연결 진단 ─────────────────────────────────────────────
    def diagnose(self) -> dict:
        try:
            data = self.list_products(max_per_page=1)
            ok = str(data.get("code", "")).upper() in {"SUCCESS", "SUCCES"} or bool(data.get("data"))
            return {
                "ok": ok,
                "code": data.get("code"),
                "message": data.get("message"),
                "sample_count": len(data.get("data") or []),
                "mode": "read_only",
            }
        except CoupangClientUnavailable as exc:
            return {"ok": False, "error": str(exc)}
