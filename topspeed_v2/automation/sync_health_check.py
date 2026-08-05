"""
automation/sync_health_check.py — "동기화가 진짜 됐는지" 확인만 하는 별도 점검

automation/Daily_sync.py(본 동기화)와는 별개로, Windows 작업 스케줄러에
**두 번째 작업**으로 등록해서 하루 중 다른 시각(예: Daily_sync.py가 새벽에
도는 것과 별개로 오전 9~10시쯤)에 한 번 더 돌리는 용도다.

Daily_sync.py가 도는 그 PC가 꺼져 있으면 이 스크립트도 당연히 같이 못
돈다 — 그건 이 스크립트로는 못 잡는다. 대신:
  - PC는 켜져 있었는데 Daily_sync.py가 중간에 실패한 경우
  - Daily_sync.py는 끝까지 돌았는데 daily_metrics에 뭔가 안 들어간 경우
를 Daily_sync.py 로그와 무관하게, DB를 직접 열어서 한 번 더 확인한다.
(PC가 꺼져있어서 이 스크립트조차 못 돈 날은, dashboard/web_app.py 쪽의
같은 점검이 "대시보드를 열 때" 잡아준다 — 배포된 Streamlit Cloud는
PC 전원과 무관하게 항상 켜져 있다.)

문제가 없으면 조용히 끝난다(카톡 안 보냄). 등록 방법은 README 참고
또는 아래 명령을 그대로 작업 스케줄러에 등록하면 된다:

    schtasks /create /tn "TOPSPEED 동기화 점검" /tr "\"<python.exe 경로>\" \"<이 파일 경로>\"" /sc daily /st 10:00
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    from dashboard.sync_health import check_and_alert

    result = check_and_alert(force=True)
    print(f"[{result['checked_date']}] ok={result['ok']} alerted={result['alerted']} reason={result.get('reason')}")
    if result.get("message"):
        print(result["message"])


if __name__ == "__main__":
    main()
