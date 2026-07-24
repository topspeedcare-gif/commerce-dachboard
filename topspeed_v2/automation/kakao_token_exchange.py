"""
automation/kakao_token_exchange.py — 카카오 "나에게 보내기" 최초 토큰 발급 (일회성 스크립트)

카카오 개발자센터 인가(authorize) 화면에서 받은 인가 코드로
access_token/refresh_token을 최초 한 번 발급받아 .env에 저장한다.
이후 만료 갱신은 automation/kakao_notify.py가 자동으로 처리하므로
이 스크립트를 다시 실행할 필요는 없다 (인가 코드가 만료되어 재발급받을 때만 예외).

KAKAO_REST_API_KEY / KAKAO_CLIENT_SECRET / KAKAO_REDIRECT_URI는 미리 .env에
있어야 한다. 인가 코드는 이 스크립트에 하드코딩하지 않고 커맨드라인 인자로 받는다
(코드가 소스에 남지 않게 하기 위함 — 인가 코드는 1회용이라 남겨봐야 쓸모도 없다).

사용법:
    python automation/kakao_token_exchange.py <인가코드>
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv, set_key

TOKEN_URL = "https://kauth.kakao.com/oauth/token"


def exchange(auth_code: str) -> dict:
    load_dotenv(ENV_PATH)
    rest_api_key = os.getenv("KAKAO_REST_API_KEY", "").strip()
    client_secret = os.getenv("KAKAO_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("KAKAO_REDIRECT_URI", "https://localhost.com/oauth").strip()

    if not rest_api_key or not client_secret:
        raise RuntimeError("KAKAO_REST_API_KEY / KAKAO_CLIENT_SECRET이 .env에 없습니다.")

    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": rest_api_key,
        "redirect_uri": redirect_uri,
        "code": auth_code,
        "client_secret": client_secret,
    }).encode()

    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded;charset=utf-8")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"카카오 토큰 발급 실패: HTTP {exc.code} — {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"카카오 서버 연결 실패: {exc}") from exc


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if len(sys.argv) < 2:
        print("사용법: python automation/kakao_token_exchange.py <인가코드>")
        sys.exit(1)

    auth_code = sys.argv[1]

    try:
        result = exchange(auth_code)
    except RuntimeError as exc:
        rest_api_key = os.getenv("KAKAO_REST_API_KEY", "")
        print(f"❌ {exc}")
        print(
            "\n인가 코드가 만료됐을 가능성이 높습니다. 아래 주소로 다시 접속해서 "
            "새 코드를 받은 뒤 다시 실행해주세요:\n"
            "https://kauth.kakao.com/oauth/authorize?response_type=code"
            f"&client_id={rest_api_key}"
            "&redirect_uri=https://localhost.com/oauth&scope=talk_message"
        )
        sys.exit(1)

    access_token = result.get("access_token", "")
    refresh_token = result.get("refresh_token", "")
    if not access_token or not refresh_token:
        print(f"❌ 응답에 토큰이 없습니다: {result}")
        sys.exit(1)

    set_key(str(ENV_PATH), "KAKAO_ACCESS_TOKEN", access_token)
    set_key(str(ENV_PATH), "KAKAO_REFRESH_TOKEN", refresh_token)

    print("✅ 토큰 발급 성공 — .env에 KAKAO_ACCESS_TOKEN / KAKAO_REFRESH_TOKEN 저장 완료")
    print(f"   access_token 만료: {result.get('expires_in')}초 후")
    print(f"   refresh_token 만료: {result.get('refresh_token_expires_in')}초 후")


if __name__ == "__main__":
    main()
