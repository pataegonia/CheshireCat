"""근거 확인 호출(#201 B, `toggles.oom_check`). 주 호출이 answer를 냈을 때만 부른다.

nano에 "마지막 질문이 묻는 것이 근거 묶음에 있는가"만 묻고, 없다(found=false)고 하면 부르는 쪽이
자료 밖 고정 문장(abstain)으로 바꾼다. 프롬프트·출력 칸은 #162 측정 도구(`harness.oom_probe`)와
같다(tutor/prompts/oom_check.md, 첫 줄 출처 주석은 보내지 않는다).

- 11/16 런박스 모의(교차 검증 160행, low): 주 호출 answer 108행 중 고침 8·망침 3(순이득 +5),
  잘림 0, 지연 p50 4.4·p95 7.4초. found=false만 바꾼다(quote 확인 안 됨까지 바꾸면 망침 24).
- 재시도 없음. 실패·잘림·시간 부족이면 주 호출 결과를 둔다. 건너뛴 사유는 횟수만 센다.
- 요청 내용은 지역 변수로만 쓴다(TU-18).
"""

import asyncio
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from tutor.judge.conversation import Conversation
from tutor.judge.main_call import _section_block, _turn_block

ROLE = "oom_check"  # TutorLLM.calls에 세는 역할 이름
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "oom_check.md"
EMPTY_BUNDLE = "(관련 자료를 찾지 못했습니다)"  # main_call.build_user_message와 같은 문구
OomSkips = Counter[str]  # no_time · error · truncated · invalid → 횟수


class OomCheckOut(BaseModel):
    """근거 확인 출력(json_schema strict, 필드 순서 = 생성 순서)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    asked: str
    quote: str
    found: bool


def load_system(path: Path = PROMPT_PATH) -> str:
    """system 문구. 첫 줄 출처 주석(<!-- … -->)은 빼고 나머지를 글자 그대로(앞뒤 빈 줄만 뺀다)."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    first, _, rest = text.partition("\n")
    if not (first.startswith("<!--") and first.endswith("-->")):
        raise ValueError(f"{path.name} 첫 줄에 출처 주석이 없다")
    return rest.strip("\n")


def build_messages(
    system: str, sections: Sequence[Any], conv: Conversation
) -> list[dict[str, str]]:
    """주 호출과 같은 근거 묶음·대화 창 두 블록만(신호·판단 예시는 넣지 않는다)."""
    bundle = "\n".join(_section_block(s) for s in sections) or EMPTY_BUNDLE
    user = f"<근거 묶음>\n{bundle}\n</근거 묶음>\n\n{_turn_block(conv)}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _skip(skips: OomSkips | None, reason: str) -> None:
    if skips is not None:
        skips[reason] += 1


async def material_absent(
    llm: Any,
    system: str,
    sections: Sequence[Any],
    conv: Conversation,
    *,
    deadline: float,
    timeout_s: float,
    skip_s: float,
    skips: OomSkips | None = None,
) -> bool:
    """근거 확인이 found=false를 냈으면 True. 실패·시간 부족·잘림이면 False(주 호출 결과 유지)."""
    left = deadline - asyncio.get_running_loop().time()
    if left < skip_s:
        _skip(skips, "no_time")
        return False
    messages = build_messages(system, sections, conv)
    try:
        result = await llm.complete(
            ROLE,
            messages,
            OomCheckOut,
            deadline=deadline,
            timeout_s=min(timeout_s, left),
            retries=0,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # 메시지는 남기지 않는다
        _skip(skips, "error")
        return False
    if result.finish_reason == "length":
        _skip(skips, "truncated")
        return False
    if result.error is not None or not isinstance(result.parsed, OomCheckOut):
        _skip(skips, "error" if result.error is not None else "invalid")
        return False
    return result.parsed.found is False
