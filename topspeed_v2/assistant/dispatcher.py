"""
assistant/dispatcher.py — 명령어 dispatcher

기존 slack_app.py의 process_event()는 1,500줄짜리 if/elif 나열이라
명령어를 추가할 때마다 전체 함수를 다시 봐야 했고, 그래서
"쿠팡 전체 동기화" 같은 명령어가 중복 등록되는 버그가 생겼음.

이 패턴은 그 문제를 구조적으로 없앤다:
  - 명령어 하나 = 함수 하나 (@command 데코레이터로 등록)
  - 새 명령어를 추가할 때 다른 명령어 코드를 볼 필요가 전혀 없음
  - 같은 트리거를 두 번 등록하면 즉시 에러로 알려줌 (조용히 묻히지 않음)
"""
from __future__ import annotations

from typing import Callable

Handler = Callable[[dict], str]

_REGISTRY: dict[str, Handler] = {}


def command(*triggers: str):
    """
    사용 예:
        @command("쿠팡 전체 동기화", "쿠팡 동기화", "쿠팡 싱크")
        def handle_coupang_sync(ctx: dict) -> str:
            return "동기화 결과 텍스트"
    """
    def decorator(func: Handler) -> Handler:
        for trigger in triggers:
            normalized = trigger.strip()
            if normalized in _REGISTRY:
                raise ValueError(
                    f"중복 명령어 등록: '{normalized}' 는 이미 "
                    f"{_REGISTRY[normalized].__name__} 에 등록되어 있습니다. "
                    f"({func.__name__} 에서 다시 등록하려 했습니다)"
                )
            _REGISTRY[normalized] = func
        return func
    return decorator


def dispatch(text: str, ctx: dict) -> str | None:
    """정확히 일치하는 명령어가 있으면 실행, 없으면 None 반환."""
    handler = _REGISTRY.get(text.strip())
    if handler is None:
        return None
    return handler(ctx)


def registered_commands() -> list[str]:
    return sorted(_REGISTRY.keys())
