"""
automation/wing_explore_ads.py — 저장된 윙 세션으로 광고센터(캠페인 분석)에 들어가서
실제로 어떤 데이터/API가 있는지 조사하는 1회성 진단 스크립트.

wing_explore.py(판매분석 조사용)와 구조는 동일하고 대상 메뉴만 "광고센터"로 다르다.
wing_login.py를 먼저 실행해서 세션을 저장해둔 뒤 이 스크립트를 실행한다.
비밀번호를 다루지 않으므로 이건 실행해도 안전하다.

결과는 automation/_wing_explore_ads_output/ 폴더에 저장된다(.gitignore 처리됨):
  - page_text.txt    : 페이지에 실제로 보이는 텍스트
  - network_log.txt  : 페이지 로드 중 오간 응답 중 JSON으로 보이는 것만 모은 것
  - screenshot.png   : 화면 캡처

사용법:
    python automation\\wing_explore_ads.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SESSION_PATH = ROOT / "automation" / ".wing_session.json"
OUT_DIR = ROOT / "automation" / "_wing_explore_ads_output"


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if not SESSION_PATH.exists():
        print("❌ 세션 파일이 없습니다. 먼저 python automation\\wing_login.py 를 실행하세요.")
        return

    OUT_DIR.mkdir(exist_ok=True)

    from playwright.sync_api import sync_playwright

    network_log: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(SESSION_PATH))
        page = context.new_page()

        def on_response(response) -> None:
            try:
                ctype = response.headers.get("content-type", "")
                if "json" not in ctype and "text" not in ctype:
                    return
                body = response.text()
                if not body or (not body.strip().startswith("{") and not body.strip().startswith("[")):
                    return
                network_log.append(
                    f"{response.status} {response.request.method} {response.url}\n{body[:3000]}\n{'=' * 80}"
                )
            except Exception:
                pass

        page.on("response", on_response)

        print("대시보드 루트로 이동")
        page.goto("https://wing.coupang.com", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2000)

        # 공지 팝업이 있으면 닫는다 (wing_explore.py에서 확인된 패턴).
        closed = False
        for label in ["닫기", "오늘 하루 보지 않기"]:
            try:
                candidates = page.get_by_text(label, exact=True).all()
                for el in candidates:
                    if el.is_visible():
                        el.click(timeout=3_000)
                        closed = True
                        print(f"'{label}' 클릭해서 팝업 닫음")
                        break
                if closed:
                    break
            except Exception:
                continue
        if not closed:
            page.keyboard.press("Escape")
        page.wait_for_timeout(1000)

        def click_first_visible(text: str) -> bool:
            candidates = page.get_by_text(text, exact=True).all()
            for el in candidates:
                try:
                    if el.is_visible():
                        el.click(timeout=5_000)
                        return True
                except Exception:
                    continue
            return False

        if click_first_visible("광고센터"):
            print("사이드바에서 '광고센터' 클릭함")
        else:
            print("⚠️ 눈에 보이는 '광고센터' 메뉴를 못 찾음 — 스크린샷/page_text로 확인 필요")

        page.wait_for_timeout(2000)

        # 새 탭으로 열릴 수도 있으니 확인한다 (광고센터는 별도 서비스일 가능성 있음).
        all_pages = context.pages
        if len(all_pages) > 1:
            print(f"새 탭이 열림 ({len(all_pages)}개) — 마지막 탭 기준으로 조사")
            page = all_pages[-1]
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
            page.wait_for_timeout(3000)

        print(f"현재 URL: {page.url}")

        (OUT_DIR / "page_text.txt").write_text(page.inner_text("body"), encoding="utf-8")
        (OUT_DIR / "network_log.txt").write_text("\n".join(network_log), encoding="utf-8")
        page.screenshot(path=str(OUT_DIR / "screenshot.png"), full_page=True)

        print(f"✅ 조사 완료 — 결과가 {OUT_DIR} 에 저장됐습니다:")
        print(f"   - page_text.txt")
        print(f"   - network_log.txt (JSON 응답 {len(network_log)}건 포착)")
        print(f"   - screenshot.png")

        browser.close()


if __name__ == "__main__":
    main()
