"""두 초안 중 고르기 J(후보 J, `toggles.draft_select`).

주 호출(A)과 같은 근거 묶음·대화로 두 번째 초안(B)을 동시에 만들고, 둘 다 answer이고 범주가
같으며 문장이 다르면 nano 선택기(J) 1회가 근거와 대조해 더 나은 쪽을 고른다. 새 답을 합성하지
않는다. 고른 초안은 기존 흐름(대행 보정·T 덮기·E·인용·후처리)을 그대로 지난다.

- B: 주 호출과 같은 정책·출력 스키마. system 프롬프트 끝에 작성 보충 지시(`prompts/draft_b.md`)만
  덧붙인다. 재시도·겹쳐 보내기 없음(A는 지금 그대로 재시도·겹쳐 보내기를 한다).
- A가 끝난 뒤 B를 `B_WAIT_S`초까지만 더 기다린다(J를 부를 시간을 남긴다). 늦은 B는 취소한다.
- A가 실패했는데 B가 성공했으면 B를 쓴다(주 호출 실패 기본값보다 B가 낫다).
- J는 요청 마감까지 `J_MIN_LEFT_S`초보다 많이 남았을 때만 부른다. 실패·잘림·시간 초과·tie면 A.
- action·범주가 다른 두 초안은 고르지 않는다(A). 고르는 일로 거절을 answer로 바꾸지 않는다.
- 요청 내용은 지역 변수로만 쓰고 남기지 않는다(TU-18). 갈래별 횟수만 센다.
"""

import asyncio
import dataclasses
import logging
from collections import Counter
from collections.abc import Awaitable, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from tutor.judge.conversation import Conversation
from tutor.judge.main_call import MainCallOut, MainCallPrompt, _section_block, _turn_block, defuse

# 가드 접근 로그(guardrail.access, INFO)의 하위 로거: 서버 로그에 "draft_select <갈래>" 한 줄을
# 남긴다. 요청 내용은 넣지 않는다(SPEC §8). "/tutor" 글자를 넣지 않아 요청 수 세기와 섞이지 않는다
log = logging.getLogger("guardrail.access.draft_select")
PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"
B_PROMPT_PATH = PROMPT_DIR / "draft_b.md"
J_PROMPT_PATH = PROMPT_DIR / "draft_select.md"
ROLE_B = "main_b"  # TutorLLM.calls에 세는 역할 이름(두 번째 초안)
ROLE_J = "select_draft"  # 선택기
B_WAIT_S = 3.0  # A가 끝난 뒤 B를 더 기다리는 최대 시간
J_MIN_LEFT_S = 4.0  # 요청 마감까지 이보다 덜 남았으면 J를 부르지 않는다
J_MARGIN_S = 1.0  # J 마감을 요청 마감보다 이만큼 앞에 둔다(뒤 단계·후처리 몫)
EMPTY_BUNDLE = "(관련 자료를 찾지 못했습니다)"


class SelectOut(BaseModel):
    """J 출력(json_schema strict)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    better: Literal["A", "B", "tie"]


class DraftCounts(Counter[str]):
    """갈래 → 횟수(본문 없음). 키: b_rescue·b_missing·not_both_answer·category_differs·same_text·
    no_time·j_failed·j_A·j_B·j_tie·none."""


def load_prompt(path: Path) -> str:
    """첫 줄 출처 주석(<!-- … -->)을 뺀 나머지(앞뒤 빈 줄만 뺀다)."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    first, _, rest = text.partition("\n")
    if not (first.startswith("<!--") and first.endswith("-->")):
        raise ValueError(f"{path.name} 첫 줄에 출처 주석이 없다")
    return rest.strip("\n")


def prompt_b(prompt: MainCallPrompt, addendum: str) -> MainCallPrompt:
    """B의 주 호출 프롬프트: 같은 system 끝에 작성 보충 지시를 덧붙인다(스키마·버전 그대로)."""
    return dataclasses.replace(prompt, system=f"{prompt.system}\n\n{addendum}")


def _render(out: MainCallOut) -> str:
    lines = []
    for s in out.sentences:
        cites = "; ".join(f"{e.sid}: {e.quote}" for e in s.evidence)
        tail = f" [근거 {cites}]" if cites else ""
        lines.append(f"- {s.text}{tail}")
    return defuse("\n".join(lines))


def build_messages(
    system: str,
    sections: Sequence[Any],
    conv: Conversation,
    a: MainCallOut,
    b: MainCallOut,
) -> list[dict[str, str]]:
    """근거 묶음 → 대화 → 답변 A → 답변 B. 순서는 늘 같다(A가 기존 주 호출)."""
    bundle = "\n".join(_section_block(s) for s in sections) or EMPTY_BUNDLE
    user = "\n\n".join(
        [
            f"<근거 묶음>\n{bundle}\n</근거 묶음>",
            _turn_block(conv),
            f"<답변 A>\n{_render(a)}\n</답변 A>",
            f"<답변 B>\n{_render(b)}\n</답변 B>",
        ]
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _words(out: MainCallOut) -> str:
    return "".join("".join(s.text.split()) for s in out.sentences)


def not_comparable(a: MainCallOut, b: MainCallOut) -> str | None:
    """J로 고를 수 없는 까닭(없으면 None). 둘 다 answer·같은 범주·다른 문장이어야 고른다."""
    if a.action != "answer" or b.action != "answer":
        return "not_both_answer"
    if a.category != b.category:
        return "category_differs"
    if _words(a) == _words(b):
        return "same_text"
    return None


async def two_drafts(
    run_a: Awaitable[MainCallOut | None],
    run_b: Awaitable[MainCallOut | None],
    *,
    deadline: float,
) -> tuple[MainCallOut | None, MainCallOut | None]:
    """A와 B를 동시에 시작한다. A가 성공하면 B를 `B_WAIT_S`초(J 몫을 남긴 만큼)까지만 더 기다리고,
    A가 실패하면 B를 끝까지(B 자기 마감까지) 기다린다. 빠져나갈 때 남은 B는 취소한다."""
    b_task = asyncio.ensure_future(run_b)
    try:
        out_a = await run_a
        if not b_task.done():
            if out_a is None:
                await asyncio.wait({b_task})
            else:
                left = deadline - asyncio.get_running_loop().time() - J_MIN_LEFT_S
                await asyncio.wait({b_task}, timeout=max(0.0, min(B_WAIT_S, left)))
        out_b = None  # B의 예외는 A처럼 올리지 않는다(B 없음으로 본다)
        if b_task.done() and not b_task.cancelled() and b_task.exception() is None:
            out_b = b_task.result()
        return out_a, out_b
    finally:
        if not b_task.done():
            b_task.cancel()  # 요청이 끝난 뒤 남는 작업이 없게(TU-18)


async def choose(
    llm: Any,
    system: str,
    sections: Sequence[Any],
    conv: Conversation,
    out_a: MainCallOut | None,
    out_b: MainCallOut | None,
    *,
    timeout_s: float,
    deadline: float,
    counts: DraftCounts | None = None,
) -> MainCallOut | None:
    """쓸 초안. A가 기본이고, J가 B를 고른 때(또는 A가 실패하고 B가 있을 때)만 B."""

    def count(key: str) -> None:
        if counts is not None:
            counts[key] += 1
        log.info("draft_select %s", key)  # 갈래 이름만(본문 없음). 실제 제출에서 J 발동을 센다

    if out_a is None:
        count("b_rescue" if out_b is not None else "none")
        return out_b
    if out_b is None:
        count("b_missing")
        return out_a
    reason = not_comparable(out_a, out_b)
    if reason is not None:
        count(reason)
        return out_a
    now = asyncio.get_running_loop().time()
    left = deadline - now
    if left < J_MIN_LEFT_S:
        count("no_time")
        return out_a
    limit = min(timeout_s, left - J_MARGIN_S)
    result = await llm.complete(
        ROLE_J,
        build_messages(system, sections, conv, out_a, out_b),
        SelectOut,
        deadline=now + limit,
        timeout_s=limit,
        retries=0,
    )
    if result.error is not None or not isinstance(result.parsed, SelectOut):
        count("j_failed")
        return out_a
    if getattr(result, "finish_reason", None) == "length":
        count("j_failed")
        return out_a
    better = result.parsed.better
    count(f"j_{better}")
    return out_b if better == "B" else out_a
