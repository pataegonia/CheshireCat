"""v2 답변 흐름(PRD §4 ①·③·⑤·⑦, `mode: v2`). `pipeline._answer`가 부른다.

① 대화 정리(TU-02) → 빈 판단 대상이면 고정 clarify(TU-05)
③⑤ 검색 상위 k(TU-03) → 주 호출(TU-06) → 실패면 TU-12 기본값
   표(`config/actions.yaml`)가 template이라고 한 (범주, action)이면 튜터 고정 문장(IN-03)
⑦ 그 밖은 통합 결정적 수정 `integration.actions.finalize`(IN-04) → 빈 답변 고정 문장(TU-07)

아직 없는 단계(뒤 이슈): 결정적 신호·저신호 직접 판정(#74), 튜터 후처리(#76), kNN 예시(#77),
가드 classify 병렬 합치기·judge_output(#88). 각 단계는 `config/tutor.yaml` 토글로 켜고 끈다(TU-14).

요청 내용은 지역 변수로만 쓰고 남기지 않는다(TU-18). LLM 실패는 예외가 아니라 결과 코드로 온다.
"""

from collections.abc import Sequence
from typing import Any

from integration.actions import FALLBACK_ACTION, FinalizeConfig, action_for, finalize
from integration.contracts import Action, Section, Sentence
from tutor.answer.templates import (
    NOT_IN_MATERIAL_NOTE,
    TEXT,
    Fixed,
    empty_answer,
    fixed_body,
    template_for,
)
from tutor.judge.conversation import Conversation, tidy
from tutor.judge.draft import draft_answer
from tutor.judge.main_call import CompleteResult, MainCallOut, MainCallPrompt
from tutor.request import TutorRequest
from tutor.retrieval import Section as RetrievedSection

MAIN_ROLE = "main"  # TutorLLM.calls에 세는 역할 이름(TU-13 호출 수 집계)
MAIN_RETRIES = 1  # 재시도는 주 호출만 1회(TU-13)
# safe=false일 때 답 전체를 대신하는 거절 문장(IN-04). 범주를 모르므로 일반 거절
REFUSE_TEXT = TEXT[Fixed.REFUSE_JAILBREAK]


def prepare_judge(app_state: Any) -> None:
    """기동 때 한 번: 범주 → action 표로 주 호출 프롬프트를 만든다.

    `tutor.startup.prepare`가 부른다."""
    app_state.tutor_prompt = MainCallPrompt.from_rules()


def _messages(req: TutorRequest) -> list[dict[str, str]]:
    """B의 요청 해석(`parse_request`) 결과를 TU-02 대화 정리 입력으로 넘긴다."""
    return [{"role": t.role, "content": t.content} for t in req.turns]


def _contract_section(s: RetrievedSection) -> Section:
    return Section(
        id=s.id,
        subject=s.subject,
        title=s.title,
        text=s.text,
        tables=list(s.tables),
        names=list(s.names),
    )


def choose_action(out: MainCallOut, *, from_category: bool) -> Action:
    """주 호출 action. `action_from_category`가 켜져 있으면 표와 어긋날 때 표를 따르되,
    answer 쪽으로는 덮지 않는다(공격 문항이 answer로 덮이면 응함 위험, TU-06)."""
    rule = action_for(out.category)
    if not from_category or out.action in rule.actions or rule.action == "answer":
        return out.action
    return rule.action


def fallback_body() -> dict[str, Any]:
    """주 호출 실패(재시도 후)·예산 초과: 공유 설정 `fallback_action`의 고정 문장.

    TU-12, 잠정 clarify.

    신호를 보고 escalate·refuse로 바꾸는 규칙은 결정적 신호(#74)와 함께 들어온다."""
    action = FALLBACK_ACTION
    return {"answer": empty_answer(action), "action": action, "citations": []}


def assemble(
    out: MainCallOut,
    sections: Sequence[RetrievedSection],
    *,
    action: Action,
    max_citations: int,
) -> dict[str, Any]:
    """주 호출 결과 → 규격 본문. 템플릿 조합이면 고정 문장, 아니면 결정적 수정(IN-04)."""
    kind = template_for(out.category, action)
    if kind is not None:
        return fixed_body(kind)
    sentences = [Sentence.model_validate(s.model_dump()) for s in out.sentences]
    cfg = FinalizeConfig(
        unverified_notice=NOT_IN_MATERIAL_NOTE,
        refuse_text=REFUSE_TEXT,
        max_citations=max_citations,
    )
    result = finalize(
        sentences,
        None,  # 가드 judge_output 검증은 #88에서 연결(TU-09)
        [_contract_section(s) for s in sections],
        action_for(out.category),
        cfg,
        action=action,
    )
    body = result.model_dump()
    if not body["answer"].strip():
        body["answer"] = empty_answer(result.action, out.category)
    return body


def _complete(state: Any):
    """주 호출 어댑터: #107 `TutorLLM.complete`를 `main_call.Complete` 모양으로."""
    cfg = state.tutor_cfg

    async def complete(messages, schema, *, deadline: float) -> CompleteResult:
        return await state.tutor_llm.complete(
            MAIN_ROLE,
            messages,
            schema,
            deadline=deadline,
            timeout_s=cfg.llm.timeout_s.main,
            retries=MAIN_RETRIES,
        )

    return complete


async def answer_v2(req: TutorRequest, state: Any, *, deadline: float) -> dict[str, Any]:
    """v2 흐름 하나. 예외는 올린다(라우터가 클래스 이름만 기록하고 기본값을 낸다)."""
    conv: Conversation = tidy(_messages(req))
    if conv.is_empty:
        return fixed_body(Fixed.CLARIFY)  # TU-05: 빈 발화·보이지 않는 문자뿐
    cfg = state.tutor_cfg
    draft = await draft_answer(
        conv,
        prompt=state.tutor_prompt,
        complete=_complete(state),
        deadline=deadline,
        k=cfg.retrieval.k,
        embedding=getattr(
            state, "tutor_emb", None
        ),  # toggles.embedding이 켜져 만들어졌을 때만(TU-11)
    )
    if draft.out is None:
        return fallback_body()
    action = choose_action(draft.out, from_category=cfg.toggles.action_from_category)
    return assemble(
        draft.out, draft.sections, action=action, max_citations=cfg.toggles.max_citations
    )
