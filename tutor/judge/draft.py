"""주 호출 경로의 앞부분(PRD §4 ③·⑤): 검색 → 주 호출. 결정적 수정·후처리 전의 초안을 낸다.

`pipeline._answer`(A 경로)는 #107(설정·LLM 호출기)·#108(베이스라인 분기)이 머지된 뒤 이 함수를
부르고, 결과를 통합 결정적 수정(IN-04, #106)과 튜터 후처리(TU-07, #76)에 넘긴다.

- 검색은 튜터 B의 `tutor.retrieval.retrieve`(TU-03, #70)를 쓴다. 질의는 판단 대상, 이력은 창 안의
  다른 학습자 발화(버튼 문구 제외, TU-02)다.
- 주 호출은 `run_main_call`(TU-06). 실패면 `out=None`이고 부르는 쪽이 TU-12 기본값을 낸다.
- 요청 내용은 이 함수의 지역 변수로만 쓰고 남기지 않는다(TU-18).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tutor.judge.conversation import Conversation
from tutor.judge.main_call import Complete, MainCallOut, MainCallPrompt, run_main_call
from tutor.retrieval import KbIndex, Section, retrieve


@dataclass(frozen=True)
class Draft:
    sections: tuple[Section, ...]  # 근거 묶음(검색 순위 순). IN-04가 남은 claim 0일 때 1위를 쓴다
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
) -> Draft:
    """검색 상위 k개를 근거 묶음으로 주 호출을 한 번 부른다.

    `embedding`은 기동 때 만든 임베딩 검색(TU-11, `state.tutor_emb`, #124)이다. 있을 때만
    `retrieve(…, embedding=)`로 넘기고, 없으면(토글 끔·기동 실패) BM25만 쓴다.
    `examples`는 판단 예시 블록(TU-08)이다. 비었으면 넣지 않는다.
    `index`는 기동 때 만든 KB 색인(`state.tutor_kb`)이다. 있을 때만 `retrieve(…, index=)`로 넘긴다.
    """
    query, history = conv.search_query()
    extra = {
        name: value
        for name, value in (("embedding", embedding), ("index", index))
        if value is not None
    }
    sections = tuple(await retrieve(query, history, k=k, **extra))
    out = await run_main_call(
        complete, prompt, conv, sections, signals, deadline=deadline, examples=examples
    )
    return Draft(sections=sections, out=out)
