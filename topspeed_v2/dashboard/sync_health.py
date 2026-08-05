"""
dashboard/sync_health.py — 동기화가 실제로 됐는지 확인하는 안전장치

automation/Daily_sync.py가 매일 돌긴 하지만, PC가 꺼져있거나 쿠팡 API가
막히거나 git push가 실패하면 daily_metrics에 어제 데이터가 아예 안 들어올
수 있다. "로그를 믿는" 대신 "실제 DB에 어제 날짜 데이터가 있는지"를 직접
확인해서, 없으면 카카오톡으로 알린다. Daily_sync.py가 남긴 상태 파일
(automation/last_sync_status.json)이 있으면, "로컬엔 동기화됐는데 git
push만 실패해서 배포된 대시보드엔 안 보이는" 경우도 구분해서 알린다.

두 군데서 호출된다:
  1. dashboard/web_app.py — 대시보드를 열 때마다 조용히 한 번 확인
  2. automation/sync_health_check.py — Windows 작업 스케줄러로 별도 시각에
     한 번 더 확인 (Daily_sync.py 자체가 도는 PC가 꺼져 있으면 이것도 같이
     못 돌지만, Daily_sync.py는 돌았는데 중간에 실패한 경우는 이걸로 잡힌다)

같은 날 카톡은 한 번만 보낸다(settings 테이블에 발송 기록) — 대시보드를
여러 번 열어도 반복 발송되지 않게.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from shared.db import get_setting, set_setting, get_conn

KST = ZoneInfo("Asia/Seoul")
LAST_SYNC_STATUS_PATH = Path(__file__).resolve().parents[1] / "automation" / "last_sync_status.json"

DEFAULT_MIN_HOUR_KST = 9  # 이 시각 전엔 "아직 안 들어왔다"고 판단하지 않는다 (Daily_sync.py가 보통 새벽~아침에 돎)


def yesterday_kst() -> str:
    return str(datetime.now(KST).date() - timedelta(days=1))


def has_data_for(target_date: str) -> bool:
    """daily_metrics에 해당 날짜 행이 하나라도 있으면 True."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM daily_metrics WHERE date = ? LIMIT 1", (target_date,)
        ).fetchone()
    return row is not None


def read_last_sync_status() -> dict | None:
    """Daily_sync.py가 실행할 때마다 남기는 상태 파일. 없으면(한 번도 안 돌았으면) None."""
    if not LAST_SYNC_STATUS_PATH.exists():
        return None
    try:
        return json.loads(LAST_SYNC_STATUS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def check_and_alert(min_hour_kst: int = DEFAULT_MIN_HOUR_KST, force: bool = False) -> dict:
    """
    어제 날짜 데이터가 없거나(동기화 자체 실패), 데이터는 있는데 git push가
    안 돼서 배포된 대시보드엔 안 보이는 상태면 카카오톡으로 알린다.

    min_hour_kst: 이 시각(KST) 이전에는 확인하지 않는다. force=True면 시각 무시.

    반환: {"ok": bool, "checked_date": str, "alerted": bool, "reason": str|None, "message": str|None}
    ok=False는 "문제를 발견했다"는 뜻이지 함수 실행 실패가 아니다.
    """
    target = yesterday_kst()
    now = datetime.now(KST)

    if not force and now.hour < min_hour_kst:
        return {"ok": True, "checked_date": target, "alerted": False, "reason": "too_early", "message": None}

    d = datetime.strptime(target, "%Y-%m-%d")
    label = f"{d.month}/{d.day}"
    status = read_last_sync_status()

    if not has_data_for(target):
        reason = "no_data"
        message = (
            f"⚠️ 어제({label}) 데이터가 아직 동기화되지 않았어요. "
            "PC가 꺼져있었는지, 자동 동기화가 실패하지 않았는지 확인해주세요."
        )
    elif status and status.get("target_date") == target and status.get("sync_ok") and not status.get("git_pushed"):
        # 로컬 DB엔 데이터가 들어왔지만 GitHub에 반영이 안 됐다 — Streamlit Cloud
        # 대시보드는 git push로만 갱신되므로, 실제로는 배포된 화면에 어제 데이터가 안 보인다.
        reason = "git_push_failed"
        message = (
            f"⚠️ 어제({label}) 데이터는 PC에서 동기화됐지만 GitHub에는 반영이 안 됐어요 — "
            "배포된 대시보드엔 아직 안 보일 수 있어요. PC에서 git push 상태를 확인해주세요."
        )
    else:
        return {"ok": True, "checked_date": target, "alerted": False, "reason": None, "message": None}

    # message는 화면 배너용으로 항상 채워둔다 — 카톡 발송 여부(dedup)와 별개다.
    alert_key = f"sync_health_alerted:{target}"
    if get_setting(alert_key):
        return {"ok": False, "checked_date": target, "alerted": False, "reason": reason, "message": message}

    try:
        from automation.kakao_notify import send_kakao_message
        send_kakao_message(message)
        set_setting(alert_key, now.isoformat())
        return {"ok": False, "checked_date": target, "alerted": True, "reason": reason, "message": message}
    except Exception as exc:
        return {
            "ok": False, "checked_date": target, "alerted": False, "reason": reason,
            "message": f"{message} (카톡 발송 실패: {exc})",
        }


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:
        pass

    print(check_and_alert(force=True))
