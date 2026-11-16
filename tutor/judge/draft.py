"""주 호출 경로의 앞부분(PRD §4 ③·⑤): 검색 → 주 호출. 결정적 수정·후처리 전의 초안을 낸다.

`pipeline._answer`(A 경로)는 #107(설정·LLM 호출기)·#108(베이스라인 분기)이 머지된 뒤 이 함수를
부르고, 결과를 통합 결정적 수정(IN-04, #106)과 튜터 후처리(TU-07, #76)에 넘긴다.

- 검색은 튜터 B의 `tutor.retrieval.retrieve`(TU-03, #70)를 쓴다. 질의는 판단 대상, 이력은 창 안의
  다른 학습자 발화(버튼 문구 제외, TU-02)다.
- 단원 닻(`anchor_unit`, TU-03)을 켜면 검색 상위 k에서 앞 대화 단원의 섹션을 앞으로 옮긴다.
  근거 묶음·IN-04의 1위·`cite_top1`의 1위가 모두 이 순서를 쓴다.
- 주 호출은 `run_main_call`(TU-06). 실패면 `out=None`이고 부르는 쪽이 TU-12 기본값을 낸다.
- 요청 내용은 이 함수의 지역 변수로만 쓰고 남기지 않는다(TU-18).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tutor.judge.conversation import Conversation
from tutor.judge.main_call import Complete, MainCallOut, MainCallPrompt, run_main_call
from tutor.retrieval import KbIndex, Section, anchor_first, find_anchor, retrieve


@dataclass(frozen=True)
class Draft:
    # 근거 묶음(검색 순위 순, 닻을 켜면 닻 단원 먼저). IN-04가 남은 claim 0일 때 1위를 쓴다
    sections: tuple[Section, ...]
    out: MainCallOut | None  # None이면 주 호출 실패


async def draft_answer(
    conv: Conversation,
    *,
    prompt: MainCallPrompt,
    complete: Complete,
    deadline: float,
    k: int,
    signals: Sequence[str] = (),
    embedding: Any = None,
    examples: str = "",
    index: KbIndex | None = None,
    anchor_unit: bool = False,
) -> Draft:
    """검색 상위 k개를 근거 묶음으로 주 호출을 한 번 부른다.

    `embedding`은 기동 때 만든 임베딩 검색(TU-11, `state.tutor_emb`, #124)이다. 있을 때만
    `retrieve(…, embedding=)`로 넘기고, 없으면(토글 끔·기동 실패) BM25만 쓴다.
    `examples`는 판단 예시 블록(TU-08)이다. 비었으면 넣지 않는다.
    `index`는 기동 때 만든 KB 색인(`state.tutor_kb`)이다. 있을 때만 `retrieve(…, index=)`로 넘긴다.
    `anchor_unit`(`toggles.anchor_unit`)이 참이고 앞 대화가 있으면 같은 색인으로 BM25 검색을 한 번
    더 해 닻 단원을 정하고 상위 k를 다시 줄 세운다. 거짓이면 검색은 한 번뿐이다.
    """
    query, history = conv.search_query()
    extra = {
        name: value
        for name, value in (("embedding", embedding), ("index", index))
        if value is not None
    }
    sections = tuple(await retrieve(query, history, k=k, **extra))
    earlier = conv.anchor_turns() if anchor_unit and len(sections) > 1 else ()
    if earlier:  # 섹션이 1개 이하면 순서가 바뀔 수 없어 닻을 찾지 않는다
        sections = anchor_first(sections, await find_anchor(earlier, index=index))
    out = await run_main_call(
        complete, prompt, conv, sections, signals, deadline=deadline, examples=examples
    )
    return Draft(sections=sections, out=out)
