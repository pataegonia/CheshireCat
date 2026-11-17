"""2차 자체 검증(`toggles.self_check`, 측정용). 주 호출이 normal answer를 냈을 때만 nano 1회.

nano에 "질문이 묻는 값·이름·조건이 초안에 자료 표현대로 들어 있는가"를 묻고, 빠졌으면
(covered=false) 고쳐 쓴 문장으로 초안 문장을 바꾼다. 범주·action은 바꾸지 않는다. 인용은 바뀐
문장으로 다시 계산한다.

- 대상: 11/17 동결 후보 판정 4회에서 normal 사실 손실이 수업 예시의 구체 값(정답 섹션은
  인용했는데 값이 빠짐)에 몰렸다. 프롬프트 한 줄(#192)은 −1.28로 기각돼, 검사 뒤 고쳐 쓰기를
  따로 잰다.
- 근거 범위: bundle은 주 호출과 같은 근거 묶음 전체, cited는 초안 claim이 인용한 섹션과
  검색 1위만.
- 고쳐 쓴 문장에 근거 범위 안 섹션을 인용한 claim이 하나도 없으면 초안을 둔다(자료 없는
  고쳐 쓰기 막기).
- 재시도 없음. 실패·잘림·시간 부족이면 초안을 둔다. 결과는 사유 횟수만 센다. 요청 내용은
  지역 변수로만 쓴다.
"""

import asyncio
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from tutor.judge.conversation import Conversation
from tutor.judge.main_call import SentenceOut, _section_block, _turn_block, defuse

ROLE = "self_check"  # TutorLLM.calls에 세는 역할 이름
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "self_check.md"
EMPTY_BUNDLE = "(관련 자료를 찾지 못했습니다)"  # main_call.build_user_message와 같은 문구
Scope = Literal["bundle", "cited"]
# kept · rewrote · no_time · error · truncated · invalid · no_evidence → 횟수
SelfCheckCounts = Counter[str]


class SelfCheckOut(BaseModel):
    """2차 자체 검증 출력(json_schema strict, 필드 순서 = 생성 순서)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    asked: str
    quote: str
    covered: bool
    sentences: list[SentenceOut]


def load_system(path: Path = PROMPT_PATH) -> str:
    """system 문구. 첫 줄 출처 주석(<!-- … -->)은 빼고 나머지를 글자 그대로(앞뒤 빈 줄만 뺀다)."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    first, _, rest = text.partition("\n")
    if not (first.startswith("<!--") and first.endswith("-->")):
        raise ValueError(f"{path.name} 첫 줄에 출처 주석이 없다")
    return rest.strip("\n")


def scoped_sections(
    sections: Sequence[Any], draft: Sequence[SentenceOut], scope: Scope
) -> list[Any]:
    """검증에 보일 섹션(검색 순서 유지). cited는 초안 claim이 인용한 섹션 + 검색 1위."""
    if scope == "bundle" or not sections:
        return list(sections)
    cited = {ev.sid for s in draft if s.kind == "claim" for ev in s.evidence}
    cited.add(sections[0].id)
    return [s for s in sections if s.id in cited]


def build_messages(
    system: str, sections: Sequence[Any], conv: Conversation, draft: Sequence[SentenceOut]
) -> list[dict[str, str]]:
    bundle = "\n".join(_section_block(s) for s in sections) or EMPTY_BUNDLE
    lines = "\n".join(f"- {defuse(s.text)}" for s in draft if s.text)
    user = f"<근거 묶음>\n{bundle}\n</근거 묶음>\n\n{_turn_block(conv)}\n\n<초안>\n{lines}\n</초안>"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _grounded(sentences: Sequence[SentenceOut], allowed: set[str]) -> bool:
    """근거 범위 안 섹션을 인용한 claim이 하나라도 있는가."""
    return any(s.kind == "claim" and any(ev.sid in allowed for ev in s.evidence) for s in sentences)


async def self_check(
    llm: Any,
    system: str,
    sections: Sequence[Any],
    conv: Conversation,
    draft: Sequence[SentenceOut],
    *,
    scope: Scope,
    deadline: float,
    timeout_s: float,
    skip_s: float,
    counts: SelfCheckCounts | None = None,
) -> tuple[str, list[SentenceOut] | None]:
    """(사유, 고쳐 쓴 문장 목록). 초안을 그대로 둘 때(covered·실패·시간 부족·근거 없는 고쳐 쓰기)는
    문장 목록이 None이다.

    사유: rewrote · kept · no_time · error · truncated · invalid · no_evidence.
    """

    def done(
        reason: str, sentences: list[SentenceOut] | None = None
    ) -> tuple[str, list[SentenceOut] | None]:
        if counts is not None:
            counts[reason] += 1
        return reason, sentences

    left = deadline - asyncio.get_running_loop().time()
    if left < skip_s:
        return done("no_time")
    shown = scoped_sections(sections, draft, scope)
    try:
        result = await llm.complete(
            ROLE,
            build_messages(system, shown, conv, draft),
            SelfCheckOut,
            deadline=deadline,
            timeout_s=min(timeout_s, left),
            retries=0,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # 메시지는 남기지 않는다
        return done("error")
    if result.finish_reason == "length":
        return done("truncated")
    if result.error is not None or not isinstance(result.parsed, SelfCheckOut):
        return done("error" if result.error is not None else "invalid")
    out = result.parsed
    if out.covered or not out.quote.strip() or not out.sentences:
        return done("kept")
    if not _grounded(out.sentences, {s.id for s in shown}):
        return done("no_evidence")
    return done("rewrote", list(out.sentences))
