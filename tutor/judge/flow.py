"""v2 답변 흐름(PRD §4 ①·③·⑤·⑦, `mode: v2`). `pipeline._answer`가 부른다.

① 대화 정리(TU-02)
③⑤ 검색 상위 k(TU-03) → 주 호출(TU-06) → 실패면 TU-12 기본값
   표(`config/actions.yaml`)가 template이라고 한 (범주, action)이면 튜터 고정 문장(IN-03)
⑦ 그 밖은 통합 결정적 수정 `integration.actions.finalize`(IN-04)

② 결정적 신호(TU-05): 저신호 직접 판정이면 LLM 없이 고정 clarify, 신호는 주 호출 <신호> 힌트
   (`toggles.signals`)와 실패 기본값(위기 → escalate, 지시 무력화 → refuse)에 쓴다

⑤-2 가드 classify 병렬 합치기(TU-10, `tutor.judge.guard_merge`): classify를 주 호출과 동시에 돌려
   공격 4범주이면 주 호출이 이미 표 고정 문장을 냈거나 더 강한 action을 낸 경우를 빼고 그 범주의
   고정 문장으로 바꾼다(`toggles.guard_classify`). serial(ⓐ)은 classify를 먼저 기다려 템플릿 범주면
   주 호출 없이 고정 문장을 낸다
⑥ 가드 judge_output 검증(TU-09 ②): 주 호출 문장이 그대로 나가는 경로에만, 합친 범주를 넘겨 부른다
   (`toggles.verify: guard`). 시간 부족·실패면 판정만 건너뛴다
⑦ 튜터 후처리(TU-07, `tutor.answer.postprocess.finalize`): 모든 응답이 마지막에 지난다

아직 없는 단계(뒤 이슈): kNN 예시(#77). 각 단계는 `config/tutor.yaml` 토글로 켜고 끈다(TU-14).

요청 내용은 지역 변수로만 쓰고 남기지 않는다(TU-18). LLM 실패는 예외가 아니라 결과 코드로 온다.
"""

import asyncio
from collections.abc import Sequence
from typing import Any

from guardrail.schema import OutputVerdict

from integration.actions import FALLBACK_ACTION, FinalizeConfig, action_for, finalize
from integration.contracts import SECTION_IDS, Action, Section, Sentence
from tutor.answer import postprocess
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
from tutor.judge.guard_merge import (
    JUDGE_SKIP_S,
    GuardSkips,
    attack_fixed,
    judge_candidate,
    merge_category,
    serial_fixed,
    start_classify,
    wait_classify,
)
from tutor.judge.main_call import CompleteResult, MainCallOut, MainCallPrompt
from tutor.judge.signals import Signal, detect, direct_clarify, labels
from tutor.request import TutorRequest
from tutor.retrieval import Section as RetrievedSection

MAIN_ROLE = "main"  # TutorLLM.calls에 세는 역할 이름(TU-13 호출 수 집계)
MAIN_RETRIES = 1  # 재시도는 주 호출만 1회(TU-13)
# safe=false일 때 답 전체를 대신하는 거절 문장(IN-04). 범주를 모르므로 일반 거절
REFUSE_TEXT = TEXT[Fixed.REFUSE_JAILBREAK]


def prepare_judge(app_state: Any) -> None:
    """기동 때 한 번: 범주 → action 표로 주 호출 프롬프트를 만들고, 가드 단계 건너뜀 횟수를 0으로.

    `tutor.startup.prepare`가 부른다."""
    app_state.tutor_prompt = MainCallPrompt.from_rules()
    app_state.guard_skips = GuardSkips()  # (단계, 사유) → 횟수. 요청 내용은 남기지 않는다


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


def fallback_body(signals: Sequence[Signal] = (), classified: str | None = None) -> dict[str, Any]:
    """주 호출 실패(재시도 후)·예산 초과의 기본 응답(TU-12).

    신호가 위기거나 가드 classify가 emotional_distress면 escalate, 지시 무력화면 refuse 고정 문장.
    그다음 classify가 공격 4범주였으면 그 범주의 고정 문장(TU-10, 공격 재현율은 classify 단독 이상).
    주 호출 결과가 없으니 classify를 쓰지 않을 이유가 없다. 그 밖에는 공유 설정
    `fallback_action`(잠정 clarify)의 고정 문장. 위기가 먼저다(PRD §3 우선순위).
    """
    if Signal.CRISIS in signals or classified == "emotional_distress":
        return fixed_body(Fixed.ESCALATE)
    if Signal.OVERRIDE in signals:
        return fixed_body(Fixed.REFUSE_JAILBREAK)
    attack = attack_fixed(classified)
    if attack is not None:
        return fixed_body(attack)
    action = FALLBACK_ACTION
    return {"answer": empty_answer(action), "action": action, "citations": []}


def assemble(
    out: MainCallOut,
    sections: Sequence[RetrievedSection],
    *,
    action: Action,
    max_citations: int,
    verdict: OutputVerdict | None = None,
) -> dict[str, Any]:
    """주 호출 결과 → 규격 본문. 템플릿 조합이면 고정 문장, 아니면 결정적 수정(IN-04).

    `verdict`는 가드 `judge_output` 결과다. None(판정 생략·실패)이면 판정 단계만 건너뛴다."""
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
        verdict,
        [_contract_section(s) for s in sections],
        action_for(out.category),
        cfg,
        action=action,
    )
    return result.model_dump()


def tutor_post(body: dict[str, Any], category: str | None = None) -> dict[str, Any]:
    """튜터 고유 후처리(TU-07, #76): 숨김 문자·채점 지시 문장 제거, 빈 답변 고정 문장, 인용 정리.

    모든 응답이 마지막에 지난다. 고정 문장 응답은 바뀌지 않는다(테스트).
    """
    return postprocess.finalize(
        body, fallback_action=FALLBACK_ACTION, known_sections=SECTION_IDS, category=category
    )


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


def _candidate(out: MainCallOut) -> str:
    """`judge_output`에 넘기는 후보 답변: 주 호출 문장을 공백 하나로 이어 붙인 것."""
    return " ".join(s.text for s in out.sentences if s.text)


async def answer_v2(req: TutorRequest, state: Any, *, deadline: float) -> dict[str, Any]:
    """v2 흐름 하나. 예외는 올린다(라우터가 클래스 이름만 기록하고 기본값을 낸다).

    가드 classify는 주 호출과 동시에 시작한다. 이 함수가 취소·예외로 빠져나가면 아직 도는 classify도
    함께 취소한다(요청이 끝난 뒤 남는 작업이 없게, TU-18).
    """
    conv: Conversation = tidy(_messages(req))
    if direct_clarify(conv):
        return tutor_post(fixed_body(Fixed.CLARIFY))  # TU-05: 빈 발화, 앞 대화 뒤의 자모·기호만
    cfg = state.tutor_cfg
    signals = detect(conv)  # 기본값 선택에는 토글과 상관없이 쓴다(TU-12)
    guard = getattr(state, "guard", None)
    skips: GuardSkips | None = getattr(state, "guard_skips", None)
    messages = _messages(req)
    serial = cfg.toggles.guard_classify == "serial"
    classify = start_classify(
        guard,
        messages,
        mode=cfg.toggles.guard_classify,
        timeout_s=cfg.llm.timeout_s.classify,
        deadline=deadline,
        skips=skips,
    )
    try:
        classified: str | None = None
        if classify is not None:
            # classify 요청이 주 호출보다 먼저 나가도록 한 번 양보한다(TU-10)
            await asyncio.sleep(0)
        if classify is not None and serial:
            classified = await wait_classify(classify, skips)  # ⓐ: 주 호출 앞에서 끝까지 기다린다
            fixed = serial_fixed(classified)
            if fixed is not None and Signal.CRISIS not in signals:
                # ⓐ: classify가 템플릿 범주면 주 호출 없이 고정 문장(통합 I13 ⓐ, #83 비교용)
                return tutor_post(fixed_body(fixed), classified)
        draft = await draft_answer(
            conv,
            prompt=state.tutor_prompt,
            complete=_complete(state),
            deadline=deadline,
            k=cfg.retrieval.k,
            signals=labels(signals) if cfg.toggles.signals else (),
            embedding=getattr(state, "tutor_emb", None),  # 토글이 켜져 만들어졌을 때만(TU-11)
        )
        if classify is not None and not serial:
            classified = await wait_classify(classify, skips)  # 자기 시간 제한 안에서 끝난다
        if draft.out is None:
            return tutor_post(fallback_body(signals, classified))
    finally:
        if classify is not None:
            classify.cancel()  # 아직 돌면 취소(요청이 끝난 뒤 남는 작업이 없게, TU-18)
    out = draft.out
    action = choose_action(out, from_category=cfg.toggles.action_from_category)
    merged = merge_category(out.category, action, classified)
    if merged.fixed is not None:
        # TU-10: classify가 공격 범주로 덮었다. 고정 문장은 판정하지 않는다
        return tutor_post(fixed_body(merged.fixed), merged.category)
    verdict = None
    # 주 호출 문장이 그대로 나가는 경로만 검증한다(고정 문장에는 판정할 주장이 없다, TU-09)
    if cfg.toggles.verify == "guard" and template_for(merged.category, action) is None:
        verdict = await judge_candidate(
            guard,
            messages,
            _candidate(out),
            merged.category,
            timeout_s=cfg.llm.timeout_s.verify,
            deadline=deadline,
            skip_s=JUDGE_SKIP_S,
            skips=skips,
        )
    unsafe = attack_fixed(merged.category) if verdict is not None and not verdict.safe else None
    if unsafe is not None:
        # safe=false인데 범주가 공격이면 일반 거절 대신 그 범주의 고정 문장(IN-04 거절의 범주판)
        return tutor_post(fixed_body(unsafe), merged.category)
    body = assemble(
        out,
        draft.sections,
        action=action,
        max_citations=cfg.toggles.max_citations,
        verdict=verdict,
    )
    return tutor_post(body, merged.category)
