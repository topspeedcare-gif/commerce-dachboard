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

    def _get(self, path: str, params: dict | None = None) -> dict:
        query = urlencode(params or {})
        url = BASE_URL + path + ("?" + query if query else "")
        req = Request(url, headers=self._headers("GET", path, query), method="GET")
        try:
            with urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
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

    def list_orders_range(self, days_back: int = 7) -> tuple[list[dict], list[str]]:
        """
        최근 N일 전체 주문 수집 (완료 포함, 상태별로 50건씩 페이징하며 전부 수집)

        반환: (주문 리스트, 에러 메시지 리스트)
        이전엔 실패를 조용히 넘어가서 "주문 0건"이 진짜 0건인지
        인증 실패인지 구분이 안 됐음 — 이제 에러를 같이 반환한다.
        """
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
        """로켓그로스 창고 재고 수집"""
        data = self.list_products(max_per_page=100, rocket_growth=True)
        products = data.get("data") or []
        result = []
        for product in products:
            for item in product.get("items", [product]):
                vid = str(item.get("vendorItemId") or item.get("sellerProductItemId") or "")
                qty = item.get("stockQuantity") or item.get("quantity") or 0
                result.append({
                    "vendorItemId": vid,
                    "itemName": item.get("itemName", ""),
                    "sellerProductName": product.get("sellerProductName", ""),
                    "quantity": int(qty),
                    "type": "rocket",
                })
        return result

    # ── 정산 ──────────────────────────────────────────────────
    def list_settlements(self, settlement_date: str) -> dict:
        """
        정산 확정 조회 (특정 날짜)
        settlement_date: yyyy-MM-dd
        """
        path = f"/v2/providers/seller_api/apis/api/v1/settlement-reports/{settlement_date}"
        params = {"vendorId": self.vendor_id}
        return self._get(path, params)

    def list_settlements_range(self, days_back: int = 30) -> list[dict]:
        """최근 N일 정산 수집"""
        results = []
        today = datetime.now(KST).date()
        for i in range(days_back):
            d = str(today - timedelta(days=i))
            try:
                data = self.list_settlements(d)
                items = data.get("data") or []
                if items:
                    results.extend(items if isinstance(items, list) else [items])
            except CoupangClientUnavailable:
                continue
        return results

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
