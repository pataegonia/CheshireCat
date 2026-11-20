"""v2 답변 흐름(PRD §4 ①·③·⑤·⑦, `mode: v2`). `pipeline._answer`가 부른다.

① 대화 정리(TU-02)
③⑤ 검색 상위 k(TU-03) → 주 호출(TU-06) → 실패면 TU-12 기본값
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
⑤-3 T 의도 판정 덮기(TU-06, `tutor.judge.intent`, `toggles.intent_override`): #209 프롬프트로 nano를
   1회 불러 이름표로 주 호출 action을 덮는다. parallel은 주 호출과 동시에, serial은 T 먼저
   (refuse·위기 고정 문장 이름표면 주 호출 없이). 가드 classify가 위기·공격 범주면 T는 비킨다.
   자료 인용 답은 T가 학습 질문이라 답한 때만 낸다(T 관문)
⑥ 가드 judge_output 검증(TU-09 ②): 주 호출 문장이 그대로 나가는 경로에만, 합친 범주를 넘겨 부른다
   (`toggles.verify: guard`). 시간 부족·실패면 판정만 건너뛴다
⑦ 튜터 후처리(TU-07, `tutor.answer.postprocess.finalize`): 모든 응답이 마지막에 지난다

각 단계는 `config/tutor.yaml` 토글로 켜고 끈다(TU-14).

요청 내용은 지역 변수로만 쓰고 남기지 않는다(TU-18). LLM 실패는 예외가 아니라 결과 코드로 온다.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from types import SimpleNamespace
from typing import Any

from guardrail.schema import OutputVerdict

from integration.actions import FALLBACK_ACTION, FinalizeConfig, action_for, finalize
from integration.contracts import SECTION_IDS, Action, Section, Sentence
from tutor.answer import postprocess
from tutor.answer.quote import material_quote, quote_sentences
from tutor.answer.templates import (
    NOT_IN_MATERIAL_NOTE,
    QUOTE_APPEND_LEAD,
    TEXT,
    Fixed,
    empty_answer,
    fixed_body,
    template_for,
)
from tutor.judge import main_call, multi_draft
from tutor.judge.conversation import Conversation, normalize_fast, tidy
from tutor.judge.draft import Draft, draft_answer
from tutor.judge.examples import build_index, normalize, render_examples
from tutor.judge.guard_merge import (
    ATTACK_CATEGORIES,
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
from tutor.judge.intent import (
    STUDY,
    WAIT_GRACE_S,
    IntentJob,
    IntentSkips,
    intent_in_time,
    start_intent,
    table_of_contents,
    wait_intent,
)
from tutor.judge.intent import build_messages as build_intent_messages
from tutor.judge.intent import decide as decide_intent
from tutor.judge.intent import load_system as load_intent_system
from tutor.judge.main_call import CompleteResult, MainCallOut, MainCallPrompt
from tutor.judge.oom_gate import GateCounts, GateJob, english_only, material_absent, start_gate
from tutor.judge.oom_gate import build_messages as build_gate_messages
from tutor.judge.oom_gate import load_system as load_gate_system
from tutor.judge.signals import Signal, detect, direct_clarify, labels
from tutor.judge.specialists import (
    REDRAFT_ROLE,
    RQ_ABSTAIN,
    RQ_CODE,
    SJ_ROLE,
    RqPrompts,
    StageRec,
    bounded,
    has_mention_marker,
    reverse_question,
    sj_messages,
)
from tutor.judge.unknown_terms import kb_blob, missing_terms
from tutor.request import TutorRequest
from tutor.retrieval import Section as RetrievedSection
from tutor.retrieval import retrieve

MAIN_ROLE = "main"  # TutorLLM.calls에 세는 역할 이름(TU-13 호출 수 집계)
MAIN_RETRIES = 1  # 재시도는 주 호출만 1회(TU-13)
MAIN_HEDGE_ROLE = "main_hedge"  # 겹쳐 보낸 두 번째 주 호출(따로 센다)
HEDGE_MIN_LEFT_S = 4.0  # 요청 마감까지 이보다 덜 남았으면 겹쳐 보내지 않는다(끝낼 수 없다)
# safe=false일 때 답 전체를 대신하는 거절 문장(IN-04). 범주를 모르므로 일반 거절
REFUSE_TEXT = TEXT[Fixed.REFUSE_JAILBREAK]


def prepare_judge(app_state: Any) -> None:
    """기동 때 한 번: 범주 → action 표와 scaffold 정책(`scaffold_state_rule`)·근거 대조 칸
    (`evidence_check`)으로 주 호출 프롬프트를, 판단 예시를 켰으면 예시 색인을 만들고,
    가드 단계 건너뜀 횟수를 0으로 둔다.

    `tutor.startup.prepare`가 부른다. 공개 세트 파일을 못 읽으면 예외가 나서 서버가 뜨지 않는다
    (KB와 같다). 예시를 끈 설정에서는 파일을 읽지 않는다."""
    toggles = app_state.tutor_cfg.toggles
    app_state.tutor_prompt = MainCallPrompt.from_rules(
        scaffold_state_rule=toggles.scaffold_state_rule, evidence_check=toggles.evidence_check
    )
    app_state.guard_skips = GuardSkips()  # (단계, 사유) → 횟수. 요청 내용은 남기지 않는다
    intent_on = toggles.intent_override != "off"  # T 의도 판정(#209): 프롬프트·목차는 기동 때 한 번
    app_state.tutor_intent_prompt = load_intent_system() if intent_on else None
    sections = app_state.tutor_kb.sections if intent_on else {}
    app_state.tutor_intent_toc = table_of_contents(sections) if intent_on else None
    app_state.intent_skips = IntentSkips()  # 사유 → 횟수(본문 없음)
    gate_on = toggles.oom_gate != "off"  # 자료 밖 확인 E(#213 B2): 프롬프트는 기동 때 한 번
    app_state.tutor_oom_gate_prompt = load_gate_system() if gate_on else None
    app_state.oom_gate_counts = GateCounts()  # 사유 → 횟수(본문 없음)
    named = toggles.oom_gate == "named_term"  # KB 전체 글(비교용)도 기동 때 한 번
    app_state.tutor_kb_blob = kb_blob(app_state.tutor_kb.sections) if named else None
    rq_on = toggles.reverse_question != "off"  # 실험 rq 프롬프트 둘: 기동 때 한 번
    app_state.tutor_rq_prompts = RqPrompts.load() if rq_on else None
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
    scaffold다(TU-06). 성공 경로의 대행 덮기와 같은 순서(`_delegation_wins`)지만, 여기서는
    `jailbreak_needs_signal` 조건을 거치기 전 classify를 본다(주 호출 결과가 없어 classify를 그대로
    쓴다). 신호 없는 jailbreak classify면 성공 경로는 academic, 실패 기본값은 refuse가 된다.
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
    top_sections: Sequence[RetrievedSection] | None = None,
) -> dict[str, Any]:
    """주 호출 결과 → 규격 본문. 템플릿 조합이면 고정 문장, 아니면 결정적 수정(IN-04).

    `verdict`는 가드 `judge_output` 결과다. None(판정 생략·실패)이면 판정 단계만 건너뛴다.
    `cite_top1`이면 결정적 수정이 정한 인용에 검색 1위 섹션을 더한다(`add_top_citation`).
    `top_sections`를 주면 근거 묶음 대신 그 목록의 1위를 더한다(규칙 A, `top1_dialog_query`)."""
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
    body = result.model_dump()
    top = sections if top_sections is None else top_sections
    return add_top_citation(body, top) if cite_top1 else body


def tutor_post(body: dict[str, Any], category: str | None = None) -> dict[str, Any]:
    """튜터 고유 후처리(TU-07, #76): 숨김 문자·채점 지시 문장 제거, 빈 답변 고정 문장, 인용 정리.

    모든 응답이 마지막에 지난다. 고정 문장 응답은 바뀌지 않는다(테스트).
    """
    return postprocess.finalize(
        body, fallback_action=FALLBACK_ACTION, known_sections=SECTION_IDS, category=category
    )


def _complete(state: Any):
    """주 호출 어댑터: #107 `TutorLLM.complete`를 `main_call.Complete` 모양으로.

    T와 가드 classify를 함께 켜면 주 호출 재시도를 끈다. classify는 1차가 탈옥이면 2차 판정까지
    nano 2회라, 주 호출 1 + classify 2 + T 1 = 4회로 TU-13 절대 4회 안에 둔다(#220 리뷰)."""
    cfg = state.tutor_cfg
    both = _t_on(cfg) and cfg.toggles.guard_classify != "off"
    retries = 0 if both else MAIN_RETRIES

    def send(role: str, tries: int, messages, schema, deadline: float):
        return asyncio.ensure_future(
            state.tutor_llm.complete(
                role,
                messages,
                schema,
                deadline=deadline,
                timeout_s=cfg.llm.timeout_s.main,
                retries=tries,
            )
        )

    async def complete(messages, schema, *, deadline: float) -> CompleteResult:
        """주 호출. `llm.timeout_s.main_hedge`초 안에 안 끝나면 같은 요청을 한 번 더 겹쳐 보내고
        먼저 성공한 쪽을 쓴다(둘 다 실패면 마지막 실패). 느린 호출은 호출마다 따로 생겨서
        (11/18 계측: 같은 문항이 되풀이해 느리지 않다) 두 번째가 살린다. 남은 호출은 취소한다."""
        tasks = {send(MAIN_ROLE, retries, messages, schema, deadline)}
        try:
            done, _ = await asyncio.wait(tasks, timeout=cfg.llm.timeout_s.main_hedge)
            left = deadline - asyncio.get_running_loop().time()
            hedge_on = cfg.llm.timeout_s.main_hedge < cfg.llm.timeout_s.main  # main 이상이면 끔
            if hedge_on and not done and left > HEDGE_MIN_LEFT_S:
                tasks.add(send(MAIN_HEDGE_ROLE, 0, messages, schema, deadline))
            result, pending = None, set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    result = task.result()  # 실패는 예외가 아니라 Result.error로 온다
                    if result.error is None:
                        return result
            return result
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()  # 요청이 끝난 뒤 남는 작업이 없게(TU-18). 취소는 삼키지 않는다

    return complete


def _draft_code(result: Any) -> str:
    """초안 결과의 로그 코드: 성공 "-", 잘림이면 finish_reason(length·content_filter),
    그 밖은 오류 코드."""
    if result.error is None:
        return "-"
    if result.error == "bad_json" and result.finish_reason in ("length", "content_filter"):
        return str(result.finish_reason)
    return str(result.error)


def _multi_complete(state: Any, runs: list[MainCallOut | None]):
    """`llm.multi_draft`를 켰을 때의 주 호출 어댑터(겹쳐 보내기 대신). 초안 K개를 동시에 보내고
    `multi_draft.wait_drafts` 규칙으로 기다린다. 초안마다 정리한 결과(무효면 None)를 `runs`에
    번호 순으로 채우고, 유효한 결과 하나(없으면 A의 결과)를 돌려준다. 실제로 고르는 일은
    `_pick_draft`가 근거 묶음을 받아서 한다. 초안 메타데이터는 `multi_draft.SHADOW`에 남긴다."""
    cfg = state.tutor_cfg
    md = cfg.llm.multi_draft
    both = _t_on(cfg) and cfg.toggles.guard_classify != "off"
    retries = 0 if both else MAIN_RETRIES  # 초안 A는 지금의 주 호출과 같은 재시도
    llm = state.tutor_llm
    sends_effort = getattr(llm, "sends_effort", True)

    async def complete(messages, schema, *, deadline: float) -> CompleteResult:
        loop = asyncio.get_running_loop()
        start = loop.time()
        shadow = multi_draft.SHADOW.get()
        if shadow is not None:  # 안전망 시간 초과로 빠져나가도 칸이 남게 보내기 전에 센다
            shadow.started += len(md.drafts)
        tasks = []
        for i, spec in enumerate(md.drafts):
            low = spec.effort == "low"
            tasks.append(
                asyncio.ensure_future(
                    llm.complete(
                        MAIN_ROLE if i == 0 else f"{MAIN_ROLE}_{multi_draft.LETTERS[i]}",
                        messages,
                        schema,
                        deadline=deadline if low else deadline - md.margin_s,
                        timeout_s=spec.timeout_s or cfg.llm.timeout_s.main,
                        retries=retries if i == 0 else 0,
                        effort=spec.effort,
                    )
                )
            )

        def valid(result: Any) -> bool:
            return result.error is None and isinstance(result.parsed, schema)

        cut = min(start + md.wait_s, deadline - md.margin_s)
        await multi_draft.wait_drafts(tasks, cut=cut, deadline=deadline, valid=valid)
        ended = loop.time()
        results = [t.result() if t.done() and not t.cancelled() else None for t in tasks]
        for i, (spec, result) in enumerate(zip(md.drafts, results, strict=True)):
            runs.append(main_call.clean(result.parsed) if result and valid(result) else None)
            if shadow is not None:
                effort = multi_draft.EFFORT_KEY[spec.effort] if sends_effort else "na"
                shadow.drafts.append(
                    multi_draft.DraftStat(
                        key=f"{effort}_{multi_draft.LETTERS[i]}",
                        ms=result.latency_ms if result else (ended - start) * 1000,
                        attempts=result.attempts if result else 1,
                        code=_draft_code(result) if result else "cut",
                        gate_ms=getattr(result, "gate_ms", 0.0) if result else 0.0,
                    )
                )
        done = [r for r in results if r is not None]
        return next((r for r in done if valid(r)), results[0] or (done[-1] if done else _NO_RESULT))

    return complete


# 초안이 하나도 끝나지 않았을 때(요청 마감) run_main_call에 돌려주는 실패 결과
_NO_RESULT = SimpleNamespace(parsed=None, error="timeout")


def _pick_draft(state: Any, draft: Draft, runs: Sequence[MainCallOut | None]) -> Draft:
    """초안 가운데 하나를 고른다(`multi_draft.select`). 유효한 초안이 없으면 주 호출 실패 그대로."""
    efforts = [d.effort for d in state.tutor_cfg.llm.multi_draft.drafts]
    sections = multi_draft.contract_sections(draft.sections)
    chosen = multi_draft.select(runs, efforts, sections)
    aa_mode = state.tutor_cfg.llm.multi_draft.abstain_if_any
    if aa_mode != "off":
        rec = _stage("aa")
        alt = multi_draft.abstain_override(runs, chosen)
        rec.before = None if chosen is None else runs[chosen].action  # type: ignore[union-attr]
        rec.trig = alt is not None
        rec.after = rec.before if alt is None else runs[alt].action  # type: ignore[union-attr]
        if alt is not None and aa_mode == "on":
            rec.changed, chosen = True, alt
    shadow = multi_draft.SHADOW.get()
    if shadow is not None:
        actions = {o.action for o in runs if o is not None}
        shadow.valid = sum(o is not None for o in runs)
        shadow.actions = [None if o is None else o.action for o in runs]
        shadow.split = len(actions) > 1
        shadow.chosen = chosen
        shadow.a_action = runs[0].action if runs and runs[0] is not None else None
        shadow.chosen_action = None if chosen is None else runs[chosen].action  # type: ignore[union-attr]
        shadow.bundle = [s.id for s in draft.sections]  # 측정 전용: 근거 묶음 섹션(검색 순위 순)
    out = None if chosen is None else runs[chosen]
    return Draft(sections=draft.sections, out=out)


def _start_intent(
    state: Any, conv: Conversation, signals: Sequence[Signal], deadline: float
) -> IntentJob | None:
    """T 의도 판정을 주 호출 앞에서 시작한다(`toggles.intent_override`). 끔이면 None."""
    system = getattr(state, "tutor_intent_prompt", None)
    if state.tutor_cfg.toggles.intent_override == "off" or system is None:
        return None
    messages = build_intent_messages(system, state.tutor_intent_toc, conv, signals)
    return start_intent(
        state.tutor_llm,
        messages,
        timeout_s=state.tutor_cfg.llm.timeout_s.intent,
        deadline=deadline,
        skips=getattr(state, "intent_skips", None),
    )


def _classify_wins(trusted: str | None) -> bool:
    """classify(신호 조건을 거친 값)가 위기·공격 범주면 T는 덮지 않는다(합치기·기본값이 정한다)."""
    return trusted in ATTACK_CATEGORIES or trusted == "emotional_distress"


def _t_on(cfg: Any) -> bool:
    """T를 부르는 설정인가. 직렬 classify(ⓐ)에서는 intent_override를 켜도 T를 부르지 않는다."""
    return cfg.toggles.intent_override != "off" and cfg.toggles.guard_classify != "serial"


def _quote_allowed(cfg: Any, label: str | None) -> bool:
    """자료 인용 답을 낼 수 있는가. T를 켰으면 T가 학습 질문이라 답한 때만(실패·다른 이름표는
    clarify). classify 실패·끔도 "없음"이라 신호 없는 공격·위기가 인용 답을 받던 길을 T로 막는다."""
    return not _t_on(cfg) or label == STUDY


# 직렬 T에서 주 호출도 불러 보는 고정 문장(redirect·scaffold). 주 호출이 성공하면 parallel과
# 같은 덮기 규칙(decide)으로 정해 주 호출의 escalate·refuse 등 더 강한 action을 지키고,
# 실패하면 이 고정 문장을 낸다. 위기 신호 정규식에 안 걸린 위기 발화를 T가 제출물 대행·무관
# 결과물 등으로 보면 escalate가 빠지던 것(#220 가드 리뷰 1번, 11/18 T 탐침 12문장 중 4문장)과,
# 주 호출 refuse를 약한 T 고정 문장이 이기던 것(#233 가드 리뷰 1번). refuse 고정 문장과
# 위기(escalate)는 주 호출 없이 낸다
SERIAL_ASKS_MAIN = frozenset(
    {Fixed.REDIRECT_OFF_TOPIC, Fixed.REDIRECT_HARMFUL, Fixed.SCAFFOLD_ACADEMIC}
)


def _intent_fixed(label: str | None, signals: Sequence[Signal], action: Action | None):
    """T가 덮어 고정 문장이 되면 (고정 문장, 범주). 아니면 None."""
    decided = decide_intent(label, signals, action)
    kind = template_for(decided.category, decided.action) if decided is not None else None
    return (kind, decided.category) if kind is not None and decided is not None else None


def _candidate(out: MainCallOut) -> str:
    """`judge_output`에 넘기는 후보 답변: 주 호출 문장을 공백 하나로 이어 붙인 것."""
    return " ".join(s.text for s in out.sentences if s.text)


async def answer_v2(req: TutorRequest, state: Any, *, deadline: float) -> dict[str, Any]:
    """v2 흐름 하나. 자료 밖 확인 E(`toggles.oom_gate`)를 켰으면 요청 시작 때 E를 함께 띄우고,
    흐름이 끝나 최종 action이 answer이고 E가 "근거에 없음"이면 자료 밖 고정 문장(abstain)으로
    바꾼다. E가 마감(E 시작부터 `llm.timeout_s.oom_gate`) 안에 끝나지 않거나 실패하면 흐름
    결과 그대로다. E 준비(검색·메시지 만들기)가 예외를 내도 E 없이 흐름을 그대로 탄다
    (`start_error`로 센다). abstain으로 바꾸지 않은 결과에만 자료 문장 덧붙이기
    (`toggles.quote_append`)를 한다. 빠져나갈 때 아직 도는 E는 취소한다(TU-18). 흐름 본체의
    예외는 올린다(라우터가 클래스 이름만 기록하고 기본값을 낸다).

    `toggles.oom_schedule`이 after_intent·after_intent_fresh면 E를 요청 시작 때가 아니라 흐름이
    주 호출을 부르기 직전에 띄운다(`_answer_v2`의 `begin_gate`). 그래서 직렬 T가 주 호출 없이 끝내는
    요청(refuse·위기 고정 문장)과 주 호출 전에 끝나는 요청에는 E를 보내지 않는다. 그 요청들의
    최종 action은 answer가 아니라 E 결과를 쓰지 않던 요청이다. after_intent는 E 마감을 요청
    시작부터 센 값 그대로 두고(호출 순서만 바뀐다), after_intent_fresh는 E 시작부터 센다.
    """
    toggles = state.tutor_cfg.toggles
    for key, mode in (
        ("rq", toggles.reverse_question),
        ("sj", toggles.soft_jailbreak_draft),
        ("mm", toggles.mention_marker),
    ):
        if mode != "off":
            _stage(key)  # 켠 단계는 돌지 않아도 기록 칸이 있다(trig 0)
    jobs: list[GateJob] = []  # 이 요청이 띄운 E(많아야 하나). finally에서 취소한다

    async def begin(gate_deadline: float) -> None:
        try:
            job = await _start_gate(req, state, gate_deadline)
        except Exception:  # 메시지는 남기지 않는다
            counts = getattr(state, "oom_gate_counts", None)
            if counts is not None:
                counts["start_error"] += 1
            return
        if job is not None:
            jobs.append(job)
            await asyncio.sleep(0)  # E 요청이 뒤따르는 호출보다 먼저 나가도록 한 번 양보한다

    later = None
    if toggles.oom_gate != "off":
        if toggles.oom_schedule == "with_request":
            await begin(deadline)
        else:
            started = asyncio.get_running_loop().time()
            gate_deadline = (
                deadline
                if toggles.oom_schedule == "after_intent_fresh"
                else min(deadline, started + state.tutor_cfg.llm.timeout_s.oom_gate)
            )

            async def later() -> None:
                if not jobs:  # 한 요청에 한 번만
                    await begin(gate_deadline)

    try:
        seen: dict[str, Any] = {}  # 요청 안에서만: sj가 쓸 주 호출 메시지·sj가 답을 냈나
        body = await _answer_v2(req, state, deadline=deadline, begin_gate=later, seen=seen)
        counts = getattr(state, "oom_gate_counts", None)
        gate = jobs[0] if jobs else None
        if (
            gate is not None
            and body.get("action") == "answer"
            and await material_absent(gate, counts)
        ):
            return tutor_post(fixed_body(Fixed.ABSTAIN), "out_of_material")
        replaced = (
            None if seen.get("sj_used") else await _reverse_question(req, state, body, deadline)
        )  # sj가 낸 answer에는 rq를 돌리지 않는다(한 요청에 단계 하나, 호출 상한 8의 전제)
        if replaced is not None:
            return replaced
        return apply_cite_rule(state, seen, body, append_quote(req, state, body))
    finally:
        for job in jobs:
            job.cancel()
        task = seen.get("redraft_task")
        if task is not None and not task.done():
            task.cancel()  # 쓰지 않은 1위 초안(요청이 끝난 뒤 남는 작업이 없게, TU-18)


QUESTION_ENDS = ("?", "？")  # 덧붙이지 않는 문장 끝(자료가 던지는 질문, 점검 세트 리뷰 11/17)
CLOSERS = "\"'”’)」』]"  # 물음표 뒤에 붙는 닫는 따옴표·괄호(벗기고 본다)
# 이 신호가 있으면 answer여도 덧붙이지 않는다: 위기·지시 무력화 발화에 자료 설명을 더하지 않고,
# 정답만 요구하면 정답 문장("정답은 …")이 붙지 않게(#225 가드 리뷰). 공개·점검 세트에서 이 신호가
# 켜지는 행은 모두 정답 action이 answer가 아니다(탈옥 17·위기 9·정답 요구 8, normal 0)
NO_QUOTE_SIGNALS = frozenset({Signal.CRISIS, Signal.OVERRIDE, Signal.ANSWER_ONLY})
_HANGUL = re.compile(r"[가-힣ㄱ-ㆎ]")
_LATIN = re.compile(r"[A-Za-z]")
_NOT_WORD = re.compile(r"[^0-9A-Za-z가-힣]")


def _english_only(text: str) -> bool:
    """한글(완성 음절·호환 자모)이 한 글자도 없고 라틴 글자가 있다(#213 E의 영어 규칙과 같다)."""
    return _HANGUL.search(text) is None and _LATIN.search(text) is not None


def append_quote(req: TutorRequest, state: Any, body: dict[str, Any]) -> dict[str, Any]:
    """`toggles.quote_append`(TU-07): 최종 answer에 인용 섹션(기본 첫 인용)의 자료 문장을 붙인다.

    `toggles.quote_from_top1`이면 인용이 `max_citations`보다 많을 때(모델 인용은 그 수까지라
    마지막 인용이 `cite_top1`이 더한 1위다) 그 1위 섹션에서 고른다. 그 섹션에서 고를 문장이 없으면
    첫 인용으로 돌아가지 않고 붙이지 않는다.

    문장 고르기는 자료 인용 답(TU-12)과 같다(`quote_sentences`: 질문과 2-gram이 가장 겹치는 설명
    문장 최대 2개, 퀴즈 답·표·코드 줄 제외, 문장부호·목록 기호 정리). 그중 물음표로 끝나는
    문장(자료가 학습자에게 던지는 질문)과 답에 이미 있는 문장은 빼고, 붙이면 4,000자를 넘거나
    붙일 문장이 없으면 그대로 둔다. 판단 대상에 한글이 없고 라틴 글자만 있는 영어 질문에도 붙이지
    않는다(문자 2-gram이 코드 낱말로만 겹친다). 위기·지시 무력화·정답만 요구 신호
    (`NO_QUOTE_SIGNALS`)가 있는 발화에도 붙이지 않는다. 답에 이미 있는지는 글자·숫자만 남겨
    본다(백틱·마침표 차이 무시).
    answer가 아닌 응답·인용 없는 응답·KB에 없는 인용, 그리고 모델 문장이 모두 걸러져 "자료에서
    확인하지 못했어요" 안내가 든 답(빈 답변, IN-04-3)은 그대로다(안내와 자료 문장이 한 답에서
    부딪히지 않게). action·인용은 바꾸지 않는다.
    붙이는 글은 KB 원문과 고정 문구뿐이라 출력 후처리(`tutor_post`) 뒤에 붙인다. 입력을 바꾸지
    않는다. 요청 내용은 지역 변수로만 쓴다(TU-18).
    """
    if not state.tutor_cfg.toggles.quote_append or body.get("action") != "answer":
        return body
    answer = body.get("answer") or ""
    if NOT_IN_MATERIAL_NOTE in answer:
        return body
    cites = body.get("citations") or []
    toggles = state.tutor_cfg.toggles
    from_top1 = toggles.quote_from_top1 and len(cites) > toggles.max_citations
    section = state.tutor_kb.sections.get(cites[-1] if from_top1 else cites[0]) if cites else None
    if section is None:
        return body
    conv = tidy(_messages(req))
    if _english_only(normalize_fast(conv.target_raw)):
        return body  # 영어 질문은 코드 낱말로만 겹쳐 엉뚱한 문장이 골라지기 쉽다(#225 리뷰)
    if NO_QUOTE_SIGNALS.intersection(detect(conv)):
        return body
    query, _ = conv.search_query()
    bare_answer = _NOT_WORD.sub("", answer)
    picked = [
        s
        for s in quote_sentences(section.text, query)
        if _NOT_WORD.sub("", s) not in bare_answer
        and not s.rstrip().rstrip(CLOSERS).endswith(QUESTION_ENDS)
    ]
    if not picked:
        return body
    joined = f"{answer} {QUOTE_APPEND_LEAD} {' '.join(picked)}"
    if len(joined) > postprocess.MAX_ANSWER_CHARS:
        return body
    return {**body, "answer": joined}


# 이 신호가 있으면 E를 부르지 않는다. 맞는 답이 escalate·refuse라 E(answer → abstain)가 고칠 것이
# 없고 nano 1회만 든다(#232 가드 리뷰). 공개 세트에서 named_term이 걸리는 14행에는 이 신호가 없다
GATE_SKIP_SIGNALS = frozenset({Signal.CRISIS, Signal.OVERRIDE})


def _english_embedding(state: Any) -> dict[str, Any]:
    """`toggles.english_embedding`이 켜졌으면 retrieve에 넘길 인자(영어 질의는 임베딩 순위만).
    기동 때 임베딩을 못 만들었으면 `embedding`이 None이라 BM25다. 꺼졌으면 빈 인자.
    `toggles.code_block_lang`이 켜졌으면 코드 블록 때문에 영어로 보인 한국어 질문을 가르는
    인자도 넘긴다."""
    toggles = state.tutor_cfg.toggles
    if not toggles.english_embedding:
        return {}
    extra = {"embedding": getattr(state, "tutor_emb", None), "english_only_embedding": True}
    if toggles.code_block_lang:
        extra["code_block_lang"] = True
    return extra


async def _start_gate(req: TutorRequest, state: Any, deadline: float) -> GateJob | None:
    """E 호출 시작. 프롬프트가 없거나(기동 때 읽지 않음) 저신호 직접 판정·위기·지시 무력화 신호·영어
    발화면 부르지 않는다. `oom_gate: named_term`이면 판단 대상에 KB에 없는 이름 붙은 말이 없을
    때도 부르지 않는다.

    `toggles.english_embedding`이 켜져 있고 기동 때 만든 임베딩이 있으면 영어 발화도 부른다(이름
    붙은 말 검사는 영어에 뜻이 없어 건너뛴다). 임베딩이 없으면 영어 발화는 지금처럼 건너뛴다.

    근거 묶음은 주 호출과 같은 질의·k의 BM25 검색이다(영어 발화는 임베딩 순위만). 영어 판정은
    창 본문이 아니라 판단 대상 원문을 정규화해서 본다(창 본문의 "[숨긴 문자: …]"에 한글이 있다)."""
    system = getattr(state, "tutor_oom_gate_prompt", None)
    if system is None:
        return None
    conv: Conversation = tidy(_messages(req))
    if direct_clarify(conv):
        return None
    counts = getattr(state, "oom_gate_counts", None)
    if GATE_SKIP_SIGNALS.intersection(detect(conv)):
        if counts is not None:
            counts["signal"] += 1
        return None
    cfg = state.tutor_cfg
    english = english_only(normalize_fast(conv.target_raw))
    by_embedding = english and getattr(state, "tutor_emb", None) is not None
    by_embedding = by_embedding and cfg.toggles.english_embedding
    if english and not by_embedding:
        if counts is not None:
            counts["english"] += 1
        return None
    if by_embedding:
        if counts is not None:
            counts["english_called"] += 1
    elif cfg.toggles.oom_gate == "named_term" and not _named_term(state, conv):
        if counts is not None:
            counts["no_term"] += 1
        return None
    query, history = conv.search_query()
    sections = await retrieve(
        query, history, k=cfg.retrieval.k, index=state.tutor_kb, **_english_embedding(state)
    )
    messages = build_gate_messages(system, sections, conv)
    return start_gate(
        state.tutor_llm,
        messages,
        timeout_s=cfg.llm.timeout_s.oom_gate,
        deadline=deadline,
        counts=counts,
    )


def _named_term(state: Any, conv: Conversation) -> bool:
    """판단 대상(창 본문)에 KB 전체 글에 없는 이름 붙은 말이 있는가(`oom_gate: named_term`).

    KB 글이 없으면(기동 때 만들지 않음) False라 E를 부르지 않는다."""
    blob = getattr(state, "tutor_kb_blob", None)
    if blob is None:
        return False
    target = next((t.text for t in conv.window if t.is_target), "")
    return bool(missing_terms(target, blob))


async def _answer_v2(
    req: TutorRequest,
    state: Any,
    *,
    deadline: float,
    begin_gate: Callable[[], Awaitable[None]] | None = None,
    seen: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """v2 흐름 본체. 예외는 올린다(라우터가 클래스 이름만 기록하고 기본값을 낸다).

    `begin_gate`가 있으면 주 호출 바로 앞에서 한 번 부른다(`oom_schedule: after_intent`의 E 시작).

    가드 classify는 주 호출과 동시에 시작한다. 이 함수가 취소·예외로 빠져나가면 아직 도는 classify도
    함께 취소한다(요청이 끝난 뒤 남는 작업이 없게, TU-18).
    """
    conv: Conversation = tidy(_messages(req))
    if direct_clarify(conv):
        return tutor_post(fixed_body(Fixed.CLARIFY))  # TU-05: 빈 발화, 앞 대화 뒤의 자모·기호만
    cfg = state.tutor_cfg
    seen = {} if seen is None else seen  # sj가 쓸 주 호출 메시지·스키마(요청 안에서만)
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
    intent: IntentJob | None = None
    try:
        # T는 직렬 classify(ⓐ 비교 기준)에서는 부르지 않는다(주 호출 없이 끝나는 경로가 있다)
        intent = None if serial else _start_intent(state, conv, signals, deadline)
        classified: str | None = None
        intent_label: str | None = None
        if classify is not None or intent is not None:
            # classify·T 요청이 주 호출보다 먼저 나가도록 한 번 양보한다(TU-10)
            await asyncio.sleep(0)
        if classify is not None and serial:
            classified = await wait_classify(classify, skips)  # ⓐ: 주 호출 앞에서 끝까지 기다린다
            fixed = serial_fixed(gate_classified(classified, signals, needs_signal=gated))
            if fixed is not None and Signal.CRISIS not in signals:
                # ⓐ: classify가 템플릿 범주면 주 호출 없이 고정 문장(통합 I13 ⓐ, #83 비교용)
                return tutor_post(fixed_body(fixed), classified)
        iskips = getattr(state, "intent_skips", None)
        deferred = None  # 직렬 T의 redirect·scaffold 고정 문장(주 호출이 실패했을 때만 낸다)
        if (
            intent is not None
            and cfg.toggles.intent_override == "serial"
            and (
                cfg.llm.timeout_s.intent_wait >= cfg.llm.timeout_s.intent  # 지금처럼 끝까지
                or await intent_in_time(intent, cfg.llm.timeout_s.intent_wait)
            )
        ):
            # 직렬 T: 주 호출 앞에서 `intent_wait`초까지 기다리고, refuse·위기 고정 문장이면 주
            # 호출 없이 낸다. redirect·scaffold 고정 문장은 주 호출도 불러 parallel과 같은
            # 규칙으로 정한다. 그 안에 T가 안 오면 주 호출을 먼저 보내고 아래 병렬 기다림에서
            # T를 받아 parallel 규칙으로 정한다(느린 T가 주 호출 창을 깎지 않게)
            intent_label = await wait_intent(intent, iskips)
            intent = None  # 받았다(아래 병렬 기다림을 건너뛴다)
            hit = _intent_fixed(intent_label, signals, None)
            if hit is not None and hit[0] in SERIAL_ASKS_MAIN:
                deferred = hit
            elif hit is not None:
                return tutor_post(fixed_body(hit[0]), hit[1])
        if begin_gate is not None:
            await begin_gate()  # 직렬 T가 고정 문장으로 끝내지 않은 요청만 여기에 온다
        multi = cfg.llm.multi_draft.enabled  # 실험: 주 호출 초안 여러 개 + 고르기
        runs: list[MainCallOut | None] = []
        complete = _multi_complete(state, runs) if multi else _complete(state)
        if cfg.toggles.soft_jailbreak_draft != "off":
            complete = _capture(complete, seen)  # sj 초안이 같은 메시지를 쓴다
        if multi and cfg.llm.multi_draft.top1_redraft == "parallel":
            # 1위 섹션 하나만 근거로 둔 초안을 정규 초안과 함께 띄운다(고르기·다수결 밖)
            seen["rule_a"] = await _rule_a_top(state, conv)
            if seen["rule_a"]:
                seen["redraft_task"] = _start_redraft(
                    state, conv, signals, seen["rule_a"][0], deadline
                )
        draft = await draft_answer(
            conv,
            prompt=state.tutor_prompt,
            complete=complete,
            deadline=deadline,
            k=cfg.retrieval.k,
            signals=labels(signals) if cfg.toggles.signals else (),
            embedding=getattr(state, "tutor_emb", None),  # 토글이 켜져 만들어졌을 때만(TU-11)
            english_only_embedding=cfg.toggles.english_embedding,
            code_block_lang=cfg.toggles.code_block_lang,
            examples=examples_block(state, conv),
            index=state.tutor_kb,  # 기동 때 가드 설정으로 만든 색인(TU-03)
        )
        if multi:
            draft = _pick_draft(state, draft, runs)
        if deferred is not None and draft.out is None:
            # 주 호출이 실패했으면 T 고정 문장. 성공했으면 아래 T 덮기(decide)가 parallel과 같은
            # 규칙으로 정한다(주 호출의 escalate·refuse 등 더 강한 action을 지킨다, #233 리뷰)
            return tutor_post(fixed_body(deferred[0]), deferred[1])
        if classify is not None and not serial:
            classified = await wait_classify(classify, skips)  # 자기 시간 제한 안에서 끝난다
        if intent is not None:
            # T 호출 제한과 주 호출이 끝난 때(+여유) 중 늦은 쪽까지 기다린다
            main_done = asyncio.get_running_loop().time() + WAIT_GRACE_S
            intent_label = await wait_intent(intent, iskips, not_before=main_done)
        if draft.out is None:
            quote = (
                material_quote(draft.sections, conv.search_query()[0])
                if cfg.toggles.fallback_quote and _quote_allowed(cfg, intent_label)
                else None
            )
            body = fallback_body(
                signals,
                classified,
                delegation=cfg.toggles.delegation_override,
                quote=quote,
            )
            # T는 기본값의 action을 주 호출 action처럼 보고 고정 문장 조합으로만 덮는다
            blocked = _classify_wins(gate_classified(classified, signals, needs_signal=gated))
            hit = None if blocked else _intent_fixed(intent_label, signals, body["action"])
            if hit is not None:
                return tutor_post(fixed_body(hit[0]), hit[1])
            return tutor_post(body)
    finally:
        if classify is not None:
            classify.cancel()  # 아직 돌면 취소(요청이 끝난 뒤 남는 작업이 없게, TU-18)
        if intent is not None:
            intent.cancel()
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
    # T 덮기(대행 보정 뒤, 합치기 앞). classify가 위기·공격이면 T는 비키고 합치기가 정한다
    decided = None if _classify_wins(trusted) else decide_intent(intent_label, signals, action)
    decided = _mention_marker(cfg, conv, intent_label, decided, action)
    if decided is not None:
        kind = template_for(decided.category, decided.action)
        if kind is not None:
            return tutor_post(fixed_body(kind), decided.category)
        # 고정 문장이 없는 조합(answer_seeking scaffold, 주 호출이 answer일 때만):
        # 주 호출 문장·인용은 그대로, 범주·action만(두 범주 모두 인용 always라 인용이 같다)
        out = out.model_copy(update={"category": decided.category})
        action = decided.action
    merged = merge_category(out.category, action, trusted)
    if merged.fixed is not None:
        # TU-10: classify가 공격 범주로 덮었다. 고정 문장은 판정하지 않는다
        soft = await _soft_jailbreak(state, merged.fixed, signals, seen, draft.sections, deadline)
        return soft or tutor_post(fixed_body(merged.fixed), merged.category)
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
    top_sections = None
    if (
        cfg.toggles.cite_top1
        and cfg.toggles.top1_dialog_query
        and template_for(merged.category, action) is None  # 고정 문장은 인용이 없다
    ):  # 규칙 A: 튜터 발화까지 넣은 질의로 한 번 더 찾아 1위만 쓴다(LLM 0회, BM25만)
        # top1_redraft parallel이 주 호출 앞에서 이미 찾았으면 그 결과(같은 질의)를 쓴다
        top_sections = seen.get("rule_a") or await _rule_a_top(state, conv)
    kind = template_for(merged.category, action)
    if kind is not None:  # 주 호출이 고른 고정 문장(탈옥 거절이면 sj가 볼 수 있다)
        soft = await _soft_jailbreak(state, kind, signals, seen, draft.sections, deadline)
        if soft is not None:
            return soft
    body = assemble(
        out,
        draft.sections,
        action=action,
        max_citations=cfg.toggles.max_citations,
        verdict=verdict,
        cite_top1=cfg.toggles.cite_top1,
        top_sections=top_sections,
    )
    final = tutor_post(body, merged.category)
    if runs and kind is None:  # 고른 초안의 문장이 나가는 경로: 초안별 인용(호출 0회)
        chosen = next((i for i, o in enumerate(runs) if o is draft.out), None)
        both = [
            None
            if o is None
            else _draft_cites(cfg, o, signals, trusted, intent_label, draft.sections, top_sections)
            for i, o in enumerate(runs)
        ]
        md_cites = [None if b is None else b[0] for b in both]
        md_model = [None if b is None else b[1] for b in both]
        top = draft.sections if top_sections is None else top_sections
        top1 = top[0].id if cfg.toggles.cite_top1 and top else None
        switched = False
        if cfg.llm.multi_draft.prefer_top1 == "on" and verdict is None and chosen is not None:
            alt = _top1_draft(state, draft, runs, md_model, chosen, top1)
            path = (
                None if alt is None else _draft_path(cfg, runs[alt], signals, trusted, intent_label)
            )
            if path is not None and path[1] == action and path[2] == merged.category:
                # 1위 섹션을 확인된 근거로 쓴 같은 action 초안의 문장·인용으로 바꾼다(호출 0회)
                body = assemble(
                    path[0],
                    draft.sections,
                    action=action,
                    max_citations=cfg.toggles.max_citations,
                    cite_top1=cfg.toggles.cite_top1,
                    top_sections=top_sections,
                )
                final, chosen, switched = tutor_post(body, merged.category), alt, True
        if chosen is not None:
            md_cites[chosen] = sorted(final["citations"])  # 고른 초안은 실제로 나간 인용
        actions = [None if o is None else o.action for o in runs]
        seen["md"] = (md_cites, actions, chosen, md_model, top1)
        seen["pick_body"] = final
        shadow = multi_draft.SHADOW.get()
        if shadow is not None:
            shadow.md_cites, shadow.md_model_cites, shadow.top1 = md_cites, md_model, top1
            shadow.top1_switch = switched
            if switched:
                shadow.chosen = chosen
        picked = None if chosen is None else runs[chosen]
        rd_mode = cfg.llm.multi_draft.top1_redraft
        if (
            rd_mode != "off"
            and verdict is None  # 가드 판정은 고른 초안의 문장에 대한 것이다
            and picked is not None
            and picked.action == action == "answer"
            and top1 is not None
            and not any(  # 같은 action의 어느 초안도 1위를 확인된 근거로 쓰지 않았다
                o is not None and o.action == picked.action and top1 in (md_model[i] or [])
                for i, o in enumerate(runs)
            )
        ):
            if rd_mode == "on":
                redone = await _top1_redraft(state, conv, signals, top[0], picked, deadline, shadow)
            else:
                redone = await _parallel_redraft(state, seen, top[0], picked, deadline, shadow)
            path = (
                None if redone is None else _draft_path(cfg, redone, signals, trusted, intent_label)
            )
            if (
                redone is not None and redone.action == "abstain"
            ):  # top1_redraft_abstain(used_abstain)
                # 고른 초안이 abstain이었을 때와 같은 뒤 단계를 지난다(대행 보정 → T → 합치기 →
                # assemble·고정 문장 → 후처리, 근거 묶음은 그 초안이 본 1위 섹션 하나)
                final = _downstream(
                    cfg, redone, [top[0]], signals, trusted, intent_label, top_sections
                )
                seen.pop("pick_body")
            elif redone is not None and (
                path is None or path[1] != action or path[2] != merged.category
            ):
                _redraft_mark(shadow, "rejected:path")
            elif path is not None:
                body = assemble(
                    path[0],
                    [top[0]],
                    action=action,
                    max_citations=cfg.toggles.max_citations,
                    cite_top1=cfg.toggles.cite_top1,
                    top_sections=top_sections,
                )
                final = tutor_post(body, merged.category)
                seen.pop("pick_body")  # 고른 초안의 응답이 아니다: cite_rule을 걸지 않는다
                _redraft_mark(shadow, "used")
    return final


def _downstream(
    cfg: Any,
    out: MainCallOut,
    sections: Sequence[RetrievedSection],
    signals: Sequence[Signal],
    trusted: str | None,
    intent_label: str | None,
    top_sections: Sequence[RetrievedSection] | None,
) -> dict[str, Any]:
    """초안 하나를 고른 초안처럼 `_answer_v2`의 뒤 단계(대행 보정 → T 덮기 → 합치기 → assemble·
    고정 문장 → 후처리)에 통과시킨 응답. 호출이 드는 가드 판정·sj와 mm 기록은 뺀다."""
    toggles = cfg.toggles
    out = delegation_guard(
        out,
        signals,
        state_rule=toggles.scaffold_state_rule,
        override=toggles.delegation_override,
        classified=trusted,
    )
    action = choose_action(out, from_category=toggles.action_from_category)
    decided = None if _classify_wins(trusted) else decide_intent(intent_label, signals, action)
    if decided is not None:
        kind = template_for(decided.category, decided.action)
        if kind is not None:
            return tutor_post(fixed_body(kind), decided.category)
        out, action = out.model_copy(update={"category": decided.category}), decided.action
    merged = merge_category(out.category, action, trusted)
    if merged.fixed is not None:
        return tutor_post(fixed_body(merged.fixed), merged.category)
    body = assemble(
        out,
        sections,
        action=action,
        max_citations=toggles.max_citations,
        cite_top1=toggles.cite_top1,
        top_sections=top_sections,
    )
    return tutor_post(body, merged.category)


async def _rule_a_top(state: Any, conv: Conversation) -> list[RetrievedSection] | None:
    """규칙 A(`top1_dialog_query`): 튜터 발화까지 넣은 질의의 검색 결과. 실패하거나 비면 None
    (더할 인용 하나를 고르는 일이라 끔일 때의 1위로 돌아간다)."""
    cfg = state.tutor_cfg
    query, history = conv.search_query(all_roles=True)
    try:
        found = await retrieve(
            query, history, k=cfg.retrieval.k, index=state.tutor_kb, **_english_embedding(state)
        )
    except Exception:  # 메시지는 남기지 않는다
        return None
    return list(found) or None


def _redraft_mark(shadow: multi_draft.Shadow | None, code: str) -> None:
    if shadow is not None:
        shadow.redraft = code


async def _top1_redraft(
    state: Any,
    conv: Conversation,
    signals: Sequence[Signal],
    top: RetrievedSection,
    picked: MainCallOut,
    deadline: float,
    shadow: multi_draft.Shadow | None,
) -> MainCallOut | None:
    """`llm.multi_draft.top1_redraft`: 근거 묶음을 cite_top1 섹션 하나로 줄인 low 초안을 한 번 더
    부른다(재시도 0). 다른 초안과 같은 프롬프트·스키마·신호·판단 예시, 근거 묶음만 다르다.
    마감은 요청 마감 − `multi_draft.margin_s`(medium 초안·기다리기 끝과 같다)이고, 그때까지
    `HEDGE_MIN_LEFT_S`보다 덜 남았으면 부르지 않는다(skip_time). 유효하고 answer이며 고른 초안과
    범주가 같고, 그 섹션을 확인된 근거로 쓴 초안만 돌려준다(아니면 rejected:<사유>, None)."""
    until = deadline - state.tutor_cfg.llm.multi_draft.margin_s
    if until - asyncio.get_running_loop().time() < HEDGE_MIN_LEFT_S:
        _redraft_mark(shadow, "skip_time")
        return None
    parsed, ms, _late = await _redraft_run(state, conv, signals, top, until)
    if shadow is not None:
        shadow.redraft_ms = ms
    return _accept_redraft(state, parsed, top, picked, shadow)


LATE_EPS_S = 0.01  # 마감에 걸려 끝난 호출로 보는 여유(bounded가 until에 끊는다)


async def _redraft_run(
    state: Any, conv: Conversation, signals: Sequence[Signal], top: RetrievedSection, until: float
) -> tuple[Any, float, bool]:
    """1위 섹션 하나만 근거로 둔 low 초안 호출 하나 → (결과 또는 None, ms, 마감에 걸렸나)."""
    cfg = state.tutor_cfg
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    messages = main_call.build_messages(
        state.tutor_prompt,
        conv,
        [top],
        labels(signals) if cfg.toggles.signals else (),
        examples_block(state, conv),
    )
    parsed = await bounded(
        state.tutor_llm,
        REDRAFT_ROLE,
        messages,
        state.tutor_prompt.schema,
        until=until,
        timeout_s=cfg.llm.timeout_s.main,
        effort="low",
    )
    now = loop.time()
    return parsed, (now - t0) * 1000, parsed is None and now >= until - LATE_EPS_S


def _start_redraft(
    state: Any,
    conv: Conversation,
    signals: Sequence[Signal],
    top: RetrievedSection,
    deadline: float,
) -> "asyncio.Future[tuple[Any, float, bool]] | None":
    """`top1_redraft: parallel`: 정규 초안과 함께 1위 초안을 띄운다. 마감은 "on"과 같은 요청 마감 −
    `multi_draft.margin_s`이고, 그때까지 `HEDGE_MIN_LEFT_S`보다 덜 남았으면 띄우지 않는다."""
    until = deadline - state.tutor_cfg.llm.multi_draft.margin_s
    shadow = multi_draft.SHADOW.get()
    if until - asyncio.get_running_loop().time() < HEDGE_MIN_LEFT_S:
        _redraft_mark(shadow, "skip_time")
        return None
    _redraft_mark(shadow, "not_needed")  # 고른 뒤 조건이 맞으면 바뀐다
    return asyncio.ensure_future(_redraft_run(state, conv, signals, top, until))


async def _parallel_redraft(
    state: Any,
    seen: dict[str, Any],
    top: RetrievedSection,
    picked: MainCallOut,
    deadline: float,
    shadow: multi_draft.Shadow | None,
) -> MainCallOut | None:
    """띄워 둔 1위 초안을 받는다. 아직 돌면 "on"과 같은 마감(요청 마감 − margin_s)까지만 기다리고,
    그때도 안 끝났거나 마감에 걸려 끝났으면 late. 받는 규칙은 "on"과 같다(`_accept_redraft`).
    띄우지 않은 요청(1위 없음·시간 부족)은 띄울 때 남긴 기록 그대로 None."""
    task = seen.get("redraft_task")
    if task is None:
        return None
    until = deadline - state.tutor_cfg.llm.multi_draft.margin_s
    left = until - asyncio.get_running_loop().time()
    if not task.done():
        await asyncio.wait({task}, timeout=max(left, 0.0) + LATE_EPS_S)
    if not task.done():
        task.cancel()
        _redraft_mark(shadow, "late")
        return None
    parsed, ms, late = task.result()
    if shadow is not None:
        shadow.redraft_ms = ms
    if late:
        _redraft_mark(shadow, "late")
        return None
    return _accept_redraft(state, parsed, top, picked, shadow)


def _accept_redraft(
    state: Any,
    parsed: Any,
    top: RetrievedSection,
    picked: MainCallOut,
    shadow: multi_draft.Shadow | None,
) -> MainCallOut | None:
    """유효하고 answer이며 고른 초안과 범주가 같고 1위 섹션을 확인된 근거로 쓴 초안만 돌려준다
    (아니면 rejected:<사유>, None). `top1_redraft_abstain`이 on이면 유효한 abstain 초안도
    돌려준다(used_abstain, 고른 초안은 이 단계에 오는 조건상 answer다). 유효한 초안의 action은
    `Shadow.redraft_action`에 남긴다(측정 전용)."""
    out = None if parsed is None else main_call.clean(parsed)
    if out is not None and shadow is not None:
        shadow.redraft_action = out.action
    abstain_on = state.tutor_cfg.llm.multi_draft.top1_redraft_abstain == "on"
    if out is not None and out.action == "abstain" and abstain_on:
        _redraft_mark(shadow, "used_abstain")
        return out
    verified = (
        set()
        if out is None
        else multi_draft.verified_sids(out, multi_draft.contract_sections([top]))
    )
    reason = (
        "fail"
        if out is None
        else "action"
        if out.action != "answer"
        else "category"
        if out.category != picked.category
        else "cite"
        if top.id not in verified
        else None
    )
    if reason is not None:
        _redraft_mark(shadow, f"rejected:{reason}")
        return None
    return out


def _top1_draft(
    state: Any,
    draft: Draft,
    runs: Sequence[MainCallOut | None],
    md_model: Sequence[Sequence[str] | None],
    chosen: int,
    top1: str | None,
) -> int | None:
    """`llm.multi_draft.prefer_top1`: 고른 초안의 모델 인용에 1위(`top1`)가 없고, action이 같은
    다른 유효한 초안의 모델 인용(확인된 근거)에 있으면 그 초안 번호. 여럿이면 고르기와 같은 순서
    (근거 비율 → 확인된 인용 수 → medium → A → 번호). 없으면 None."""
    if top1 is None or top1 in (md_model[chosen] or []):
        return None
    action = runs[chosen].action  # type: ignore[union-attr]
    keep = [
        o
        if o is not None and i != chosen and o.action == action and top1 in (md_model[i] or [])
        else None
        for i, o in enumerate(runs)
    ]
    if not any(keep):
        return None
    efforts = [d.effort for d in state.tutor_cfg.llm.multi_draft.drafts]
    return multi_draft.select(keep, efforts, multi_draft.contract_sections(draft.sections))


def _draft_path(
    cfg: Any,
    out: MainCallOut,
    signals: Sequence[Signal],
    trusted: str | None,
    intent_label: str | None,
) -> tuple[MainCallOut, Action, str] | None:
    """초안 하나를 `_answer_v2`의 결정적 단계(대행 보정 → T 덮기 → 합치기)에 통과시킨 (결과, action,
    범주). 고정 문장이 되면 None. 호출이 드는 단계(가드 판정·sj)와 mm 기록은 뺀다."""
    toggles = cfg.toggles
    out = delegation_guard(
        out,
        signals,
        state_rule=toggles.scaffold_state_rule,
        override=toggles.delegation_override,
        classified=trusted,
    )
    action = choose_action(out, from_category=toggles.action_from_category)
    decided = None if _classify_wins(trusted) else decide_intent(intent_label, signals, action)
    if decided is not None:
        if template_for(decided.category, decided.action) is not None:
            return None
        out, action = out.model_copy(update={"category": decided.category}), decided.action
    merged = merge_category(out.category, action, trusted)
    if merged.fixed is not None or template_for(merged.category, action) is not None:
        return None
    return out, action, merged.category


def _draft_cites(
    cfg: Any,
    out: MainCallOut,
    signals: Sequence[Signal],
    trusted: str | None,
    intent_label: str | None,
    sections: Sequence[RetrievedSection],
    top_sections: Sequence[RetrievedSection] | None,
) -> tuple[list[str], list[str]]:
    """초안이 골라졌다면 `_answer_v2`의 결정적 단계(대행 보정 → T 덮기 → 합치기 → assemble →
    후처리)가 냈을 (인용, 모델 인용), 둘 다 정렬. 모델 인용은 결정적 수정(IN-04)이 고른 인용 가운데
    그 초안의 claim 인용 문구가 원문에서 확인된 섹션이다(검색 1위 채우기·cite_top1 추가는 빠진다).
    호출 0회라 호출이 드는 부분은 뺀다: 가드 판정(`toggles.verify`)은 None으로 보고, sj 초안은
    부르지 않으며(고정 문장이면 []), mm은 기록하지 않는다(기록 칸을 쓰기 때문, 이 브랜치 설정에서
    끔). 규칙 A 1위(`top_sections`)는 고른 초안이 쓴 것을 그대로 쓴다(같은 질의라 같은 결과)."""
    toggles = cfg.toggles
    path = _draft_path(cfg, out, signals, trusted, intent_label)
    if path is None:
        return [], []
    out, action, category = path
    body = assemble(out, sections, action=action, max_citations=toggles.max_citations)
    verified = multi_draft.verified_sids(out, multi_draft.contract_sections(sections))
    model = [c for c in tutor_post(body, category)["citations"] if c in verified]
    if toggles.cite_top1:  # assemble(cite_top1=True)와 같은 추가
        body = add_top_citation(body, sections if top_sections is None else top_sections)
    return sorted(tutor_post(body, category)["citations"]), sorted(model)


def apply_cite_rule(
    state: Any, seen: dict[str, Any], body: dict[str, Any], final: dict[str, Any]
) -> dict[str, Any]:
    """`llm.multi_draft.cite_rule`: `body`가 고른 초안의 assemble 경로가 낸 응답 그대로일 때만
    (`seen["pick_body"]`) 자료 문장 덧붙이기 뒤의 `final`에서 인용 목록만 바꾼다. 답·action은
    그대로다(덧붙일 섹션은 이미 pick 인용으로 골랐다). T·E·rq·sj·고정 문장이 바꾼 응답은 그대로."""
    md = seen.get("md")
    if md is None or seen.get("pick_body") is not body:
        return final
    shadow = multi_draft.SHADOW.get()
    if shadow is not None:
        shadow.cite_path = True
    rule = state.tutor_cfg.llm.multi_draft.cite_rule
    cites = multi_draft.compose_cites(rule, final["citations"], *md)
    return final if cites == final["citations"] else {**final, "citations": cites}


# --- 실험 단계(tutor.judge.specialists). shadow는 기록만, on은 응답을 바꾼다 ---


def _stage(key: str) -> StageRec:
    """이 요청의 단계 기록. 기록 칸(라우터·측정 도구의 Shadow)이 없으면 버리는 기록."""
    shadow = multi_draft.SHADOW.get()
    if shadow is None:
        return StageRec()
    return shadow.stages.setdefault(key, StageRec())


def _capture(complete: Any, seen: dict[str, Any]) -> Any:
    """주 호출 어댑터를 감싸 보낸 메시지·스키마를 `seen`에 둔다(sj 초안이 같은 메시지를 쓴다)."""

    async def wrapped(messages, schema, *, deadline: float) -> CompleteResult:
        seen["messages"], seen["schema"] = messages, schema
        return await complete(messages, schema, deadline=deadline)

    return wrapped


HARMFUL_DEMEANING = "유해-비하"  # T 이름표(tutor.judge.intent.TARGET)


def _mention_marker(
    cfg: Any, conv: Conversation, label: str | None, decided: Any, action: Action
) -> Any:
    """mm: T가 유해-비하로 덮으려 하는데 판단 대상에 언급 표지가 있으면, on에서 덮기를 버리고
    주 호출 action을 둔다. shadow는 기록만. 주 호출이 없는 경로(실패)는 여기에 오지 않는다."""
    mode = cfg.toggles.mention_marker
    if mode == "off" or label != HARMFUL_DEMEANING or decided is None:
        return decided
    rec = _stage("mm")
    marked = has_mention_marker(conv.target_raw)
    rec.trig, rec.verdict = True, str(int(marked))
    rec.before, rec.after = decided.action, action if marked else decided.action
    if marked and mode == "on":
        rec.changed = True
        return None
    return decided


# sj를 돌리지 않는 신호: 지시 무력화(진짜 탈옥), 위기(escalate가 먼저), 과제 대행(대행 덮기 몫)
SJ_BLOCKERS = frozenset({Signal.OVERRIDE, Signal.CRISIS, Signal.DELEGATION})


async def _soft_jailbreak(
    state: Any,
    kind: Fixed,
    signals: Sequence[Signal],
    seen: dict[str, Any],
    sections: Sequence[RetrievedSection],
    deadline: float,
) -> dict[str, Any] | None:
    """sj: 지시 무력화 신호 없는 탈옥 거절이면 "정상으로 보고 개념 설명" low 초안을 하나 더 부른다.

    초안이 answer이고 결정적 수정 뒤에도 인용 있는 answer(빈 답 안내 아님)면 used. on에서만 그 답을
    낸다(None이면 거절 그대로). 예외·실패·시간 부족은 kept, 초안이 왔지만 쓸 수 없으면 invalid."""
    cfg = state.tutor_cfg
    mode = cfg.toggles.soft_jailbreak_draft
    if (
        mode == "off"
        or kind != Fixed.REFUSE_JAILBREAK
        or not SJ_BLOCKERS.isdisjoint(signals)
        or "messages" not in seen
    ):
        return None
    rec = _stage("sj")
    rec.trig, rec.before, rec.after, rec.verdict = True, "refuse", "refuse", "kept"
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    body = None
    try:
        parsed = await bounded(
            state.tutor_llm,
            SJ_ROLE,
            sj_messages(seen["messages"]),
            seen["schema"],
            until=deadline - cfg.llm.soft_jailbreak_draft.margin_s,
            timeout_s=cfg.llm.timeout_s.main,
            effort="low",
        )
        if parsed is not None:
            rec.verdict = "invalid"
            out = main_call.clean(parsed)
            if out.action == "answer":
                candidate = assemble(
                    out.model_copy(update={"category": "normal"}),
                    sections,
                    action="answer",
                    max_citations=cfg.toggles.max_citations,
                    cite_top1=cfg.toggles.cite_top1,
                )
                answer = candidate.get("answer") or ""
                if (
                    candidate.get("action") == "answer"
                    and candidate.get("citations")
                    and NOT_IN_MATERIAL_NOTE not in answer
                ):
                    body = candidate
    except Exception:  # noqa: BLE001 - 단계 실패는 거절을 그대로 둔다(메시지는 남기지 않는다)
        body = None
    rec.ms = (loop.time() - t0) * 1000
    if body is None:
        return None
    rec.verdict, rec.after = "used", "answer"
    if mode != "on":
        return None
    rec.changed = True
    seen["sj_used"] = True
    return tutor_post(body, "normal")


CODE_FENCE = "`" * 3  # 판단 대상에 코드 블록이 있으면 rq를 돌리지 않는다


async def _reverse_question(
    req: TutorRequest, state: Any, body: dict[str, Any], deadline: float
) -> dict[str, Any] | None:
    """rq: 최종 answer(자료 문장 덧붙이기 전)의 문장만으로 되살린 질문을 학습자 질문과 견준다.

    다른 속성·다른 대상(신뢰도 `min_confidence` 이상)이면 on에서 자료 밖 고정 문장(E와 같은 길).
    판단 대상에 코드 블록이 있거나, 빈 답 안내가 든 답이거나, sj가 낸 answer면 돌지 않는다.
    예외·실패·시간 부족은 fail이고 응답을 바꾸지 않는다."""
    cfg = state.tutor_cfg
    mode = cfg.toggles.reverse_question
    prompts = getattr(state, "tutor_rq_prompts", None)
    if mode == "off" or prompts is None or body.get("action") != "answer":
        return None
    answer = body.get("answer") or ""
    if not answer or NOT_IN_MATERIAL_NOTE in answer:
        return None
    conv = tidy(_messages(req))
    if CODE_FENCE in conv.target_raw:
        return None
    rec = _stage("rq")
    rec.trig, rec.before, rec.after = True, "answer", "answer"
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    try:
        code, conf = await reverse_question(
            state.tutor_llm,
            prompts,
            cfg.llm.reverse_question,
            answer=answer,
            question=conv.target_raw,
            deadline=deadline,
        )
    except Exception:  # noqa: BLE001 - 단계 실패는 답을 그대로 둔다
        code, conf = "fail", 0
    rec.ms = (loop.time() - t0) * 1000
    rec.verdict, rec.conf = RQ_CODE.get(code, "fail"), conf
    if code not in RQ_ABSTAIN or conf < cfg.llm.reverse_question.min_confidence:
        return None
    rec.after = "abstain"
    if mode != "on":
        return None
    rec.changed = True
    return tutor_post(fixed_body(Fixed.ABSTAIN), "out_of_material")
