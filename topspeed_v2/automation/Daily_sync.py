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
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_git(*args: str) -> tuple[int, str]:
    result = subprocess.run(
        [GIT_EXE, *args], cwd=ROOT, capture_output=True, text=True
    )
    return result.returncode, (result.stdout + result.stderr).strip()


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
    log(f"동기화 시작: {target_date}")

    result = run_sync(target_date, unit_costs={})
    log("동기화 결과:\n" + result)

    # DB 변경사항을 GitHub에 반영
    code, out = run_git("add", "shared/topspeed.db")
    log(f"git add: {out or 'OK'}")

    code, out = run_git("status", "--porcelain")
    if not out.strip():
        log("변경사항 없음 (동기화 결과가 이전과 동일) — commit 생략")
        return

    code, out = run_git("commit", "-m", f"auto sync {target_date}")
    log(f"git commit: {out}")
    if code != 0:
        log("❌ commit 실패 — 아래 push는 건너뜁니다")
        return

    code, out = run_git("push")
    log(f"git push: {out}")
    if code == 0:
        log("✅ GitHub 반영 완료 — Streamlit Cloud가 곧 자동 재배포됩니다")

        try:
            from automation.kakao_notify import send_daily_summary
            kakao_result = send_daily_summary(target_date)
            log(f"카카오톡 알림 발송: {kakao_result}")
        except Exception as exc:
            log(f"⚠️ 카카오톡 알림 발송 실패 (동기화 자체는 정상 완료됨): {exc}")
    else:
        log("❌ push 실패 — 수동으로 'git push' 한번 실행해서 인증을 확인해보세요")


if __name__ == "__main__":
    main()
