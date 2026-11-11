"""운영진 로컬 채점기(competition/grader·harness)를 고치지 않고 그대로 불러 쓴다."""

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

COMPETITION = Path(__file__).resolve().parents[2] / "competition"
KB_DIR = COMPETITION / "kb"
PUBLIC_DIR = COMPETITION / "public"
# 운영진 score_local.py처럼 맨 앞에 넣는다(같은 이름의 다른 모듈이 끼어들지 않게).
# 두 폴더에는 judge·scoring·score_local 세 모듈뿐이다.
for _path in (COMPETITION / "grader", COMPETITION / "harness"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import judge as _judge  # noqa: E402  운영진 판정기
import score_local  # noqa: E402  운영진 로컬 채점기
import scoring  # noqa: E402  운영진 채점 규칙

__all__ = ["KB_DIR", "PUBLIC_DIR", "judge_from_env", "score_local", "scoring"]


def judge_from_env() -> Callable[[str, dict[str, Any]], dict[str, Any]] | None:
    """로컬 채점기와 같은 규칙: OPENAI_BASE_URL·OPENAI_MODEL·OPENAI_API_KEY가 모두 있으면 판정기."""
    env = {k: os.environ.get(f"OPENAI_{k.upper()}") for k in ("base_url", "model", "api_key")}
    return _judge.make_judge(**env) if all(env.values()) else None
