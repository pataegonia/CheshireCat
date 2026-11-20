"""주 호출 경로의 앞부분(PRD §4 ③·⑤): 검색 → 주 호출. 결정적 수정·후처리 전의 초안을 낸다.

`pipeline._answer`(A 경로)는 #107(설정·LLM 호출기)·#108(베이스라인 분기)이 머지된 뒤 이 함수를
부르고, 결과를 통합 결정적 수정(IN-04, #106)과 튜터 후처리(TU-07, #76)에 넘긴다.

- 검색은 튜터 B의 `tutor.retrieval.retrieve`(TU-03, #70)를 쓴다. 질의는 판단 대상, 이력은 창 안의
  다른 학습자 발화(버튼 문구 제외, TU-02)다.
- 주 호출은 `run_main_call`(TU-06). 실패면 `out=None`이고 부르는 쪽이 TU-12 기본값을 낸다.
- 요청 내용은 이 함수의 지역 변수로만 쓰고 남기지 않는다(TU-18).
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from tutor.judge.conversation import Conversation
from tutor.judge.draft_select import two_drafts
from tutor.judge.main_call import Complete, MainCallOut, MainCallPrompt, run_main_call
from tutor.retrieval import KbIndex, Section, retrieve


@dataclass(frozen=True)
class SecondDraft:
    """두 번째 초안 B와 고르기(후보 J). 흐름(`flow._second_draft`)이 요청마다 만든다."""

    prompt: MainCallPrompt  # B 프롬프트(주 호출 system + 작성 보충)
    complete: Complete  # B 호출(재시도·겹쳐 보내기 없음)
    # (근거 묶음, A, B) → 쓸 초안(`draft_select.choose`)
    choose: Callable[
        [Sequence[Section], MainCallOut | None, MainCallOut | None],
        Awaitable[MainCallOut | None],
    ]


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
    english_only_embedding: bool = False,
    examples: str = "",
    index: KbIndex | None = None,
    second: SecondDraft | None = None,
) -> Draft:
    """검색 상위 k개를 근거 묶음으로 주 호출을 한 번 부른다.

    `second`(후보 J, `toggles.draft_select`)가 있으면 같은 근거 묶음으로 두 번째 초안 B를 동시에
    만들고 `second.choose`가 고른 초안을 낸다(`tutor.judge.draft_select`). 없으면 지금과 같다.

    `embedding`은 기동 때 만든 임베딩 검색(TU-11, `state.tutor_emb`, #124)이다. 있을 때만
    `retrieve(…, embedding=)`로 넘기고, 없으면(토글 끔·기동 실패) BM25만 쓴다.
    `english_only_embedding`이 True일 때만 `retrieve(…, english_only_embedding=True)`로 넘긴다.
    `examples`는 판단 예시 블록(TU-08)이다. 비었으면 넣지 않는다.
    `index`는 기동 때 만든 KB 색인(`state.tutor_kb`)이다. 있을 때만 `retrieve(…, index=)`로 넘긴다.
    """
    query, history = conv.search_query()
    extra = {
        name: value
        for name, value in (("embedding", embedding), ("index", index))
        if value is not None
    }
    if english_only_embedding:
        extra["english_only_embedding"] = True
    sections = tuple(await retrieve(query, history, k=k, **extra))
    run_a = run_main_call(
        complete, prompt, conv, sections, signals, deadline=deadline, examples=examples
    )
    if second is None:
        return Draft(sections=sections, out=await run_a)
    run_b = run_main_call(
        second.complete,
        second.prompt,
        conv,
        sections,
        signals,
        deadline=deadline,
        examples=examples,
    )
    out_a, out_b = await two_drafts(run_a, run_b, deadline=deadline)
    return Draft(sections=sections, out=await second.choose(sections, out_a, out_b))
