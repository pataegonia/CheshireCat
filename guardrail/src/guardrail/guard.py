"""판정기: 결과 타입, 상수 판정기, 조립(Guard) (master-plan §4.1·§4.2, C2-judges S2.8).

서버는 Judgement.response만 직렬화한다. 나머지 필드는 하네스용이고 로그에 남기지 않는다.
Guard.judge는 예외를 밖으로 내지 않는다(GR-09). 실패는 기본값 응답과 오류 코드로 남는다.
"""

import asyncio
import dataclasses
import html
import time
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

from guardrail.classifier import LrClassifier
from guardrail.config import GuardConfig, load_prompt
from guardrail.data import LabeledRow, labeled_rows, load_public
from guardrail.decide import (
    Tier,
    fallback_category,
    output_verdict,
    recheck_block,
    request_category,
    safe_of,
    trusted,
)
from guardrail.judges.input_judge import (
    act_config,
    build_window,
    check_oom,
    check_request,
    judge_input,
)
from guardrail.judges.output_judge import (
    MAX_SENTENCES,
    ask_grounded,
    flagged_sentences,
    output_context,
    quote_ok,
    repair_quote,
    sentence_span,
)
from guardrail.judges.schemas import OutputBaselineOut, OutputOut
from guardrail.judges.schemas_exp import OutputSentOut
from guardrail.kb import Hit, KbIndex
from guardrail.knn import KnnIndex
from guardrail.llm import LLM
from guardrail.preprocess import clip, decode_tags, normalize, reveal_hidden, strip_tags
from guardrail.rules import literal_rule, sentences, tidy_span
from guardrail.schema import (
    Category,
    GuardRequest,
    GuardResponse,
    Message,
    OutputVerdict,
    _messages,
)
from guardrail.signals import (
    Signals,
    has_hidden_tags,
    is_low_signal,
    is_refusal,
    is_self_intro,
)

# nano 호출은 스스로 마감 안에 끝난다(llm.complete). 그 결과를 받을 여유만 둔다
DEADLINE_GRACE_S = 0.2
# 출력 medium 호출(output.medium_parallel)은 요청 마감보다 이만큼 먼저 끊는다(low 답을 낼 여유)
MEDIUM_MARGIN_S = 0.5
NO_SIGNALS = Signals(False, False, False)
CATEGORIES: tuple[str, ...] = get_args(Category)  # 튜플: 해시할 수 없는 값도 TypeError 없이 비교

# given = 튜터 공유 API(judge_output)가 범주를 넘겨 입력 nano를 부르지 않았다
InputSource = Literal["low_signal", "nano", "third_call", "fallback", "default", "given"]


@dataclass(frozen=True)
class Judgement:
    response: GuardResponse
    input_source: InputSource
    output_tier: Tier | None  # GR-07 표의 ①–④
    nano_grounded: bool | None  # 출력 nano 원값
    signals: Signals
    knn_major: Category | None
    lr_pred: Category | None
    calls: int  # 시작한 nano 호출 수(재시도는 세지 않는다. 실제 전송 수는 LLM.calls)
    errors: tuple[str, ...]  # 오류 코드만
    latency_ms: Mapping[str, float]  # 단계별
    # 출력 nano의 인용이 자른 원문·문맥의 부분 문자열인가(R14-D2). 인용 원문은 두지 않는다
    answer_quote_ok: bool | None = None
    kb_quote_ok: bool | None = None
    nano_refuses: bool | None = None  # 기록만 하고 safe에 쓰지 않는다(R14-D3)
    quote_repaired: bool | None = (
        None  # S3.13 인용을 근사 일치로 고쳐 썼는가(nano 결과가 없으면 None)
    )
    # output.medium_parallel로 두 호출을 보냈을 때 쓴 쪽: med | low | default(둘 다 실패)
    output_source: str | None = None
    # input.ensemble로 1차를 3표로 냈을 때의 표 "A,B,C"(범주 이름, 무효는 -). 하네스 기록용
    input_votes: str | None = None


class ConstantGuard:
    """상수 판정(S1.2–S1.5 기준선): allow·normal, 후보 답변이 있으면 true/[]/safe=true."""

    async def judge(self, req: GuardRequest) -> Judgement:
        output = OutputVerdict(True, (), True) if req.has_candidate else None
        return Judgement(
            response=GuardResponse("normal", output),
            input_source="default",
            output_tier=None,
            nano_grounded=None,
            signals=NO_SIGNALS,
            knn_major=None,
            lr_pred=None,
            calls=0,
            errors=(),
            latency_ms={},
        )


def unsupported_switches(cfg: GuardConfig) -> list[str]:
    """Guard가 아직 지원하지 않는데 켜져 있는 설정 이름. 조용히 무시하지 않으려고 미리 본다.

    규칙 ②의 미구현 값(bare_numeral·rule_require_same_kind)은 설정 로드가 먼저 막는다(config.py).
    """
    t = cfg.toggles
    checks = (
        ("third_call", t.third_call),
        ("oom_kb_snippet", t.oom_kb_snippet),
        ("embedding", t.embedding),
    )
    return [name for name, on in checks if on]


def build_indexes(
    cfg: GuardConfig, rows: Sequence[LabeledRow] | None = None
) -> tuple[KbIndex, KnnIndex | None, LrClassifier | None]:
    """KB·kNN·LR 색인(기동 때 한 번). rows가 None이면 공개 400행. 꺼진 보조 판정기는 안 만든다."""
    if rows is None:
        rows = labeled_rows(load_public(cfg.paths.public))
    t = cfg.toggles
    kb = KbIndex.load(cfg.paths.kb, ngram=cfg.kb.ngram, k1=cfg.kb.k1, b=cfg.kb.b)
    knn = KnnIndex(rows, cfg) if t.knn_fewshot or t.knn_vote else None
    lr = LrClassifier(rows, cfg) if t.lr else None
    return kb, knn, lr


class GuardUnavailable(RuntimeError):
    """튜터 공유 API가 판정하지 못했다. 메시지에 본문·범주 값을 넣지 않는다(GR-12)."""


def _as_messages(raw: object) -> tuple[Message, ...] | None:
    """튜터가 넘긴 대화 → Message 튜플. `Message`는 그대로, dict는 /guard 본문과 같은 규칙
    (`schema._messages`)으로 읽는다. list·tuple이 아니면 빈 대화다(GR-09: 예외를 내지 않는다).
    읽다가 예외가 나면 None: 예외 메시지에 본문이 들 수 있어 밖으로 내지 않는다(GR-12)."""
    if not isinstance(raw, list | tuple):
        return ()
    try:
        out: list[Message] = []
        for m in raw:
            out.extend((m,) if isinstance(m, Message) else _messages([m]))
        return tuple(out)
    except Exception:  # noqa: BLE001
        return None


def _consume(task: asyncio.Task) -> None:
    """태스크의 예외를 회수한 것으로 표시한다(GR-12: 예외 메시지가 로그로 새지 않게)."""
    if not task.cancelled():
        task.exception()


@dataclass(frozen=True)
class _Prepared:
    """호출 전 CPU 단계의 결과. 요청 하나 동안만 산다."""

    target: str  # 마지막 학습자 발화를 `preprocess.clip`으로 줄인 글(앞 + 뒤)
    window: list[Message]  # 내용을 같은 방식으로 줄인 창
    low: bool
    knn_major: Category | None
    lr_pred: Category | None
    examples: list[LabeledRow]
    answer: str | None  # `input.max_answer_chars`로 자른 원문. 읽을 답변이 없으면 None
    signals: Signals
    hits: list[Hit]  # 출력 nano에 줄 문맥
    rule_spans: tuple[str, ...] | None
    revealed: str  # 숨긴 문자를 드러낸 답변(출력 nano용)
    output_error: str | None = None  # 출력 쪽 준비(KB 검색·규칙 ②)가 실패했을 때의 오류 코드
    oom_hits: tuple[Hit, ...] = ()  # 자료 밖 확인(`oom_check`)에 줄 문맥. 꺼졌으면 빈 값


@dataclass(frozen=True)
class _Output:
    verdict: OutputVerdict | None
    tier: Tier | None = None
    nano_grounded: bool | None = None
    answer_quote_ok: bool | None = None
    kb_quote_ok: bool | None = None
    nano_refuses: bool | None = None
    quote_repaired: bool | None = None


@dataclass(frozen=True)
class _NanoRead:
    """출력 nano 값을 읽은 결과. answer_ok는 인용 자체의 검증, answer_trusted는 ③ 판단용."""

    ungrounded: bool | None = None
    refuses: bool | None = None
    answer_ok: bool | None = None
    kb_ok: bool | None = None
    answer_trusted: bool | None = None
    quote_span: str | None = None
    repaired: bool | None = None


class Guard:
    """입력 범주 판정과 출력 근거 판정을 묶는다. 만든 뒤 색인은 바뀌지 않는다.

    튜터 공유 API(S2.11, interface.md §2.1): `classify`·`judge_output`. 범주 → 튜터 action
    (PRD §9 T3): normal·pii → answer, answer_seeking·academic → scaffold, out_of_material →
    abstain, jailbreak → refuse, harmful → refuse/redirect, off_topic → redirect, emotional →
    escalate, low_signal → clarify. 시간 제한은 튜터가 바깥에서 건다(취소는 그대로 올라간다).
    취소되면 안쪽 nano 태스크를 회수하느라 최대 `DEADLINE_GRACE_S`(0.2초) 늦게 돌아올 수 있다.
    재시도는 하지 않는다. nano 호출: classify 1회, judge_output은 category가 있으면 1회·없으면 2회
    (병렬). low-signal 직행·학습자 발화 없음이면 더 적다.
    """

    def __init__(
        self,
        cfg: GuardConfig,
        llm: LLM,
        kb: KbIndex,
        knn: KnnIndex | None,
        lr: LrClassifier | None,
    ) -> None:
        # 아직 없는 기능을 켠 설정은 여기서 막는다(조용히 무시하면 ablation이 "효과 없음"이 된다)
        missing = unsupported_switches(cfg)
        if missing:
            raise NotImplementedError(f"Guard가 아직 지원하지 않는 설정: {', '.join(missing)}")
        # 프롬프트는 첫 요청이 아니라 여기서 읽는다(경로가 틀리면 기동에서 드러난다)
        baseline = cfg.output.mode == "baseline"
        for name in ("input_judge", "output_judge_baseline" if baseline else "output_judge"):
            load_prompt(cfg, name)
        if cfg.toggles.jb_check:
            load_prompt(cfg, "jb_check")
        if cfg.input.ensemble:
            load_prompt(act_config(cfg), "input_judge")
        if cfg.toggles.oom_check:
            load_prompt(cfg, "oom_check")
        if cfg.toggles.request_check or cfg.toggles.block_recheck:
            load_prompt(cfg, "request_check")
        if cfg.output.judge_mode == "sentences" and not baseline:
            load_prompt(cfg, "output_judge_sent")
        if cfg.output.medium_parallel and "output_judge_medium" not in cfg.llm.per_role:
            # 없으면 medium 호출이 low와 같은 값으로 나가 호출만 늘어난다
            raise ValueError("output.medium_parallel은 llm.per_role.output_judge_medium이 필요하다")
        self.cfg, self.llm, self.kb, self.knn, self.lr = cfg, llm, kb, knn, lr
        self.block = frozenset(cfg.block_categories)

    @classmethod
    async def build(
        cls, cfg: GuardConfig, *, llm: LLM | None = None, rows: Sequence[LabeledRow] | None = None
    ) -> "Guard":
        """llm이 없으면 만들고 start()(probe 1회)한다. 주입한 llm은 이미 start()된 것으로 본다."""
        if llm is None:
            llm = LLM(cfg.llm)
            await llm.start()
        return cls(cfg, llm, *await asyncio.to_thread(build_indexes, cfg, rows))

    async def judge(self, req: GuardRequest) -> Judgement:
        t = self.cfg.toggles
        return await self._run(
            req,
            intro_rule=t.pii_intro_rule,
            oom_check=t.oom_check,
            request_check=t.request_check,
            block_recheck=t.block_recheck,
            medium=self.cfg.output.medium_parallel,
            ensemble=self.cfg.input.ensemble,
        )

    async def classify(self, messages: list[dict[str, str]] | Sequence[Message]) -> Category:
        """마지막 학습자 발화의 범주(nano·low-signal 직행은 `judge`와 같다).

        nano 1회(1차가 탈옥이면 2차 판정까지 2회), 재시도 없음.

        nano 실패·판정 오류면 `GuardUnavailable`(kNN·LR 추측값을 성공처럼 주지 않는다. 튜터는
        그때 주 호출 범주만 쓴다). 학습자 발화가 없으면 실패가 아니라 "normal"이다.
        """
        msgs = _as_messages(messages)
        if msgs is None:
            raise GuardUnavailable("대화를 읽지 못했다")
        j = await self._run(GuardRequest(msgs, None, False), retries=0)
        if j.input_source not in ("nano", "low_signal") and "no_user_message" not in j.errors:
            raise GuardUnavailable("입력 범주를 판정하지 못했다")
        return j.response.category

    async def judge_output(
        self,
        messages: list[dict[str, str]] | Sequence[Message],
        candidate_answer: str,
        category: Category | None = None,
    ) -> OutputVerdict:
        """후보 답변의 근거·safe 판정. 재시도 없음. 실패해도 예외 없이 값을 낸다.

        출력 nano가 실패하면 nano 없이 GR-07을 계산한다: block 범주면 ①(true), 규칙 ②가 걸리면
        false와 그 구간, 그 밖에는 `output.failure_default`. 가드 예외·학습자 발화 없음이면 규칙 ②도
        없이 ①·`output.failure_default`만 쓴다. category가 있으면 safe는 이 모든 경우에 GR-08 규칙
        그대로다. category가 None인데 가드 예외가 나면 `/guard`와 같이 safe=true다.

        category가 있으면 입력 nano 없이 그 범주로 GR-07 ①과 safe를 계산한다(low-signal로 덮지
        않는다). 판정이 실패하거나 학습자 발화가 없어도 그 범주로 ①·safe(GR-08)는 계산한다.
        None이면 `judge`처럼 입력·출력 nano를 병렬로 부르고, 입력 nano가 실패하면 `judge`처럼
        kNN·LR 폴백 범주로 계산한다(튜터는 늘 category를 넘긴다).
        """
        if category is not None and category not in CATEGORIES:
            raise ValueError("category가 Category 값이 아니다")  # 받은 값은 넣지 않는다(GR-12)
        answer = candidate_answer if isinstance(candidate_answer, str) else None
        msgs = _as_messages(messages)
        req = GuardRequest(() if msgs is None else msgs, answer, True)  # 못 읽으면 발화 없음
        out = (await self._run(req, category, retries=0)).response.output
        return cast(OutputVerdict, out)  # has_candidate=True라 None이 오지 않는다

    # --- 내부 ---

    async def _run(
        self,
        req: GuardRequest,
        category: Category | None = None,
        retries: int | None = None,
        intro_rule: bool = False,
        oom_check: bool = False,
        request_check: bool = False,
        block_recheck: bool = False,
        medium: bool = False,
        ensemble: bool = False,
    ) -> Judgement:
        t0 = time.monotonic()
        try:
            return await self._judge(
                req, t0, category, retries, intro_rule, oom_check, request_check, block_recheck,
                medium, ensemble,
            )  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - GR-09: 어떤 실패에도 규격 응답. 종류 이름만 남긴다
            out = (
                _Output(self._default_output(req)) if category is None
                else self._given_output(req, category)
            )  # fmt: skip
            return self._result(
                "normal", "default", out, None, t0, t0, 0, (f"guard:{type(exc).__name__}",)
            )

    async def _judge(
        self,
        req: GuardRequest,
        t0: float,
        given: Category | None = None,
        retries: int | None = None,
        intro_rule: bool = False,
        oom_check: bool = False,
        request_check: bool = False,
        block_recheck: bool = False,
        medium: bool = False,
        ensemble: bool = False,
    ) -> Judgement:
        """given이 있으면 입력 nano 없이 그 범주를 쓴다. retries는 nano마다(None=설정값).

        oom_check도 `judge`만 켠다: nano가 normal이라 한 발화에 KB를 붙여 한 번 더 묻고, 자료에
        없는 값을 묻는다고 하면 범주를 out_of_material로 낸다(S3.10). 출력은 그대로다.

        request_check·block_recheck(S3.13)도 `judge`만 켠다. 입력 쪽 추가 호출은 탈옥 2차·자료 밖
        확인·요청 재확인 가운데 최대 하나라 `/guard`당 nano는 최대 3회다. 범주를 바꾸면 그 범주로
        decision·출력 단 ①·safe를 계산한다(응답 범주만 바꾸는 oom_check와 다르다).

        medium(output.medium_parallel)도 `judge`만 켠다: 출력 판정을 low·medium 두 호출로 보낸다
        (`_ask_pair`). 그때 `/guard`당 nano는 최대 4회다.

        ensemble(input.ensemble, 실험)도 `judge`만 켠다: 1차를 3표로 내고(`judge_input`) 이어지는
        확인·출력은 합친 범주로 지금과 같이 간다. 1차 호출이 2회 늘어 최대 6회다.

        intro_rule은 `judge`(`/guard` 응답)만 켠다: nano가 normal·answer_seeking이라 한 자기소개
        발화의 범주를 pii_disclosure로 낸다(S3.9). 출력은 nano 범주로 이미 계산한 값 그대로다.
        """
        cfg = self.cfg
        deadline = t0 + cfg.budget_s
        # CPU 단계는 길이 상한으로 자른 뒤 한 번에 스레드로 보낸다(이벤트 루프를 막지 않는다)
        p = await asyncio.to_thread(self._prepare, req)
        wants_kb = oom_check or request_check or block_recheck
        if wants_kb and isinstance(p, _Prepared) and not p.low and given is None:
            p = await asyncio.to_thread(self._with_oom_hits, p)  # classify()는 검색하지 않는다
        t1 = time.monotonic()
        if isinstance(p, Signals) and given is not None:  # 발화는 없지만 범주는 받았다
            j = self._result(
                given, "given", self._given_output(req, given), None, t0, t1, 0,
                ("no_user_message",),
            )  # fmt: skip
            return dataclasses.replace(j, signals=p)
        if isinstance(p, Signals):  # 학습자 발화가 없다(빈 messages 포함): 파싱 실패와 같은 규칙
            out = self._default_output(req)
            if out is not None and p.hidden_tags and cfg.toggles.safe_rule:
                out = OutputVerdict(out.grounded, (), False)  # 숨긴 태그는 그래도 본다(GR-08)
            j = self._result(
                "normal", "default", _Output(out), None, t0, t1, 0, ("no_user_message",)
            )
            return dataclasses.replace(j, signals=p)

        baseline = cfg.output.mode == "baseline"
        tasks: dict[str, asyncio.Task] = {}
        trace: dict[str, str | None] = {}  # 탈옥 2차 판정(입력 태스크 안, 같은 마감)의 결과
        if not p.low and given is None:
            tasks["input_judge"] = asyncio.create_task(
                self._input(
                    p, deadline, retries, trace, oom_check, request_check, block_recheck, ensemble
                )
            )
        wants_nano = baseline or cfg.toggles.nano_tier3 or cfg.output.call_nano_when_unused
        if p.answer and wants_nano and p.output_error is None:
            asking = ask_grounded(
                p.target, p.revealed, p.hits, self.llm, cfg,
                deadline=deadline, baseline=baseline, retries=retries,
            )  # fmt: skip
            if medium and not baseline:
                asking = self._ask_pair(asking, p, deadline, trace)
            tasks["output_judge"] = asyncio.create_task(asking)
        for task in tasks.values():  # 회수하지 못한 예외를 asyncio가 메시지째 로그에 찍지 않게 한다
            task.add_done_callback(_consume)
        errors: list[str] = []
        try:
            if tasks:
                wait_s = max(deadline - time.monotonic(), 0) + DEADLINE_GRACE_S
                await asyncio.wait(tasks.values(), timeout=wait_s)  # 넘어도 취소하지 않는다
            results = {role: self._take(task, role, errors) for role, task in tasks.items()}
        finally:
            late = [t for t in tasks.values() if not t.done()]
            for t in late:
                t.cancel()
            if late:  # 취소를 삼키는 호출이 응답을 붙잡지 못하게 기다림에 상한을 둔다
                await asyncio.wait(late, timeout=DEADLINE_GRACE_S)

        if trace.get("jb_check", "") is None:  # 2차 실패: 1차(탈옥)를 지켰고 오류로 센다
            errors.append("jb_check")
        if given is None:
            category, source = self._category(p, results.get("input_judge"))
        else:
            category, source = given, "given"
        if p.output_error is not None:
            errors.append(p.output_error)
            out = self._fallback_output(req, category, p.signals)
        else:
            try:
                out = self._output(req, p, category, results.get("output_judge"))
            except Exception as exc:  # noqa: BLE001 - 입력 판정은 살리고 출력만 기본값으로
                errors.append(f"output:{type(exc).__name__}")
                out = self._fallback_output(req, category, p.signals)
        if trace.get("oom_check", "") is None:  # 자료 밖 확인 실패: 1차(normal)를 두고 오류로 센다
            errors.append("oom_check")
        if trace.get("request_check", "") is None:  # 요청 재확인 실패: 1차를 두고 오류로 센다
            errors.append("request_check")
        # 입력 쪽 추가 호출은 하나뿐이다. request_check가 oom_check 칸도 채우므로 한 번만 센다
        second = "oom_check" in trace or "request_check" in trace
        calls = len(tasks) + ("jb_check" in trace) + second + ("out" in trace)
        calls += 2 * ("ensemble" in trace)  # 투표자 B·C
        intro = intro_rule and source == "nano" and category in ("normal", "answer_seeking")
        if intro and is_self_intro(p.target):  # 둘 다 허용 범주라 decision·출력 단·safe는 그대로다
            category = "pii_disclosure"  # 자기소개 + 학습 질문은 pii가 먼저다(입력 프롬프트)
        elif trace.get("oom_check") == "not_stated" and source == "nano" and category == "normal":
            category = "out_of_material"  # normal과 같은 허용 범주라 출력·safe는 그대로다
        j = self._result(category, source, out, p, t0, t1, calls, tuple(errors))
        return dataclasses.replace(
            j, output_source=trace.get("out"), input_votes=trace.get("ensemble")
        )

    async def _ask_pair(
        self, low_call: Awaitable[Any], p: _Prepared, deadline: float, trace: dict[str, str | None]
    ) -> Any:
        """출력 판정을 low(기존 호출)와 medium으로 함께 보낸다(output.medium_parallel).

        medium이 끝날 때까지 기다린다(medium은 재시도 없이 read 한도와 `마감 - MEDIUM_MARGIN_S` 중
        이른 쪽에서 끊긴다). medium이 스키마에 맞는 답을 냈으면 그것을 쓰고 low는 버린다(아직 돌고
        있으면 취소). 아니면 low를 끝까지 기다려 쓴다(low는 요청 마감 안에 끝난다). 둘 다 실패면
        None이고 기존 실패 기본값 경로로 간다. 쓴 쪽을 trace["out"]에 적는다(med·low·default).
        """
        low = asyncio.ensure_future(low_call)
        low.add_done_callback(_consume)
        trace["out"] = "default"
        try:
            try:
                best = await ask_grounded(
                    p.target, p.revealed, p.hits, self.llm, self.cfg,
                    deadline=deadline - MEDIUM_MARGIN_S, retries=0, role="output_judge_medium",
                )  # fmt: skip
            except Exception:  # noqa: BLE001 - medium이 깨져도 low 답을 쓴다
                best = None
            if best is not None:
                trace["out"] = "med"
                return best
            out = await low
            if out is not None:
                trace["out"] = "low"
            return out
        finally:
            if not low.done():
                low.cancel()

    async def _input(
        self,
        p: _Prepared,
        deadline: float,
        retries: int | None,
        trace: dict[str, str | None],
        oom_check: bool,
        request_check: bool = False,
        block_recheck: bool = False,
        ensemble: bool = False,
    ) -> Category | None:
        """입력 판정. 1차 뒤에 확인 호출을 최대 하나 이어 부른다(탈옥 2차가 불렸으면 생략).

        - request_check: 1차가 normal·answer_seeking이면 요청 재확인(oom_check 칸을 대신 쓴다).
          offcourse_output일 때만 off_topic으로 바꾼다
        - block_recheck: 1차가 off_topic이면 요청 재확인(academic_dishonesty는 부르지 않는다)
        - oom_check: normal이면 자료 밖 확인
        """
        category = await judge_input(
            p.window, p.target, p.examples, self.llm, self.cfg,
            deadline=deadline, retries=retries, trace=trace, ensemble=ensemble,
        )  # fmt: skip
        if category is None or "jb_check" in trace:
            return category
        opening = request_check and category in ("normal", "answer_seeking")
        if opening or (block_recheck and category == "off_topic"):
            return await self._recheck_request(p, category, deadline, retries, trace, oom_check)
        # KB 문맥이 없으면(검색 실패) 부르지 않는다: 프롬프트가 kb 없음을 not_stated로 읽는다
        if oom_check and not request_check and p.oom_hits and category == "normal":
            try:
                trace["oom_check"] = await check_oom(
                    p.window, p.target, p.oom_hits, self.llm, self.cfg,
                    deadline=deadline, retries=retries,
                )  # fmt: skip
            except Exception:  # noqa: BLE001 - 확인이 깨져도 1차(normal)를 지킨다. trace로 센다
                trace["oom_check"] = None
        return category

    async def _recheck_request(
        self,
        p: _Prepared,
        first: Category,
        deadline: float,
        retries: int | None,
        trace: dict[str, str | None],
        oom_check: bool,
    ) -> Category:
        """S3.13 요청 재확인 1회. 실패(예외·None)면 1차 범주를 두고 trace["request_check"]=None."""
        try:
            out = await check_request(
                p.window, p.target, p.oom_hits, self.llm, self.cfg,
                deadline=deadline, retries=retries,
            )  # fmt: skip
        except Exception:  # noqa: BLE001 - 확인이 깨져도 1차를 지킨다. trace로 센다
            out = None
        if out is None:
            trace["request_check"] = None
            return first
        kinds = [a.kind for a in out.asks]
        trace["request_check"] = ",".join(kinds)
        if first in ("normal", "answer_seeking"):
            if first == "normal" and oom_check and p.oom_hits:  # 기존 out_of_material 규칙이 읽는다
                trace["oom_check"] = out.verdict
            return request_category(first, kinds) or first
        return recheck_block(first, kinds)

    def _with_oom_hits(self, p: _Prepared) -> _Prepared:
        """자료 밖 확인에 줄 KB 문맥(학습자 발화로 검색). `judge`가 확인을 켰을 때만 부른다."""
        try:
            users = normalize("\n".join(m.content for m in p.window if m.role == "user"))
            ranking = self.kb.rank(users[-self.cfg.retrieval.max_query_chars :])
            return dataclasses.replace(
                p, oom_hits=tuple(output_context(ranking, self.kb, self.cfg))
            )
        except Exception:  # noqa: BLE001 - 문맥이 없으면 `_input`이 자료 밖 확인을 건너뛴다
            return p

    def _prepare(self, req: GuardRequest) -> _Prepared | Signals:
        """전처리·신호·kNN·LR·KB 검색·규칙 ②. 받은 글은 모두 상한으로 자른 뒤에 쓴다."""
        cfg, t = self.cfg, self.cfg.toggles
        target = next((m.content for m in reversed(req.messages) if m.role == "user"), None)
        if target is None:  # 판정할 발화가 없다. 답변의 숨긴 태그만 본다
            full = req.candidate_answer
            return Signals(False, False, isinstance(full, str) and has_hidden_tags(full))
        # 앞만 남기면 뒤에 붙인 공격이 빠진다. 판정기와 같은 방식(앞 + 뒤)으로 줄인 글을 본다
        # 여기(스레드)서 줄여 넘긴다. 판정기가 이벤트 루프에서 큰 글을 다시 줄이지 않게
        clipped = target = clip(target, cfg.input.max_chars)
        window = [
            Message(m.role, clip(m.content, cfg.input.max_chars))
            for m in build_window(req.messages, cfg.input.window_last)
        ]
        text = normalize(clipped)[: cfg.retrieval.max_query_chars]
        low = t.low_signal_direct and is_low_signal(
            strip_tags(clipped), len(req.messages) > 1, decode_tags(clipped)[1]
        )
        knn_major = self.knn.majority(text) if t.knn_vote and self.knn else None
        lr_pred = self.lr.predict(text) if t.lr and self.lr else None
        examples = self.knn.fewshot(text) if t.knn_fewshot and self.knn and not low else []

        full = req.candidate_answer
        if not full:  # 없음, 문자열 아님, 빈 문자열: 읽을 답변이 없다
            signals = Signals(low, False, False)
            return _Prepared(
                target, window, low, knn_major, lr_pred, examples, None, signals, [], None, ""
            )
        answer = full[: cfg.input.max_answer_chars]
        # 숨긴 태그 검사만 자르기 전 전체를 본다(GR-02)
        signals = Signals(low, is_refusal(answer), has_hidden_tags(full))
        try:  # 출력 쪽 준비가 실패해도 입력 판정은 그대로 간다(GR-09: 출력 실패는 출력만 바꾼다)
            half = cfg.retrieval.max_query_chars // 2
            users = [m.content for m in window if m.role == "user"]
            # KbIndex는 normalize를 거친 질의를 받는다(숨긴 문자를 끼운 답변도 같은 조각을 찾게)
            ranking = self.kb.rank(
                normalize("\n".join(users))[-half:] + "\n" + normalize(answer)[:half]
            )
            rule_spans = None
            if t.literal_rule:
                top = list(ranking.hits[: cfg.kb.top_k])
                rule_spans = literal_rule(answer, top, ranking.margin, self.kb, cfg)
            hits, revealed = output_context(ranking, self.kb, cfg), reveal_hidden(answer)
        except Exception as exc:  # noqa: BLE001 - 종류 이름만 남긴다
            return _Prepared(
                target, window, low, knn_major, lr_pred, examples, answer, signals, [], None, "",
                f"output:{type(exc).__name__}",
            )  # fmt: skip
        return _Prepared(
            target, window, low, knn_major, lr_pred, examples, answer, signals,
            hits, rule_spans, revealed,
        )  # fmt: skip

    @staticmethod
    def _take(task: asyncio.Task, role: str, errors: list[str]) -> object | None:
        """끝난 태스크의 결과. 마감을 넘겼거나 실패했으면 None과 오류 코드."""
        if not task.done() or task.cancelled():
            errors.append(role)
            return None
        exc = task.exception()
        if exc is not None:
            errors.append(f"{role}:{type(exc).__name__}")
            return None
        result = task.result()
        if result is None:  # 재시도 뒤에도 실패(오류 코드는 LLM이 센다)
            errors.append(role)
        return result

    @staticmethod
    def _category(p: _Prepared, nano: object | None) -> tuple[Category, InputSource]:
        if p.low:
            return "low_signal_input", "low_signal"
        if nano is not None:
            return nano, "nano"  # type: ignore[return-value]
        fallback = fallback_category(p.knn_major, p.lr_pred)
        return fallback, "fallback" if p.knn_major or p.lr_pred else "default"

    def _default_output(self, req: GuardRequest) -> OutputVerdict | None:
        """판정하지 못했을 때의 출력(GR-09): `output.failure_default`, safe=true."""
        if not req.has_candidate:
            return None
        return OutputVerdict(self.cfg.output.failure_default == "grounded", (), True)

    def _fallback_output(self, req: GuardRequest, category: Category, sig: Signals) -> _Output:
        """출력을 만들지 못했을 때: GR-07에서 nano·규칙 ② 없이 계산(GR-09), safe는 GR-08 그대로."""
        cfg = self.cfg
        default = self._default_output(req)
        if default is None:
            return _Output(None)
        safe = safe_of(category, self.block, sig, cfg.toggles)
        if cfg.output.mode == "baseline":
            return _Output(OutputVerdict(default.grounded, (), safe))
        grounded, unsupported, tier = output_verdict(
            category, self.block, None, None, False, cfg.toggles, cfg.output.failure_default
        )
        return _Output(OutputVerdict(grounded, unsupported, safe), tier)

    def _given_output(self, req: GuardRequest, given: Category) -> _Output:
        """받은 범주로 nano 없이(GR-07 ①, GR-08). 판정 오류·학습자 발화 없음일 때(judge_output)."""
        full = req.candidate_answer
        if not full:  # 응한 내용이 없다(또는 문자열이 아니다): 이전과 같은 기본 출력
            return _Output(self._default_output(req))
        answer = full[: self.cfg.input.max_answer_chars]
        return self._fallback_output(
            req, given, Signals(False, is_refusal(answer), has_hidden_tags(full))
        )

    def _output(
        self, req: GuardRequest, p: _Prepared, category: Category, nano: object | None
    ) -> _Output:
        cfg = self.cfg
        if not req.has_candidate:
            return _Output(None)
        if req.candidate_answer == "":  # 응한 내용이 없다
            return _Output(OutputVerdict(True, (), True))
        if cfg.output.mode == "baseline":  # nano 값을 그대로 쓴다. 실패면 true/[]·safe=true
            if isinstance(nano, OutputBaselineOut):
                verdict = OutputVerdict(nano.grounded, (), nano.safe)
                return _Output(verdict, nano_grounded=nano.grounded)
            return _Output(OutputVerdict(True, (), True))

        read = self._read_nano(p, nano)
        grounded, unsupported, tier = output_verdict(
            category,
            self.block,
            p.rule_spans,
            read.ungrounded,
            read.ungrounded is not None
            and trusted(cfg.output.tier3_trust, read.answer_trusted, read.kb_ok),
            cfg.toggles,
            cfg.output.failure_default,
            read.quote_span,
        )
        safe = safe_of(category, self.block, p.signals, cfg.toggles)
        if cfg.output.span_tidy:
            unsupported = tuple(dict.fromkeys(tidy_span(s) for s in unsupported))
        return _Output(
            OutputVerdict(grounded, unsupported, safe),
            tier,
            None if read.ungrounded is None else not read.ungrounded,
            read.answer_ok,
            read.kb_ok,
            read.refuses,
            read.repaired,
        )

    def _read_nano(self, p: _Prepared, nano: object | None) -> "_NanoRead":
        """출력 nano 값 → 근거 없음 여부·인용 검증·③에 쓸 구간. nano 결과가 없으면 모두 None."""
        cfg = self.cfg
        if not (isinstance(nano, OutputOut | OutputSentOut) and p.answer is not None):
            return _NanoRead()
        ungrounded = not nano.grounded
        flagged: list[int] = []
        if isinstance(nano, OutputSentOut):  # 문장 하나라도 어긋나면 근거 없음
            shown = min(len(sentences(p.revealed)), MAX_SENTENCES)
            flagged = flagged_sentences(nano.checks, shown)
            ungrounded = ungrounded or bool(flagged)
        answer_ok = quote_ok(nano.answer_quote, p.answer, cfg.output.quote_min_chars)
        kb_ok = quote_ok(nano.kb_quote, "\n".join(h.text for h in p.hits), 1)
        quote = html.unescape(nano.answer_quote)
        span = quote if answer_ok and quote in p.answer else None  # 구간은 원문에 글자 그대로만
        trusted_answer, repaired = answer_ok, False
        if ungrounded and not answer_ok:  # 인용 검증 실패: 근사 일치 → 문장 대체 순서로 구한다
            fixed = None
            if cfg.output.quote_repair:
                fixed = repair_quote(nano.answer_quote, p.answer, cfg.output.quote_min_chars)
            repaired = fixed is not None
            fixed = fixed or sentence_span(p.answer, p.revealed, flagged)  # 문장은 원문 그대로다
            if fixed is not None:
                span, trusted_answer = fixed, True
        return _NanoRead(ungrounded, nano.refuses, answer_ok, kb_ok, trusted_answer, span, repaired)

    @staticmethod
    def _result(
        category: Category,
        source: InputSource,
        out: _Output,
        p: _Prepared | None,
        t0: float,
        t1: float,
        calls: int,
        errors: tuple[str, ...],
    ) -> Judgement:
        now = time.monotonic()
        return Judgement(
            response=GuardResponse(category, out.verdict),
            input_source=source,
            output_tier=out.tier,
            nano_grounded=out.nano_grounded,
            signals=p.signals if p else NO_SIGNALS,
            knn_major=p.knn_major if p else None,
            lr_pred=p.lr_pred if p else None,
            calls=calls,
            errors=errors,
            latency_ms={
                "prepare": (t1 - t0) * 1000,
                "llm": (now - t1) * 1000,
                "total": (now - t0) * 1000,
            },
            answer_quote_ok=out.answer_quote_ok,
            kb_quote_ok=out.kb_quote_ok,
            nano_refuses=out.nano_refuses,
            quote_repaired=out.quote_repaired,
        )
