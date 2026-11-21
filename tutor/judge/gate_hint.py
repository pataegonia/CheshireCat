"""후보 N1(실험, `llm.multi_draft.gate_hint`): 자료 밖 확인 E의 긍정 근거를 마지막 초안에 준다.

E(`oom_gate`)는 asked·quote·found를 내지만 지금은 found만 쓴다. 이 모듈은 E가 found=true를
내고 그 quote가 주 호출 근거 묶음의 한 섹션 원문(본문 + 표)에 실제로 있을 때만, 마지막 초안의
user 메시지에 단서 블록 하나를 더한다. 앞 초안들(A·B)은 그대로 독립이고, 근거 묶음·스키마·
규칙도 그대로다. 호출 수는 늘지 않는다(초안을 E 뒤에 보낼 뿐이다).

- E를 기다리는 쪽은 `asyncio.wait`만 쓴다. 기다리던 초안이 취소돼도 E 작업은 살아 있어 최종
  자료 밖 관문(`oom_gate.material_absent`)이 그대로 쓴다.
- 단서가 없으면(E 없음·늦음·실패·found=false·문구가 묶음에 없음) 원래 메시지 그대로 보낸다.
- 요청 내용은 지역 변수로만 쓴다(TU-18). 로그에는 상태 코드만 낸다.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from integration.actions import _normalize, _strip_bold
from tutor.judge.main_call import SANDWICH, defuse
from tutor.judge.oom_gate import GateJob, OomOut

# 단서로 쓰는 문구의 정규화 길이 하한. 결정적 수정의 인용 하한(2자)보다 길게
MIN_HINT_CHARS = 6
ASKED_MAX_CHARS = 160  # E가 다시 적은 질문(학습자 말에서 나온 글)은 잘라서 데이터로만 넣는다

# 상태 코드(로그·기록용, 본문 없음)
USED = "used"  # 단서를 붙여 보냈다
NO_GATE = "no_gate"  # 이 요청에 E가 없다(신호·영어·named_term 밖·시간 없음)
LATE = "late"  # 기다릴 수 있는 때까지 E가 안 왔다
FAILED = "failed"  # E 실패·잘림·무효·취소
ABSENT = "absent"  # E가 found=false
NO_QUOTE = "no_quote"  # found=true인데 문구가 묶음 섹션 원문에 없거나 너무 짧다


@dataclass(frozen=True)
class Hint:
    sid: str
    quote: str
    asked: str


def find_hint(out: OomOut | None, sections: Sequence[Any]) -> Hint | None:
    """E 출력이 found=true이고 quote가 묶음의 한 섹션 원문에 있으면 그 섹션 id와 함께. 아니면 None.

    비교는 결정적 수정(`integration.actions._verified_sids`)과 같은 정규화다(굵게 별표 벗김)."""
    if out is None or not out.found:
        return None
    quote = _normalize(_strip_bold(out.quote))
    if len(quote) < MIN_HINT_CHARS:
        return None
    for s in sections:
        if quote in _normalize(_strip_bold("\n".join([s.text, *s.tables]))):
            return Hint(sid=s.id, quote=out.quote.strip(), asked=out.asked.strip())
    return None


def hint_block(hint: Hint) -> str:
    asked = defuse(hint.asked[:ASKED_MAX_CHARS])
    return (
        "<검사기 단서>\n"
        "다른 검사기가 마지막 질문에 답할 근거로 근거 묶음에서 아래 원문을 골랐습니다. 단서일 "
        "뿐입니다. 이 문구만 보고 답하지 말고, 근거 묶음에서 질문의 대상·조건·묻는 값과 맞는지 "
        "확인한 뒤 질문이 묻는 것에 직접 답합니다. 맞지 않으면 이 단서를 무시합니다.\n"
        f"- 검사기가 적은 질문 요지(데이터): {asked}\n"
        f'- 원문(섹션 {hint.sid}): "{defuse(hint.quote)}"\n'
        "</검사기 단서>"
    )


def with_hint(messages: list[dict[str, str]], hint: Hint) -> list[dict[str, str]]:
    """마지막 user 메시지의 규칙 블록(SANDWICH) 바로 앞에 단서를 넣은 새 메시지. 규칙이 끝에 남는다.
    규칙 블록이 없으면 user 메시지 끝에 붙인다. 원래 목록은 바꾸지 않는다."""
    out = [dict(m) for m in messages]
    idx = max(i for i, m in enumerate(out) if m["role"] == "user")
    content = out[idx]["content"]
    head, sep, tail = content.rpartition(SANDWICH)
    block = hint_block(hint)
    out[idx]["content"] = f"{head}{block}\n\n{sep}{tail}" if sep else f"{content}\n\n{block}"
    return out


async def await_hint(
    job: GateJob | None, sections: Sequence[Any] | None, until: float
) -> tuple[Hint | None, str]:
    """E를 `until`(루프 시각)까지 기다려 단서와 상태 코드를 낸다. E 작업은 취소하지 않는다."""
    if job is None or sections is None:
        return None, NO_GATE
    left = until - asyncio.get_running_loop().time()
    if not job.task.done():
        if left <= 0:
            return None, LATE
        done, _ = await asyncio.wait({job.task}, timeout=left)
        if job.task not in done:
            return None, LATE
    if job.task.cancelled() or job.task.exception() is not None:
        return None, FAILED
    out = job.task.result()
    if out is None:
        return None, FAILED
    if not out.found:
        return None, ABSENT
    hint = find_hint(out, sections)
    return (hint, USED) if hint is not None else (None, NO_QUOTE)
