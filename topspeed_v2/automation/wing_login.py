"""
automation/wing_login.py — 쿠팡 윙 판매자센터에 로그인해서 세션(쿠키)을 저장한다.

⚠️ 실제 윙 로그인 비밀번호를 사용하는 스크립트다 — 반드시 대표님이 직접 실행할 것.
로그인에 성공하면 세션을 automation/.wing_session.json에 저장한다(.gitignore 처리됨,
git에 절대 올라가지 않음). 이후 다른 스크립트(wing_explore.py 등)는 이 세션 파일만
재사용하고 재로그인하지 않는다 — 로그인을 자주 반복하면 쿠팡 봇 탐지에 걸릴 위험이
커지므로, 세션이 살아있는 한 최대한 재사용하는 게 안전하다.

2단계 인증(OTP)이 걸려있으면 브라우저 창이 뜬 채로 자동화가 멈춘다 — 그 상태에서
직접 인증을 완료하고, 로그인된 화면이 보이면 터미널로 돌아와 Enter를 눌러주면 된다.
(headless=False라서 화면이 실제로 보인다.)

사용법:
    python automation\\wing_login.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SESSION_PATH = ROOT / "automation" / ".wing_session.json"
LOGIN_URL = "https://wing.coupang.com/login"


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    wing_id = os.getenv("COUPANG_WING_ID", "").strip()
    wing_pw = os.getenv("COUPANG_WING_PASSWORD", "").strip()
    if not wing_id or not wing_pw:
        print("❌ .env에 COUPANG_WING_ID / COUPANG_WING_PASSWORD가 없습니다. 먼저 채워주세요.")
        return

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        # headless=False — 2단계 인증이 뜨면 직접 처리할 수 있게 실제 창을 띄운다.
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        print(f"로그인 페이지로 이동: {LOGIN_URL}")
        # networkidle은 백그라운드 폴링이 있는 페이지에서 영영 안 끝날 수 있어 타임아웃이
        # 자주 남 — domcontentloaded로 바꾸고, 입력창이 실제로 뜨는지는 아래에서 따로 기다린다.
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_selector('input[type="password"]', timeout=15_000)
        except Exception:
            print("⚠️ 로그인 입력창이 15초 안에 안 떴습니다 — 브라우저 창을 직접 확인해주세요.")

        # 정확한 필드 name/id를 모르는 상태라, type 속성 기준으로 범용적으로 찾는다.
        try:
            id_input = page.locator('input[type="text"], input[type="email"]').first
            pw_input = page.locator('input[type="password"]').first
            id_input.fill(wing_id, timeout=10_000)
            pw_input.fill(wing_pw, timeout=10_000)
        except Exception as exc:
            print(f"⚠️ 아이디/비밀번호 입력창을 자동으로 못 찾았습니다: {exc}")
            print("브라우저 창에서 직접 아이디/비밀번호를 입력하고 로그인해주세요.")
        else:
            try:
                submit = page.locator('button[type="submit"], button:has-text("로그인")').first
                submit.click(timeout=10_000)
                print("로그인 버튼 클릭 완료.")
            except Exception as exc:
                print(f"⚠️ 로그인 버튼을 자동으로 못 찾았습니다: {exc}")
                print("브라우저 창에서 직접 로그인 버튼을 눌러주세요.")

        print()
        print("2단계 인증(OTP)이 뜨면 브라우저 창에서 직접 완료해주세요.")
        input("로그인이 완료되어 윙 화면(로그인 페이지가 아닌)이 보이면, 여기로 돌아와 Enter를 눌러주세요...")

        if "/login" in page.url:
            print("⚠️ 아직 로그인 페이지에 머물러 있는 것 같습니다. 로그인이 정말 완료됐는지 브라우저를 다시 확인해주세요.")
            print(f"   (현재 URL: {page.url})")

        page.context.storage_state(path=str(SESSION_PATH))
        print(f"✅ 세션 저장 완료: {SESSION_PATH}")
        browser.close()


if __name__ == "__main__":
    main()
