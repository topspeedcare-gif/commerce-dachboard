"""
dashboard/coupang_ads_client.py — 쿠팡 광고(Ads) API 클라이언트

주의: 쿠팡 광고 API는 오픈API(coupang_client.py)와 별도 신청·별도 키가 필요합니다.
     https://advertising.coupang.com 파트너센터에서 광고 API 접근 권한을 먼저 신청하세요.

이 파일은 인증 방식(HMAC)은 오픈API와 동일한 패턴으로 미리 맞춰뒀지만,
실제 엔드포인트 경로(path)는 광고 API 키 발급 후 공식 문서에서
반드시 확인·교체해야 합니다. TODO 표시된 부분이 그 지점입니다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ADS_BASE_URL = "https://api-gateway.coupang.com"  # TODO: 광고 API 전용 게이트웨이 여부 확인


class CoupangAdsUnavailable(RuntimeError):
    pass


class CoupangAdsClient:
    def __init__(self, access_key: str, secret_key: str, ad_account_id: str) -> None:
        if not access_key or not secret_key or not ad_account_id:
            raise CoupangAdsUnavailable("광고 API 키 또는 ad_account_id가 설정되지 않았습니다.")
        self.access_key = access_key.strip()
        self.secret_key = secret_key.strip()
        self.ad_account_id = ad_account_id.strip()

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
        url = ADS_BASE_URL + path + ("?" + query if query else "")
        req = Request(url, headers=self._headers("GET", path, query), method="GET")
        try:
            with urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise CoupangAdsUnavailable(f"광고 API 호출 실패: {exc}") from exc

    # ── 광고 성과 조회 ────────────────────────────────────────
    def get_ad_performance(self, date_from: str, date_to: str) -> list[dict]:
        """
        SKU별 일일 광고 성과 조회 (노출수·클릭수·클릭비용·전환수)

        TODO: 실제 경로는 쿠팡 광고 API 문서의
              "광고 리포트 조회" 엔드포인트로 교체 필요.
        """
        path = "/v2/providers/ads_api/apis/api/v1/reports/campaign-performance"  # TODO 확인
        params = {
            "adAccountId": self.ad_account_id,
            "startDate": date_from,
            "endDate": date_to,
        }
        data = self._get(path, params)
        return data.get("data") or []

    def diagnose(self) -> dict:
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            data = self.get_ad_performance(today, today)
            return {"ok": True, "sample_count": len(data)}
        except CoupangAdsUnavailable as exc:
            return {"ok": False, "error": str(exc)}
