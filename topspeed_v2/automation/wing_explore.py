"""
automation/wing_explore.py — 저장된 윙 세션으로 판매분석 페이지에 들어가서
실제로 어떤 데이터/버튼/API가 있는지 조사하는 1회성 진단 스크립트.

wing_login.py를 먼저 실행해서 세션을 저장해둔 뒤 이 스크립트를 실행한다.
이 스크립트는 로그인을 하지 않는다 — 저장된 세션(쿠키)만 그대로 재사용한다.
비밀번호를 다루지 않으므로 이건 실행해도 안전하다.

결과는 automation/_wing_explore_output/ 폴더에 저장된다(.gitignore 처리됨):
  - page_text.txt    : 페이지에 실제로 보이는 텍스트
  - network_log.txt  : 페이지 로드 중 오간 XHR/fetch 요청·응답 중 JSON만 모은 것
                        (판매분석 데이터를 실제로 어느 API에서 가져오는지 여기서 찾는다)
  - screenshot.png   : 화면 캡처

사용법:
    python automation\\wing_explore.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SESSION_PATH = ROOT / "automation" / ".wing_session.json"
OUT_DIR = ROOT / "automation" / "_wing_explore_output"
TARGET_URL = "https://wing.coupang.com/tenants/business-insight/sales-analysis?start_date=2026-07-30&end_date=2026-08-05"


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
            # resource_type 필터를 빼고 content-type이 json이거나 본문이 json처럼 보이면 다 잡는다
            # (GraphQL 등은 resource_type이 xhr/fetch로 안 잡히는 경우가 있어서).
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
                pass  # 응답 스트림이 이미 닫혔거나 읽을 수 없는 경우 조용히 건너뛴다

        page.on("response", on_response)

        # URL 직접 이동은 위젯이 초기화가 안 되고 "Loading items..."에서 멈추는 걸 확인함
        # (2026-08-05) — SPA 내부 상태가 클릭 네비게이션에서만 제대로 세팅되는 것으로 보임.
        # 그래서 대시보드 루트로 먼저 간 다음, 사이드바를 실제로 클릭해서 들어간다.
        print("대시보드 루트로 이동")
        page.goto("https://wing.coupang.com", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2000)

        # 공지사항 팝업이 사이드바를 가리는 경우가 있어(확인됨, 2026-08-05) 먼저 닫는다.
        # role=button으로 못 찾을 수 있어(네이티브 버튼이 아닐 수 있음) 텍스트 기반으로 시도하고,
        # 그래도 안 되면 Escape 키로 모달을 닫는 걸 마지막 수단으로 쓴다.
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
            print("팝업 버튼을 못 찾아 Escape 키로 시도함")
        page.wait_for_timeout(1000)

        def click_first_visible(text: str) -> bool:
            """같은 텍스트를 가진 숨겨진 요소(검색창 등)가 여러 개 있을 수 있어,
            눈에 보이는 것만 골라서 클릭한다."""
            candidates = page.get_by_text(text, exact=True).all()
            for el in candidates:
                try:
                    if el.is_visible():
                        el.click(timeout=5_000)
                        return True
                except Exception:
                    continue
            return False

        try:
            # '판매분석'은 '비즈니스 인사이트' 하위 2단계 메뉴라 부모를 먼저 펼쳐야 보인다.
            if click_first_visible("비즈니스 인사이트"):
                print("사이드바에서 '비즈니스 인사이트' 클릭함 (하위 메뉴 펼치기)")
            else:
                print("⚠️ 눈에 보이는 '비즈니스 인사이트' 메뉴를 못 찾음")
            page.wait_for_timeout(1000)
            if click_first_visible("판매분석"):
                print("사이드바에서 '판매분석' 클릭함")
            else:
                print("⚠️ 눈에 보이는 '판매분석' 메뉴를 못 찾음")
        except Exception as exc:
            print(f"⚠️ 메뉴 클릭 실패: {exc}")

        print("데이터 로딩 대기 중 (최대 20초)...")
        try:
            page.wait_for_selector("text=Loading items", state="detached", timeout=20_000)
        except Exception:
            print("⚠️ 'Loading items...' 상태가 20초 안에 안 사라졌습니다 — 그래도 계속 진행합니다.")
        page.wait_for_timeout(3000)

        (OUT_DIR / "page_text.txt").write_text(page.inner_text("body"), encoding="utf-8")
        (OUT_DIR / "network_log.txt").write_text("\n".join(network_log), encoding="utf-8")
        page.screenshot(path=str(OUT_DIR / "screenshot.png"), full_page=True)

        # iframe 안에 실제 위젯이 들어있을 수도 있어 각 iframe의 텍스트도 따로 저장한다.
        for i, frame in enumerate(page.frames):
            if frame == page.main_frame:
                continue
            try:
                text = frame.inner_text("body")
                if text.strip():
                    (OUT_DIR / f"iframe_{i}_text.txt").write_text(f"URL: {frame.url}\n\n{text}", encoding="utf-8")
            except Exception:
                pass

        if "/login" in page.url:
            print("⚠️ 세션이 만료된 것 같습니다 (로그인 페이지로 튕김). wing_login.py를 다시 실행해서 세션을 갱신해주세요.")
        else:
            print(f"✅ 조사 완료 — 결과가 {OUT_DIR} 에 저장됐습니다:")
            print(f"   - page_text.txt")
            print(f"   - network_log.txt (JSON 응답 {len(network_log)}건 포착)")
            print(f"   - screenshot.png")
            print()
            print("이 세 파일을 확인하시거나 저에게 공유해주시면, 실제 데이터를 어디서")
            print("가져와야 하는지 분석해서 다음 단계(자동 수집 스크립트)를 만들겠습니다.")

        browser.close()


if __name__ == "__main__":
    main()
