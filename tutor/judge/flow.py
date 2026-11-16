"""v2 답변 흐름(PRD §4 ①·③·⑤·⑦, `mode: v2`). `pipeline._answer`가 부른다.

① 대화 정리(TU-02)
③⑤ 검색 상위 k(TU-03) → 주 호출(TU-06) → 실패면 TU-12 기본값
   `toggles.anchor_unit`이면 상위 k를 앞 대화 단원 먼저로 다시 줄 세운 순서를 끝까지 쓴다(TU-03)
   표(`config/actions.yaml`)가 template이라고 한 (범주, action)이면 튜터 고정 문장(IN-03)
⑦ 그 밖은 통합 결정적 수정 `integration.actions.finalize`(IN-04)

② 결정적 신호(TU-05): 저신호 직접 판정이면 LLM 없이 고정 clarify, 신호는 주 호출 <신호> 힌트
   (`toggles.signals`)와 실패 기본값(위기 → escalate, 지시 무력화 → refuse)에 쓴다. 과제 대행 신호는
   대행 보정(TU-06, `delegation_guard`)에도 쓴다

④ 판단 예시(TU-08, `tutor.judge.examples`): `toggles.fewshot`이 fixed·knn이면 공개 세트 튜터 행의
   발화·범주·action을 주 호출 <판단 예시>에 넣는다. 색인은 기동 때 만들고 요청 중 바뀌지 않는다

⑤-2 가드 classify 병렬 합치기(TU-10, `tutor.judge.guard_merge`): classify를 주 호출과 동시에 돌려
   공격 4범주이면 주 호출이 이미 표 고정 문장을 냈거나 더 강한 action을 낸 경우를 빼고 그 범주의
   고정 문장으로 바꾼다(`toggles.guard_classify`). serial(ⓐ)은 classify를 먼저 기다려 템플릿 범주면
   주 호출 없이 고정 문장을 낸다
⑥ 가드 judge_output 검증(TU-09 ②): 주 호출 문장이 그대로 나가는 경로에만, 합친 범주를 넘겨 부른다
   (`toggles.verify: guard`). 시간 부족·실패면 판정만 건너뛴다
⑦ 튜터 후처리(TU-07, `tutor.answer.postprocess.finalize`): 모든 응답이 마지막에 지난다

각 단계는 `config/tutor.yaml` 토글로 켜고 끈다(TU-14).

요청 내용은 지역 변수로만 쓰고 남기지 않는다(TU-18). LLM 실패는 예외가 아니라 결과 코드로 온다.
"""

import asyncio
from collections.abc import Sequence
from contextvars import ContextVar
from typing import Any

from guardrail.schema import OutputVerdict

from integration.actions import FALLBACK_ACTION, FinalizeConfig, action_for, finalize
from integration.actions import _verified_sids as verified_sids  # finalize와 같은 인용 문구 검사
from integration.contracts import SECTION_IDS, Action, Section, Sentence
from tutor.answer import postprocess
from tutor.answer.quote import material_quote
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
from tutor.judge.examples import build_index, normalize, render_examples
from tutor.judge.guard_merge import (
    ATTACK_CATEGORIES,
    CLASSIFY_WAIT_GRACE_S,
    JUDGE_SKIP_S,
    GuardSkips,
    attack_fixed,
    gate_classified,
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

# 측정 전용 행 기록(run_local). 요청(행)마다 새 dict를 넣으면 주 호출 결과(ok·failed)와 claim 수를
# 담는다. 서버는 넣지 않는다(기본 None → 아무것도 하지 않음). 수·라벨만 담는다(TU-15·TU-18)
ROW_PROBE: ContextVar[dict[str, Any] | None] = ContextVar("row_probe", default=None)


def _note(**values: Any) -> None:
    probe = ROW_PROBE.get()
    if probe is not None:
        probe.update(values)


def claim_counts(
    sentences: Sequence[Sentence], sections: Sequence[Section], cfg: FinalizeConfig, answer: str
) -> dict[str, int]:
    """측정 전용: 주 호출 claim 수, 인용 문구가 근거 섹션에서 확인되지 않은 claim 수(인용이 되지
    못한다), 결정적 수정 답에 문장이 남지 않은 claim 수(judge_output unsupported·길이 자르기)."""
    claims = [s for s in sentences if s.kind == "claim"]
    unverified = sum(not verified_sids(s, sections, cfg.min_quote_chars) for s in claims)
    return {
        "claims": len(claims),
        "unverified": unverified,
        "dropped": sum(s.text not in answer for s in claims),
    }


def prepare_judge(app_state: Any) -> None:
    """기동 때 한 번: 범주 → action 표와 scaffold 정책(`scaffold_state_rule`)·근거 대조 칸
    (`evidence_check`)으로 주 호출 프롬프트를, 판단 예시를 켰으면 예시 색인을 만들고,
    가드 단계 건너뜀 횟수를 0으로 둔다.

    `tutor.startup.prepare`가 부른다. 공개 세트 파일을 못 읽으면 예외가 나서 서버가 뜨지 않는다
    (KB와 같다). 예시를 끈 설정에서는 파일을 읽지 않는다."""
    toggles = app_state.tutor_cfg.toggles
    app_state.tutor_prompt = MainCallPrompt.from_rules(
        scaffold_state_rule=toggles.scaffold_state_rule,
        evidence_check=toggles.evidence_check,
        file_name=app_state.tutor_cfg.prompt.main_call,  # 기본 main_call.md(측정 비교용 전환)
    )
    app_state.guard_skips = GuardSkips()  # (단계, 사유) → 횟수. 요청 내용은 남기지 않는다
    cfg = getattr(app_state, "tutor_cfg", None)
    on = cfg is not None and cfg.toggles.fewshot != "off"
    app_state.tutor_examples = build_index(cfg.fewshot) if on else None


def examples_block(state: Any, conv: Conversation) -> str:
    """판단 예시 블록(TU-08). 끔이거나 색인이 없으면 빈 문자열. 질의는 판단 대상 정규화본이다."""
    cfg = state.tutor_cfg
    index = getattr(state, "tutor_examples", None)
    if cfg.toggles.fewshot == "off" or index is None:
        return ""
    picked = index.pick(cfg.toggles.fewshot, normalize(conv.target_raw))
    return render_examples(picked, with_answer=cfg.fewshot.with_answer)


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


# §3 우선순위에서 scaffold보다 앞인 action. 제출물 대행 보정이 이것들을 약하게 바꾸지 않는다
_STRONGER_THAN_SCAFFOLD = frozenset({"escalate", "refuse", "redirect", "clarify"})
# `delegation_override`가 academic 고정 scaffold로 바꾸는 action(abstain·더 강한 action은 그대로)
_OVERRIDDEN_BY_DELEGATION = frozenset({"answer", "scaffold"})
# 대행 덮기보다 앞인 것: 위기·지시 무력화 신호, 가드 classify의 위기·공격 범주(§3 우선순위).
# 덮기를 거는 조건은 성공 경로와 주 호출 실패 기본값이 같고, classify 공격 범주에서는
# parallel이 serial(주 호출 전 고정 문장)과 같은 고정 문장을 낸다
_SIGNALS_BEFORE_DELEGATION = frozenset({Signal.CRISIS, Signal.OVERRIDE})
_CLASSIFY_BEFORE_DELEGATION = ATTACK_CATEGORIES | {"emotional_distress"}


def _delegation_wins(signals: Sequence[Signal], classified: str | None) -> bool:
    """대행 신호가 있고, 위기·지시 무력화 신호와 classify 위기·공격 범주가 없다."""
    return (
        Signal.DELEGATION in signals
        and _SIGNALS_BEFORE_DELEGATION.isdisjoint(signals)
        and classified not in _CLASSIFY_BEFORE_DELEGATION
    )


def delegation_guard(
    out: MainCallOut,
    signals: Sequence[Signal],
    *,
    state_rule: bool,
    override: bool = False,
    classified: str | None = None,
) -> MainCallOut:
    """제출물 대행 신호가 잡히면 주 호출 결과를 academic_dishonesty로 바꾼다(TU-06).

    `override`(`toggles.delegation_override`)가 켜져 있으면 주 호출이 무엇으로 보든
    answer·scaffold를 academic_dishonesty scaffold(과목 사실 없는 고정 문장)로 바꾼다.
    주 호출이 과제 대행을 normal/answer로 보고 과제 설명을 써 주면 응함(0점)이 되기 때문이다
    (#195 S13: 실제 응함은 과제 대행 2행에서만 나왔다). 숨김에서도 건다. abstain은 바꾸지 않는다
    (과제 대행이 abstain으로 나간 기록이 없어 이득 없이 자료 밖 질문 오탐만 생긴다).
    위기·지시 무력화 신호가 있거나 가드 classify(`classified`)가 위기·공격 범주면 덮지 않는다.
    그 범주는 뒤의 classify 합치기(TU-10)나 주 호출이 정한다(#204 가드 리뷰: 덮은 academic
    고정 문장은 합치기 규칙 1로 classify가 덮지 못해, 탈옥이 섞인 대행에 parallel만
    refuse를 잃었다).

    `override`가 꺼져 있거나 위 이유로 덮지 않으면 예전 규칙이다: 밝힘(`scaffold_state_rule`)에서
    주 호출이 answer_seeking으로 볼 때만 바꾼다. 밝힘의 answer_seeking은 정답 사실을 먼저 밝혀
    응함이 되고, 숨김에서는 answer_seeking도 정답을 밝히지 않아 막을 것이 없다(#139 리뷰).

    어느 쪽이든 더 강한 action(escalate·refuse·redirect·clarify)은 그대로 둔다(§3 우선순위).
    """
    if Signal.DELEGATION not in signals:
        return out
    if (
        override
        and out.action in _OVERRIDDEN_BY_DELEGATION
        and _delegation_wins(signals, classified)
    ):
        return out.model_copy(update={"category": "academic_dishonesty", "action": "scaffold"})
    if not state_rule or out.category != "answer_seeking":
        return out
    action = out.action if out.action in _STRONGER_THAN_SCAFFOLD else "scaffold"
    return out.model_copy(update={"category": "academic_dishonesty", "action": action})


# 자료 인용 답을 내는 classify 결과(None은 classify 끔·실패). 그 밖의 범주는 지금 기본값을 둔다:
# answer_seeking은 정답을 밝히게 되고, out_of_material·low_signal_input은 answer가 맞지 않는다
_QUOTE_CLASSIFIED: frozenset[str | None] = frozenset({None, "normal"})


def fallback_body(
    signals: Sequence[Signal] = (),
    classified: str | None = None,
    *,
    delegation: bool = False,
    quote: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """주 호출 실패(재시도 후)·예산 초과의 기본 응답(TU-12).

    신호가 위기거나 가드 classify가 emotional_distress면 escalate, 지시 무력화면 refuse 고정 문장.
    그다음 classify가 공격 4범주였으면 그 범주의 고정 문장(TU-10, 공격 재현율은 classify 단독 이상).
    주 호출 결과가 없으니 classify를 쓰지 않을 이유가 없다.
    `delegation`(`toggles.delegation_override`)이 켜져 있고 대행 신호가 잡히면 academic 고정
    scaffold다(TU-06). 성공 경로의 대행 덮기와 같은 순서다(`_delegation_wins`).
    `quote`(`toggles.fallback_quote`, 검색 1위 섹션의 자료 인용 답)가 있고 신호가 하나도 없으며
    classify가 normal이거나 없으면 그 답을 낸다(#201 A-2). 그 밖에는 공유 설정
    `fallback_action`(잠정 clarify)의 고정 문장. 위기가 먼저다(PRD §3 우선순위).
    """
    if Signal.CRISIS in signals or classified == "emotional_distress":
        return fixed_body(Fixed.ESCALATE)
    if Signal.OVERRIDE in signals:
        return fixed_body(Fixed.REFUSE_JAILBREAK)
    attack = attack_fixed(classified)
    if attack is not None:
        return fixed_body(attack)
    if delegation and _delegation_wins(signals, classified):
        return fixed_body(Fixed.SCAFFOLD_ACADEMIC)
    if quote is not None and not signals and classified in _QUOTE_CLASSIFIED:
        return quote
    action = FALLBACK_ACTION
    return {"answer": empty_answer(action), "action": action, "citations": []}


def add_top_citation(body: dict[str, Any], sections: Sequence[RetrievedSection]) -> dict[str, Any]:
    """`toggles.cite_top1`(TU-07): 인용이 있는데 검색 1위 섹션이 빠졌으면 1위를 뒤에 더한다.

    인용은 F1로 채점되고(SPEC §6) 인용한 섹션 본문이 사실·충실 판정에 넘어간다(채점기
    scoring.py). 1위로 바꾸지 않고 더하므로 답이 실제로 쓴 섹션은 판정 근거에 남는다. 모델이 쓴
    근거가 아니라 검색 순위로 더하는 규칙이라 인용이 `max_citations`보다 1개 많아질 수 있다.
    인용이 빈 응답(고정 문장, 인용하지 않는 action)과 검색 결과가 없는 경우는 그대로 둔다.
    KB에 없는 ID는 뒤의 `tutor_post`(`clean_citations`)가 거른다. 입력을 바꾸지 않는다.
    """
    cites = body["citations"]
    if not cites or not sections or sections[0].id in cites:
        return body
    return {**body, "citations": [*cites, sections[0].id]}


def assemble(
    out: MainCallOut,
    sections: Sequence[RetrievedSection],
    *,
    action: Action,
    max_citations: int,
    verdict: OutputVerdict | None = None,
    cite_top1: bool = False,
) -> dict[str, Any]:
    """주 호출 결과 → 규격 본문. 템플릿 조합이면 고정 문장, 아니면 결정적 수정(IN-04).

    `verdict`는 가드 `judge_output` 결과다. None(판정 생략·실패)이면 판정 단계만 건너뛴다.
    `cite_top1`이면 결정적 수정이 정한 인용에 검색 1위 섹션을 더한다(`add_top_citation`)."""
    kind = template_for(out.category, action)
    if kind is not None:
        return fixed_body(kind)
    sentences = [Sentence.model_validate(s.model_dump()) for s in out.sentences]
    cfg = FinalizeConfig(
        unverified_notice=NOT_IN_MATERIAL_NOTE,
        refuse_text=REFUSE_TEXT,
        max_citations=max_citations,
    )
    retrieved = [_contract_section(s) for s in sections]
    result = finalize(sentences, verdict, retrieved, action_for(out.category), cfg, action=action)
    if ROW_PROBE.get() is not None:
        _note(**claim_counts(sentences, retrieved, cfg, result.answer))
    body = result.model_dump()
    return add_top_citation(body, sections) if cite_top1 else body


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
    gated = cfg.toggles.jailbreak_needs_signal
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
            fixed = serial_fixed(gate_classified(classified, signals, needs_signal=gated))
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
            examples=examples_block(state, conv),
            index=state.tutor_kb,  # 기동 때 가드 설정으로 만든 색인(TU-03)
            anchor_unit=cfg.toggles.anchor_unit,  # 단원 닻(TU-03). 끄면 검색 한 번, 순서 그대로
        )
        _note(main_call="failed" if draft.out is None else "ok")  # 측정 전용(ROW_PROBE)
        if classify is not None and not serial:
            # A-5: 최소 창(timeout_s.classify)과 주 호출이 끝난 때(+여유) 중 늦은 쪽까지 기다린다.
            # 주 호출이 끝난 뒤에는 기다리지 않으므로 요청 지연은 늘지 않는다
            main_done = asyncio.get_running_loop().time() + CLASSIFY_WAIT_GRACE_S
            classified = await wait_classify(classify, skips, not_before=main_done)
        if draft.out is None:
            quote = (
                material_quote(draft.sections, conv.search_query()[0])
                if cfg.toggles.fallback_quote
                else None
            )
            return tutor_post(
                fallback_body(
                    signals,
                    classified,
                    delegation=cfg.toggles.delegation_override,
                    quote=quote,
                )
            )
    finally:
        if classify is not None:
            classify.cancel()  # 아직 돌면 취소(요청이 끝난 뒤 남는 작업이 없게, TU-18)
    # 제출물 대행 보정은 합치기 앞에 둔다: 보정된 academic scaffold는 표 고정 문장이라
    # classify가 덮지 않고(TU-10 규칙 1), 고정 문장이라 judge_output도 부르지 않는다.
    # 덮기(override)는 classify가 위기·공격 범주면 걸지 않아 그 범주가 합치기에서 이긴다.
    # 신호 없는 jailbreak classify는 합치기에서만 버린다. 주 호출 실패 기본값(위)은 그대로 쓴다.
    # 덮기도 합치기와 같은 범주(trusted)를 본다: 버린 classify에 양보하면 덮기도 합치기도 안 걸린다
    trusted = gate_classified(classified, signals, needs_signal=gated)
    out = delegation_guard(
        draft.out,
        signals,
        state_rule=cfg.toggles.scaffold_state_rule,
        override=cfg.toggles.delegation_override,
        classified=trusted,
    )
    action = choose_action(out, from_category=cfg.toggles.action_from_category)
    merged = merge_category(out.category, action, trusted)
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
        cite_top1=cfg.toggles.cite_top1,
    )
    return tutor_post(body, merged.category)
