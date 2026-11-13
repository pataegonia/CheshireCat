"""판정기: 결과 타입, 상수 판정기, 조립(Guard) (master-plan §4.1·§4.2, C2-judges S2.8).

서버는 Judgement.response만 직렬화한다. 나머지 필드는 하네스용이고 로그에 남기지 않는다.
Guard.judge는 예외를 밖으로 내지 않는다(GR-09). 실패는 기본값 응답과 오류 코드로 남는다.
"""

import asyncio
import dataclasses
import html
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from guardrail.classifier import LrClassifier
from guardrail.config import GuardConfig, load_prompt
from guardrail.data import LabeledRow, labeled_rows, load_public
from guardrail.decide import Tier, fallback_category, output_verdict, safe_of, trusted
from guardrail.judges.input_judge import build_window, judge_input
from guardrail.judges.output_judge import ask_grounded, output_context, quote_ok
from guardrail.judges.schemas import OutputBaselineOut, OutputOut
from guardrail.kb import Hit, KbIndex
from guardrail.knn import KnnIndex
from guardrail.llm import LLM
from guardrail.preprocess import decode_tags, normalize, reveal_hidden, strip_tags
from guardrail.rules import literal_rule
from guardrail.schema import Category, GuardRequest, GuardResponse, Message, OutputVerdict
from guardrail.signals import Signals, has_hidden_tags, is_low_signal, is_refusal

# nano 호출은 스스로 마감 안에 끝난다(llm.complete). 그 결과를 받을 여유만 둔다
DEADLINE_GRACE_S = 0.2
NO_SIGNALS = Signals(False, False, False)

InputSource = Literal["low_signal", "nano", "third_call", "fallback", "default"]


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


def _consume(task: asyncio.Task) -> None:
    """태스크의 예외를 회수한 것으로 표시한다(GR-12: 예외 메시지가 로그로 새지 않게)."""
    if not task.cancelled():
        task.exception()


@dataclass(frozen=True)
class _Prepared:
    """호출 전 CPU 단계의 결과. 요청 하나 동안만 산다."""

    target: str  # 마지막 학습자 발화 원문(`input.max_chars`로 자름)
    window: list[Message]
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


@dataclass(frozen=True)
class _Output:
    verdict: OutputVerdict | None
    tier: Tier | None = None
    nano_grounded: bool | None = None
    answer_quote_ok: bool | None = None
    kb_quote_ok: bool | None = None
    nano_refuses: bool | None = None


class Guard:
    """입력 범주 판정과 출력 근거 판정을 묶는다. 만든 뒤 색인은 바뀌지 않는다."""

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
        t0 = time.monotonic()
        try:
            return await self._judge(req, t0)
        except Exception as exc:  # noqa: BLE001 - GR-09: 어떤 실패에도 규격 응답. 종류 이름만 남긴다
            return self._result(
                "normal", "default", _Output(self._default_output(req)), None, t0, t0, 0,
                (f"guard:{type(exc).__name__}",),
            )  # fmt: skip

    # --- 내부 ---

    async def _judge(self, req: GuardRequest, t0: float) -> Judgement:
        cfg = self.cfg
        deadline = t0 + cfg.budget_s
        # CPU 단계는 길이 상한으로 자른 뒤 한 번에 스레드로 보낸다(이벤트 루프를 막지 않는다)
        p = await asyncio.to_thread(self._prepare, req)
        t1 = time.monotonic()
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
        if not p.low:
            tasks["input_judge"] = asyncio.create_task(
                judge_input(p.window, p.target, p.examples, self.llm, cfg, deadline=deadline)
            )
        wants_nano = baseline or cfg.toggles.nano_tier3 or cfg.output.call_nano_when_unused
        if p.answer and wants_nano and p.output_error is None:
            asking = ask_grounded(
                p.target, p.revealed, p.hits, self.llm, cfg, deadline=deadline, baseline=baseline
            )
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

        category, source = self._category(p, results.get("input_judge"))
        if p.output_error is not None:
            errors.append(p.output_error)
            out = self._fallback_output(req, category, p.signals)
        else:
            try:
                out = self._output(req, p, category, results.get("output_judge"))
            except Exception as exc:  # noqa: BLE001 - 입력 판정은 살리고 출력만 기본값으로
                errors.append(f"output:{type(exc).__name__}")
                out = self._fallback_output(req, category, p.signals)
        return self._result(category, source, out, p, t0, t1, len(tasks), tuple(errors))

    def _prepare(self, req: GuardRequest) -> _Prepared | Signals:
        """전처리·신호·kNN·LR·KB 검색·규칙 ②. 받은 글은 모두 상한으로 자른 뒤에 쓴다."""
        cfg, t = self.cfg, self.cfg.toggles
        target = next((m.content for m in reversed(req.messages) if m.role == "user"), None)
        if target is None:  # 판정할 발화가 없다. 답변의 숨긴 태그만 본다
            full = req.candidate_answer
            return Signals(False, False, isinstance(full, str) and has_hidden_tags(full))
        target = target[: cfg.input.max_chars]
        window = build_window(req.messages, cfg.input.window_last)
        text = normalize(target)[: cfg.retrieval.max_query_chars]
        low = t.low_signal_direct and is_low_signal(
            strip_tags(target), len(req.messages) > 1, decode_tags(target)[1]
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
            users = [m.content[: cfg.input.max_chars] for m in window if m.role == "user"]
            ranking = self.kb.rank("\n".join(users)[-half:] + "\n" + answer[:half])
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
        """출력을 만들지 못했을 때: grounded는 `failure_default`, safe는 GR-08 그대로."""
        default = self._default_output(req)
        if default is None:
            return _Output(None)
        safe = safe_of(category, self.block, sig, self.cfg.toggles)
        return _Output(OutputVerdict(default.grounded, (), safe))

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

        ungrounded = answer_ok = kb_ok = refuses = quote_span = None
        if isinstance(nano, OutputOut) and p.answer is not None:
            ungrounded, refuses = not nano.grounded, nano.refuses
            answer_ok = quote_ok(nano.answer_quote, p.answer, cfg.output.quote_min_chars)
            kb_ok = quote_ok(nano.kb_quote, "\n".join(h.text for h in p.hits), 1)
            quote = html.unescape(nano.answer_quote)
            if answer_ok and quote in p.answer:  # 구간은 원문에 글자 그대로 있는 것만
                quote_span = quote
        grounded, unsupported, tier = output_verdict(
            category,
            self.block,
            p.rule_spans,
            ungrounded,
            ungrounded is not None and trusted(cfg.output.tier3_trust, answer_ok, kb_ok),
            cfg.toggles,
            cfg.output.failure_default,
            quote_span,
        )
        safe = safe_of(category, self.block, p.signals, cfg.toggles)
        return _Output(
            OutputVerdict(grounded, unsupported, safe),
            tier,
            None if ungrounded is None else not ungrounded,
            answer_ok,
            kb_ok,
            refuses,
        )

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
        )
