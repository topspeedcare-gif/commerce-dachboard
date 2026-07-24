"""
assistant/bot.py — Slack 봇 진입점 (최소 골격)

명령어 로직은 여기 없다. commands_*.py 파일들을 import만 하면
그 안의 @command 데코레이터가 자동으로 dispatcher에 등록된다.

새 명령어 그룹을 추가하고 싶으면:
  1. assistant/commands_xxx.py 새 파일 생성
  2. @command(...) 데코레이터로 함수 작성
  3. 아래 import 목록에 한 줄 추가
그게 전부 — 다른 파일은 절대 안 건드림.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 명령어 그룹들 — 새 그룹 추가 시 여기에 한 줄만 추가
from assistant import commands_coupang  # noqa: F401
from assistant import commands_inventory  # noqa: F401

from assistant.dispatcher import dispatch, registered_commands

try:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
except ImportError:
    App = None  # 로컬에서 슬랙 없이 dispatcher 테스트할 수 있도록 허용


def start_app() -> None:
    if App is None:
        raise RuntimeError("slack_bolt 미설치: pip install slack_bolt --break-system-packages")

    app = App(token=os.getenv("SLACK_BOT_TOKEN"))

    @app.event("message")
    def handle_message(event: dict, client) -> None:
        text = (event.get("text") or "").strip()
        channel = event.get("channel")
        thread_ts = event.get("ts")

        result = dispatch(text, ctx={"event": event})
        if result is None:
            return  # 등록된 명령어가 아니면 조용히 무시 (기존처럼 거대 폴백 로직 없음)

        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=result)

    print(f"등록된 명령어 {len(registered_commands())}개:", registered_commands())
    SocketModeHandler(app, os.getenv("SLACK_APP_TOKEN")).start()


if __name__ == "__main__":
    start_app()
