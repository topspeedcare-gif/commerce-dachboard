"""
automation/Daily_sync.py — 완전 자동화 스크립트

Windows 작업 스케줄러가 매일 이 파일을 실행하면:
  1. .env에 있는 쿠팡 키로 sync_job.run_sync() 실행 (PC의 등록된 IP로 호출되므로 403 안 남)
  2. 결과를 shared/topspeed.db에 저장
  3. 그 DB 파일을 GitHub에 자동 commit + push
  4. Streamlit Cloud가 GitHub 변경을 감지하고 자동 재배포 → 대시보드에 반영

사전 조건:
  - 이 폴더가 실제로 GitHub 저장소와 연결된 git 클론본이어야 함 (git clone으로 받은 폴더)
  - PC에서 `git push`가 이미 한 번 이상 성공한 적 있어야 함 (인증 캐시됨)
  - .env 파일에 COUPANG_ACCESS_KEY 등이 채워져 있어야 함
  - 이 PC의 공인 IP가 쿠팡윙 개발자센터에 등록되어 있어야 함
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

KST = ZoneInfo("Asia/Seoul")
LOG_PATH = ROOT / "automation" / "sync_log.txt"
STATUS_PATH = ROOT / "automation" / "last_sync_status.json"


def find_git() -> str:
    """
    시스템 PATH에 git이 등록되어 있으면 그걸 쓰고,
    없으면(흔히 GitHub Desktop만 설치하고 'Git for Windows'는 따로 설치 안 한 경우)
    GitHub Desktop이 내장해둔 git.exe 위치를 직접 찾아본다.
    """
    found = shutil.which("git")
    if found:
        return found

    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "GitHubDesktop",
    ]
    for base in candidates:
        if not base.exists():
            continue
        for git_path in base.rglob("mingw64/bin/git.exe"):
            return str(git_path)
        for git_path in base.rglob("git.exe"):
            return str(git_path)

    raise FileNotFoundError(
        "git.exe를 찾을 수 없습니다. https://git-scm.com/download/win 에서 "
        "'Git for Windows'를 설치하시면 (기본 옵션 그대로) 해결됩니다."
    )


GIT_EXE = None  # main()에서 채워짐


def log(msg: str) -> None:
    line = f"[{datetime.now(KST).isoformat(timespec='seconds')}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        # 로그 파일 기록 실패가 동기화 자체를 막아선 안 된다 — 2026-07-26~08-05(10일간)
        # daily_sync.bat의 stdout 리다이렉트(>> sync_log.txt)와 이 open()이 같은 파일을
        # 동시에 붙잡으면서 매번 여기서 PermissionError로 죽어 동기화가 통째로 실패했다.
        # bat의 리다이렉트는 제거했지만, 다른 이유(OneDrive 동기화 등)로 파일이 잠겨도
        # 최소한 콘솔 출력은 남도록 방어한다.
        print(f"⚠️ 로그 파일 기록 실패 (동기화는 계속 진행): {exc}")


def run_git(*args: str) -> tuple[int, str]:
    result = subprocess.run(
        [GIT_EXE, *args], cwd=ROOT, capture_output=True, text=True
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def _write_status(status: dict) -> None:
    """
    이번 실행이 어디까지 갔는지 JSON으로 남긴다 — dashboard/sync_health.py가
    이 파일을 읽어서 "동기화는 됐는데 git push만 실패해서 배포된 대시보드엔
    안 보이는" 것 같은, sync_log.txt를 일일이 파싱하지 않고는 알기 어려운
    상황을 구분해낸다. 실행이 어느 단계에서 끝나든(성공/실패 무관) 항상 남긴다.
    """
    status["finished_at"] = datetime.now(KST).isoformat(timespec="seconds")
    try:
        STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log(f"⚠️ 상태 파일 기록 실패 (본 동기화 결과엔 영향 없음): {exc}")


def main() -> None:
    global GIT_EXE
    # Windows 콘솔 기본 인코딩(cp949)은 이모지(✅❌⚠️)를 못 담아서 그냥 실행하면
    # 작업 스케줄러가 매번 UnicodeEncodeError로 죽는다 — UTF-8로 강제한다.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        GIT_EXE = find_git()
        log(f"git 위치 확인: {GIT_EXE}")
    except FileNotFoundError as exc:
        log(f"❌ {exc}")
        return

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass  # python-dotenv 없으면 시스템 환경변수만 사용

    from dashboard.sync_job import run_sync

    target_date = str(datetime.now(KST).date() - timedelta(days=1))
    status = {
        "target_date": target_date,
        "sync_ok": False,
        "git_committed": False,
        "git_pushed": False,
        "kakao_sent": False,
    }

    try:
        log(f"동기화 시작: {target_date}")

        result = run_sync(target_date, unit_costs={})
        log("동기화 결과:\n" + result)
        status["sync_ok"] = "❌" not in result

        # 취소·반품까지 반영된 정확한 매출(wing_sync.py, 윙 로그인 세션 재사용) —
        # 세션이 만료돼 실패해도 로켓그로스 주문 API 기반 동기화 자체는 이미 끝났으니
        # 여기서 막지 않는다. 세션 재로그인은 사람이 python automation\wing_login.py로
        # 직접 해야 한다(비밀번호를 쓰는 부분이라 자동화하지 않음).
        try:
            from automation.wing_sync import run_wing_sync
            wing_result = run_wing_sync()
            log(f"윙 판매통계 동기화: {wing_result}")
        except Exception as exc:
            log(f"⚠️ 윙 판매통계 동기화 실패 (동기화 자체는 정상 진행): {exc}")

        # DB 변경사항을 GitHub에 반영
        code, out = run_git("add", "shared/topspeed.db")
        log(f"git add: {out or 'OK'}")

        code, out = run_git("status", "--porcelain")
        if not out.strip():
            log("변경사항 없음 (동기화 결과가 이전과 동일) — commit 생략")
            # 변경사항이 없다는 건 이미 최신 상태로 커밋·푸시돼 있다는 뜻이라 실패가 아니다.
            status["git_committed"] = True
            status["git_pushed"] = True
            return

        code, out = run_git("commit", "-m", f"auto sync {target_date}")
        log(f"git commit: {out}")
        if code != 0:
            log("❌ commit 실패 — 아래 push는 건너뜁니다")
            return
        status["git_committed"] = True

        code, out = run_git("push")
        log(f"git push: {out}")
        if code == 0:
            status["git_pushed"] = True
            log("✅ GitHub 반영 완료 — Streamlit Cloud가 곧 자동 재배포됩니다")

            try:
                from automation.kakao_notify import send_daily_summary
                kakao_result = send_daily_summary(target_date)
                log(f"카카오톡 알림 발송: {kakao_result}")
                status["kakao_sent"] = True
            except Exception as exc:
                log(f"⚠️ 카카오톡 알림 발송 실패 (동기화 자체는 정상 완료됨): {exc}")
        else:
            log("❌ push 실패 — 수동으로 'git push' 한번 실행해서 인증을 확인해보세요")
    finally:
        _write_status(status)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # bat의 stdout 리다이렉트를 없앴으니, 여기서 처리 안 된 예외가 나면
        # 콘솔에만 찍히고 아무 데도 안 남을 수 있다 — log()로 한 번 더 남긴다.
        import traceback
        log("❌ 처리되지 않은 예외로 중단됨:\n" + traceback.format_exc())
        raise
