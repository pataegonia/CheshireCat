"""운영진 로컬 채점기(competition/grader·harness)를 고치지 않고 그대로 불러 쓴다."""

import json
import os
import statistics
import sys
import urllib.request
from collections import Counter
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


GRADER_MAX_TOKENS = 4000  # 운영진 judge.make_judge가 보내는 max_completion_tokens


def judge_from_env(
    effort: str | None = None, max_tokens: int | None = None, repeat: int = 1
) -> Callable[[str, dict[str, Any]], dict[str, Any]] | None:
    """로컬 채점기와 같은 규칙: OPENAI_BASE_URL·OPENAI_MODEL·OPENAI_API_KEY가 모두 있으면 판정기.

    셋 다 기본값이면 운영진 판정기 그대로(`judge.make_judge`: reasoning_effort를 보내지 않아 모델
    기본값, max_completion_tokens 4000). 하나라도 주면 `tuned_judge`(같은 프롬프트·요청에
    reasoning_effort·상한만 바꾸고, repeat번 물어 다수결)."""
    env = {k: os.environ.get(f"OPENAI_{k.upper()}") for k in ("base_url", "model", "api_key")}
    if not all(env.values()):
        return None
    if effort is None and max_tokens is None and repeat == 1:
        return _judge.make_judge(**env)
    return tuned_judge(
        **env,  # type: ignore[arg-type]
        effort=effort,
        max_tokens=max_tokens or GRADER_MAX_TOKENS,
        repeat=repeat,
    )


def _stream(resp: Any) -> tuple[str, str | None]:
    """운영진 `judge._content`와 같이 읽되 finish_reason(잘림 length)도 돌려준다."""
    if "text/event-stream" not in (resp.headers.get("Content-Type") or ""):
        choice = json.loads(resp.read())["choices"][0]
        return choice["message"]["content"], choice.get("finish_reason")
    parts: list[str] = []
    finish = None
    for raw in resp:
        line = raw.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        for choice in json.loads(data).get("choices") or []:
            parts.append((choice.get("delta") or {}).get("content") or "")
            finish = choice.get("finish_reason") or finish
    return "".join(parts), finish


def tuned_judge(
    base_url: str,
    model: str,
    api_key: str,
    *,
    effort: str | None,
    max_tokens: int,
    repeat: int = 1,
    timeout: int = 60,
    retries: int = 2,
) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """운영진 판정기와 같은 SYSTEM·PROMPTS·요청 모양·재시도(판정 한 번에 retries+1번까지)에
    reasoning_effort(None이면 보내지 않음)·max_completion_tokens만 바꾼다. repeat번 판정해 다수결:
    참/거짓은 과반(동률이면 첫 판정), facts 점수는 중앙값. 하나도 못 얻으면 JudgeError.
    `judge.stats`(Counter)에 판정 시도 수(`attempts`)와 잘린 시도 수(`length_<kind>`)를 센다."""
    url = base_url.rstrip("/") + "/chat/completions"
    stats: Counter[str] = Counter()

    def once(kind: str, payload: dict[str, Any]) -> Any:
        body: dict[str, Any] = {
            "model": model,
            "stream": True,
            "max_completion_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": f"{_judge.SYSTEM}\n{_judge.PROMPTS[kind]}"},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        if effort is not None:
            body["reasoning_effort"] = effort
        req = urllib.request.Request(  # noqa: S310 - 주소는 OPENAI_BASE_URL(운영진 판정기와 같다)
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {api_key}",
            },
        )
        for _ in range(retries + 1):
            stats["attempts"] += 1
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                    content, finish = _stream(resp)
                if finish == "length":
                    stats[f"length_{kind}"] += 1
                value = json.loads(content).get(_judge.FIELD[kind])
                if _judge._valid(kind, value):
                    return value
            except Exception:  # noqa: BLE001, S112 - 운영진 판정기와 같이 다음 시도로
                continue
        return None

    def judge(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        values = [v for v in (once(kind, payload) for _ in range(repeat)) if v is not None]
        if not values:
            raise scoring.JudgeError(f"판정 실패({kind})")
        if kind == "facts":
            value: Any = statistics.median(values)
        else:
            yes = sum(bool(v) for v in values)
            value = values[0] if yes * 2 == len(values) else yes * 2 > len(values)
        return {_judge.FIELD[kind]: value}

    judge.stats = stats  # type: ignore[attr-defined]
    return judge
