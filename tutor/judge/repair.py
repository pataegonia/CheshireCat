"""근거 불일치 답변의 선택적 재작성(`toggles.repair`, 2026-11-18 후보 D). nano 1회, 재시도 없음.

`cite_top1`이 모델 인용에 없던 검색 1위 섹션을 더한 최종 answer만 다룬다. 그 섹션을 근거 묶음에
더해(`추가="예"`) 초안 문장·근거와 함께 보내고, 같은 스키마(`MainCallOut`)로 고쳐 쓴 답을 받는다.
범주·action이 초안과 다르거나, claim 문장의 인용 문구가 섹션 원문에 없거나, 호출이 실패하면
None이라 부르는 쪽이 초안 답을 그대로 낸다.
고쳐 쓴 답도 결정적 수정(IN-04)·후처리를 그대로 거친다. 요청 내용은 지역 변수로만 쓴다(TU-18).
"""

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tutor.judge.conversation import Conversation
from tutor.judge.main_call import MainCallOut, _section_block, _turn_block, clean, defuse

ROLE = "repair"  # TutorLLM.calls에 세는 역할 이름
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "repair.md"
TIMEOUT_S = 10.0  # 호출 하나의 상한(11/18 8초에서 25회 중 18회 시간 초과)
MIN_LEFT_S = 4.0  # 요청 마감까지 이보다 적게 남았으면 부르지 않는다
_NOT_WORD = re.compile(r"[^0-9A-Za-z가-힣]")


def _squash(text: str) -> str:
    return _NOT_WORD.sub("", text).lower()


def grounded(out: MainCallOut, sections: Sequence[Any]) -> bool:
    """claim 문장마다 근거 하나 이상이 묶음 안 섹션 원문(글자·숫자만 비교)에 실제로 있는가."""
    texts = {s.id: _squash(s.text + "".join(getattr(s, "tables", ()) or ())) for s in sections}
    for sentence in out.sentences:
        if sentence.kind != "claim":
            continue
        if not any(
            e.sid in texts and _squash(e.quote) and _squash(e.quote) in texts[e.sid]
            for e in sentence.evidence
        ):
            return False
    return True


def load_system(path: Path = PROMPT_PATH) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    first, _, rest = text.partition("\n")
    if not (first.startswith("<!--") and first.endswith("-->")):
        raise ValueError(f"{path.name} 첫 줄에 출처 주석이 없다")
    return rest.strip("\n")


def _draft_block(out: MainCallOut) -> str:
    lines = [f'<초안 category="{out.category}" action="{out.action}">']
    for s in out.sentences:
        ev = "; ".join(f"{e.sid}: {defuse(e.quote)}" for e in s.evidence) or "없음"
        lines.append(f'<문장 kind="{s.kind}" 근거="{ev}">{defuse(s.text)}</문장>')
    lines.append("</초안>")
    return "\n".join(lines)


def build_messages(
    system: str, sections: Sequence[Any], added: Any, out: MainCallOut, conv: Conversation
) -> list[dict[str, str]]:
    cited = {e.sid for s in out.sentences for e in s.evidence}
    used = [s for s in sections if s.id in cited and s.id != added.id] or list(sections[:1])
    blocks = [_section_block(s) for s in used]  # 입력을 줄인다: 초안이 인용한 섹션 + 더한 1위
    extra = _section_block(added).replace("<섹션 id=", '<섹션 추가="예" id=', 1)
    bundle = "\n".join([*blocks, extra])
    user = f"<근거 묶음>\n{bundle}\n</근거 묶음>\n\n{_draft_block(out)}\n\n{_turn_block(conv)}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def rewrite(
    llm: Any,
    system: str,
    sections: Sequence[Any],
    added: Any,
    out: MainCallOut,
    conv: Conversation,
    *,
    deadline: float,
    now: float,
) -> MainCallOut | None:
    """고쳐 쓴 주 호출 출력. 시간이 모자라거나 실패·범주/action 변경이면 None."""
    left = deadline - now
    if left < MIN_LEFT_S:
        return None
    result = await llm.complete(
        ROLE,
        build_messages(system, sections, added, out, conv),
        MainCallOut,
        deadline=deadline,
        timeout_s=min(TIMEOUT_S, left - 1.0),
        retries=0,
    )
    if result.error is not None or not isinstance(result.parsed, MainCallOut):
        return None
    if getattr(result, "finish_reason", None) == "length":
        return None
    fixed = clean(result.parsed)
    if fixed.category != out.category or fixed.action != out.action or not fixed.sentences:
        return None
    return fixed if grounded(fixed, [*sections, added]) else None
