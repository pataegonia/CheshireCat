# ruff: noqa: E501  한글 표 머리·문장이 길다(scripts/는 CI ruff 대상이 아니다)
"""r20: minimal·low·medium 추론으로 좁은 질문·한 번 판정·전문가 묶음을 재는 오프라인 실험 도구.

서버 코드는 건드리지 않는다. `guardrail.llm.LLM`을 그대로 써서 키·주소·모델은 서버와 같이
환경변수에서만 온다(이 스크립트는 `.env`를 읽지 않고 키를 찍지 않는다). 실측은 런박스에서만 한다.

    uv run python -X utf8 scripts/r20/minimal_probe.py run --out runs/r20-min --effort minimal --yes
    uv run python -X utf8 scripts/r20/minimal_probe.py analyze --dir runs/r20-min --baseline dec.jsonl

자세한 것은 같은 폴더 README.md.
"""

import argparse
import asyncio
import hashlib
import html
import json
import random
import sys
import time
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, get_args

sys.path.insert(0, str(Path(__file__).parent))
import probe_analysis as pa  # noqa: E402
import probe_prompts as pp  # noqa: E402

from guardrail.config import GuardConfig, load_config  # noqa: E402
from guardrail.data import PublicRow  # noqa: E402
from guardrail.eval.cv import load_splits  # noqa: E402
from guardrail.eval.load import guard_rows, tutor_rows  # noqa: E402
from guardrail.eval.score import scoring  # noqa: E402
from guardrail.guard import Guard  # noqa: E402
from guardrail.judges.input_judge import _conversation, _data, build_window  # noqa: E402
from guardrail.judges.output_judge import output_context  # noqa: E402
from guardrail.judges.schemas import _Out  # noqa: E402
from guardrail.kb import Hit, KbIndex  # noqa: E402
from guardrail.llm import LLM  # noqa: E402
from guardrail.preprocess import normalize  # noqa: E402
from guardrail.schema import Category  # noqa: E402

STREAK = 20  # 연달아 이만큼 실패하면 멈춘다(eval/run.py의 RATE_LIMIT_STREAK와 같은 값)
EXIT_USAGE, EXIT_STOPPED, EXIT_START = 2, 6, 7
# 추론 수준별 호출 설정. medium은 토큰 2000(운영진 프록시 상한)·read 20초
EFFORTS: dict[str, dict[str, Any]] = {
    "minimal": {},
    "low": {},
    "medium": {"llm.max_completion_tokens": 2000, "llm.timeout_s.read": 20},
}
YN = Literal["yes", "no"]
Decision = Literal["allow", "block"]


class YesNo(_Out):
    answer: YN


class CatOut(_Out):
    category: Category


class FinalOut(_Out):
    reasoning: str  # 판정 앞에 쓰게 한다
    decision: Decision
    category: Category


class StepsOut(_Out):
    is_injection: YN
    is_harmful: YN
    is_deliverable_request: YN
    is_answer_only: YN
    in_subject: YN
    decision: Decision
    category: Category


class SpecOut(_Out):
    """전문가 모두가 같은 스키마를 쓴다(응답 스키마가 달라도 접두 캐시가 깨지지 않게)."""

    reason: str
    verdict: Literal[
        "attack", "study", "none", "harmful", "own_pii", "distress", "abusive", "deliverable",
        "own_work_help", "answer_only", "wants_reasoning", "in_subject", "off_subject", "no_content",
        "not_asked", "covered", "not_covered", "tone_only", "manipulation", "neutral", "strong",
        "weak", "used", "quoted", "absent", "yes", "no",
    ]  # fmt: skip
    confidence: Literal[1, 2, 3, 4, 5]


class TightOut(_Out):
    material_says: str  # 자료가 대신 주는 것(한 구절)
    tight: str  # 구간에서 글자 그대로 옮긴 가장 짧은 조각


@dataclass(frozen=True)
class Probe:
    name: str
    group: str  # battery | output | rubric | panel | chair | span
    system: str
    schema: type[_Out]
    task: str = ""  # user 메시지 맨 앞의 역할 블록
    needs: tuple[str, ...] = ()  # kb | answer | panel | span
    effort: str | None = None  # None = --effort. 의장만 따로 정한다


def _probes() -> dict[str, Probe]:
    out = [
        Probe(n, "battery", pp.BATTERY_SYSTEM + q, YesNo, needs=("kb",) * (n == "in_material"))
        for n, q in pp.BATTERY.items()
    ]
    out.append(Probe("category", "battery", pp.CATEGORY_PROMPT, CatOut))
    out += [
        Probe(n, "output", pp.OUTPUT_SYSTEM + q, YesNo, needs=("kb", "answer"))
        for n, q in pp.OUTPUT.items()
    ]
    out.append(Probe("rubric_full", "rubric", pp.GUIDE, FinalOut, pp.RUBRIC_FULL, ("kb",)))
    out.append(Probe("rubric_steps", "rubric", pp.GUIDE, StepsOut, pp.RUBRIC_STEPS, ("kb",)))
    out += [
        Probe(n, "panel", pp.GUIDE, SpecOut, task, ("kb",) * (n == "spec_covered"))
        for n, task in pp.SPECIALISTS.items()
    ]
    out += [
        Probe(f"chair_{e}", "chair", pp.GUIDE, FinalOut, pp.CHAIR, ("panel",), e)
        for e in ("low", "medium")
    ]
    needs = ("kb", "answer", "span")
    out.append(Probe("span_tighten", "span", pp.SPAN_SYSTEM, TightOut, needs=needs))
    return {p.name: p for p in out}


PROBES = _probes()
PROMPT_HASH = hashlib.sha256(json.dumps(pp.ALL, sort_keys=True).encode()).hexdigest()[:12]


def select_probes(spec: str, chair_efforts: str) -> list[Probe]:
    """`--probes`: 이름이나 묶음(battery·output·rubric·panel·all)을 쉼표로. panel은 의장까지."""
    chairs = {f"chair_{e}" for e in chair_efforts.split(",") if e}
    names: list[str] = []
    for word in (t.strip() for t in spec.split(",") if t.strip()):
        if word in PROBES:
            hit = [word]
        elif word == "all":
            hit = [n for n, p in PROBES.items() if p.group != "chair" or n in chairs]
        elif word == "panel":
            hit = [n for n, p in PROBES.items() if p.group == "panel" or n in chairs]
        else:
            hit = [n for n, p in PROBES.items() if p.group == word]
        if not hit:
            raise ValueError(f"모르는 질문·묶음: {word}")
        names += [n for n in hit if n not in names]
    return [PROBES[n] for n in names]


# --- 메시지 ---


@dataclass(frozen=True)
class Ctx:
    row: PublicRow
    target: str
    kb: tuple[Hit, ...]  # 학습자 발화로 찾은 문맥(가드의 자료 밖 확인과 같은 질의)
    kb_answer: tuple[Hit, ...]  # 발화 + 후보 답변으로 찾은 문맥(가드의 출력 판정과 같은 질의)


def context(row: PublicRow, cfg: GuardConfig, kb: KbIndex) -> Ctx | None:
    users = [
        m.content for m in build_window(row.messages, cfg.input.window_last) if m.role == "user"
    ]
    if not users:
        return None
    q = normalize("\n".join(users))
    hits = output_context(kb.rank(q[-cfg.retrieval.max_query_chars :]), kb, cfg)
    hits_answer: list[Hit] = []
    if row.candidate_answer is not None:
        half = cfg.retrieval.max_query_chars // 2
        query = q[-half:] + "\n" + normalize(row.candidate_answer)[:half]
        hits_answer = output_context(kb.rank(query), kb, cfg)
    return Ctx(row, users[-1], tuple(hits), tuple(hits_answer))


def build_messages(
    p: Probe, c: Ctx, cfg: GuardConfig, panel: dict | None = None, span: str = ""
) -> list[dict]:
    """[system(질문마다 고정), user(역할 블록 → 자료 → 전문가 보고 → 대화 → 대상 발화 → 답변 → 구간)]."""
    parts = [p.task] if p.task else []
    if "kb" in p.needs:
        hits = c.kb_answer if "answer" in p.needs else c.kb
        parts += [
            f'<kb id="{html.escape(h.section_id)}">{html.escape(h.text, quote=False)}</kb>'
            for h in hits
        ] or ["<kb>(no related material found)</kb>"]
    if "panel" in p.needs:
        parts.append(f"<panel>{json.dumps(panel, ensure_ascii=False)}</panel>")
    parts.append(_conversation(c.row.messages, cfg))
    parts.append(f"<target>{_data(c.target, cfg)}</target>")
    if "answer" in p.needs:
        answer = (c.row.candidate_answer or "")[: cfg.input.max_answer_chars]
        parts.append(f"<answer>{html.escape(answer, quote=False)}</answer>")
    if "span" in p.needs:
        parts.append(f"<span>{html.escape(span, quote=False)}</span>")
    return [
        {"role": "system", "content": p.system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


# --- 호출 ---

USAGE: ContextVar[dict | None] = ContextVar("r20_usage", default=None)


def tap_usage(llm: LLM) -> None:
    """응답의 usage(prompt_tokens·cached_tokens)를 호출한 쪽 상자에 담는다. 본문은 보지 않는다."""
    inner = llm._require_send()

    async def send(kwargs: dict[str, Any]) -> Any:
        resp = await inner(kwargs)
        box, usage = USAGE.get(), getattr(resp, "usage", None)
        if box is not None:
            box["prompt_tokens"] = getattr(usage, "prompt_tokens", None)
            details = getattr(usage, "prompt_tokens_details", None)
            box["cached_tokens"] = getattr(details, "cached_tokens", None)
        return resp

    llm._send = send


def _fake_value(node: dict, defs: dict, rnd: random.Random) -> Any:
    if "$ref" in node:
        node = defs[node["$ref"].rsplit("/", 1)[-1]]
    if "enum" in node:
        return rnd.choice(node["enum"])
    if node.get("type") == "boolean":
        return rnd.choice([True, False])
    if "const" in node:
        return node["const"]
    return "fake"


async def fake_send(kwargs: dict[str, Any]) -> Any:
    """`--fake`: 스키마에 맞는 무작위 답(API 0회). 같은 메시지면 같은 답."""
    schema = (kwargs.get("response_format") or {}).get("json_schema", {}).get("schema", {})
    rnd = random.Random(json.dumps(kwargs.get("messages"), ensure_ascii=False))  # noqa: S311
    body = {
        k: _fake_value(v, schema.get("$defs", {}), rnd)
        for k, v in schema.get("properties", {}).items()
    }
    n = sum(len(m.get("content", "")) for m in kwargs.get("messages", [])) // 4
    usage = SimpleNamespace(
        completion_tokens=5,
        prompt_tokens=n,
        prompt_tokens_details=SimpleNamespace(cached_tokens=1024 if n > 1100 else 0),
    )
    choice = SimpleNamespace(
        message=SimpleNamespace(content=json.dumps(body)), finish_reason="stop"
    )
    return SimpleNamespace(choices=[choice], usage=usage)


def effort_config(effort: str, concurrency: int) -> GuardConfig:
    """기본 설정에서 공통 추론 수준만 바꾼다(호출별 덮어쓰기는 비운다)."""
    return load_config(
        overrides={
            "llm.reasoning_effort": effort,
            "llm.per_role": {},
            "llm.gate": concurrency,
            **EFFORTS[effort],
        }
    )


async def start_llm(effort: str, concurrency: int, fake: bool) -> LLM:
    """LLM을 띄우고, 프록시가 이 추론 수준을 받았는지 확인한다. 안 받았으면 ValueError."""
    llm = LLM(effort_config(effort, concurrency).llm, send=fake_send if fake else None)
    await llm.start()
    result = dict(llm.probe_result)
    if result.get("base") is not None:
        raise ValueError(f"시작 점검의 맨 요청이 실패했다: {result['base']}")
    if llm.params.get("reasoning_effort") != effort:
        # LLM.probe()는 거절된 파라미터를 조용히 끈다. 그대로 돌리면 다른 것을 재게 된다
        raise ValueError(
            f"프록시가 reasoning_effort={effort}를 거절했다(시작 점검: {result}). 호출하지 않았다"
        )
    tap_usage(llm)
    return llm


class Runner:
    def __init__(self, llms: dict[str, LLM], cfg: GuardConfig, out: Path, args: argparse.Namespace):
        self.llms, self.cfg, self.args = llms, cfg, args
        self.path = out / "probes.jsonl"
        self.sem = asyncio.Semaphore(args.concurrency)
        self.results: dict[tuple[str, str, int], dict] = {}  # 성공한 답(의장 입력용)
        self.streak, self.stop, self.seq, self.t0 = 0, None, 0, time.monotonic()
        self.errors: Counter[str] = Counter()
        self.guard: Guard | None = None  # span_tighten일 때만 만든다

    def load_done(self) -> None:
        if not self.path.is_file():
            return
        for line in self.path.read_text("utf-8").splitlines():
            r = json.loads(line)
            if r.get("error") is None and r.get("answer") is not None:
                self.results[(r["id"], r["probe"], r["rep"])] = r["answer"]

    async def _call(self, llm: LLM, messages: list[dict], schema: type[_Out]) -> tuple | None:
        """호출 한 번(동시 수 제한 안에서). 멈춘 뒤면 None. (결과, usage 상자, 시작 시각)."""
        async with self.sem:
            if self.stop:
                return None
            box: dict = {}
            USAGE.set(box)
            started = time.monotonic()
            # 재시도 0: 지연을 한 번의 호출로 재고, 실패한 칸은 다시 돌릴 때(resume) 채운다
            r = await llm.complete(
                "input_judge",
                messages,
                schema,
                deadline=started + llm.cfg.timeout_s.read + 10,
                retries=0,
            )
        return r, box, started

    def _write(
        self, row_id: str, probe: str, rep: int, effort: str, answer: dict | None, **more: Any
    ):
        self.seq += 1
        rec = {"id": row_id, "probe": probe, "rep": rep, "effort": effort, "answer": answer, **more,
               "seq": self.seq}  # fmt: skip
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        error = more.get("error")
        if error is None:
            if answer is not None:
                self.results[(row_id, probe, rep)] = answer
            self.streak = 0
            return
        self.errors[error] += 1
        if error != "bad_json":  # 형식 오류는 모델 쪽이라 멈출 이유가 아니다
            self.streak += 1
        if error == "auth":
            self.stop = "인증 오류(401·403)가 나서 멈췄다"
        elif self.streak >= STREAK:
            self.stop = f"호출 실패가 {STREAK}회 이어져 멈췄다(마지막: {error})"

    def _fields(self, r: Any, box: dict, started: float) -> dict:
        return {
            "error": r.error, "finish_reason": r.finish_reason, "latency_ms": round(r.latency_ms, 1),
            "attempts": r.attempts, "completion_tokens": r.completion_tokens,
            "prompt_tokens": box.get("prompt_tokens"), "cached_tokens": box.get("cached_tokens"),
            "t_s": round(started - self.t0, 3),
        }  # fmt: skip

    async def one(self, c: Ctx, rep: int, p: Probe) -> None:
        effort = p.effort or self.args.effort
        panel = None
        if "panel" in p.needs:
            panel = {s: self.results.get((c.row.id, s, rep)) for s in pp.SPECIALISTS}
        got = await self._call(self.llms[effort], build_messages(p, c, self.cfg, panel), p.schema)
        if got is None:
            return
        r, box, started = got
        answer = r.parsed.model_dump() if r.parsed is not None else None
        self._write(c.row.id, p.name, rep, effort, answer, **self._fields(r, box, started))

    async def span_row(self, c: Ctx, rep: int, p: Probe) -> None:
        """출력 판정(low)으로 구간을 얻고, 구간마다 줄이는 호출을 한다. 글은 쓰지 않고 위치만 남긴다.

        `span_base` 줄: 저장소의 `Guard.judge_output`(정답 범주를 넘겨 입력 호출 없이, medium 병렬 끔)
        이 낸 구간의 [시작, 길이](후보 답변 안). 실패해도 기본값을 내는 함수라 오류는 구분되지 않는다.
        `span_tighten` 줄: 줄인 뒤의 [시작, 길이]. 구간의 부분 문자열이 아닌 답은 버리고 원래 구간을 둔다.
        """
        answer = c.row.candidate_answer or ""
        base = self.results.get((c.row.id, "span_base", rep))
        if base is None:
            async with self.sem:
                if self.stop:
                    return
                started = time.monotonic()
                v = await self.guard.judge_output(c.row.messages, answer, c.row.labels["category"])
            found = [[answer.find(s), len(s)] for s in v.unsupported]
            base = {
                "grounded": v.grounded,
                "spans": [s for s in found if s[0] >= 0],
                "unlocated": sum(s[0] < 0 for s in found),  # 답변에서 글자 그대로 못 찾은 구간
            }
            self._write(
                c.row.id, "span_base", rep, "low", base, error=None,
                latency_ms=round((time.monotonic() - started) * 1000, 1),
                t_s=round(started - self.t0, 3),
            )  # fmt: skip
        if not base["spans"]:
            return
        texts = [answer[s : s + n] for s, n in base["spans"]]
        llm = self.llms[self.args.effort]
        got = await asyncio.gather(
            *(self._call(llm, build_messages(p, c, self.cfg, span=t), p.schema) for t in texts)
        )
        if any(g is None for g in got):
            return
        spans, rejected = [], 0
        for (start, n), text, (r, _box, _t) in zip(base["spans"], texts, got, strict=True):
            tight = html.unescape(r.parsed.tight) if r.parsed is not None else ""
            if tight and tight in text:
                spans.append([start + text.find(tight), len(tight)])
            else:  # 실패했거나 구간에 글자 그대로 없는 답: 원래 구간
                spans.append([start, n])
                rejected += r.parsed is not None
        r, box, started = max(got, key=lambda g: g[0].latency_ms)
        error = next((g[0].error for g in got if g[0].error), None)
        out = {"spans": spans, "rejected": rejected, "calls": len(got)}
        fields = {**self._fields(r, box, started), "error": error}
        self._write(c.row.id, p.name, rep, self.args.effort, None if error else out, **fields)

    async def stage(self, jobs: list[tuple[Ctx, int, Probe]], fn: Any = None) -> None:
        """첫 호출은 혼자 보내 접두 캐시를 덥힌 뒤 나머지를 동시에 보낸다."""
        fn = fn or self.one
        if jobs and not self.stop:
            await fn(*jobs[0])
            await asyncio.gather(*(fn(*j) for j in jobs[1:]))


def say(text: str) -> None:
    sys.stdout.write(text + "\n")


def dev_rows(endpoint: str) -> list[PublicRow]:
    """홀드아웃 묶음을 뺀 공개 행(eval/load.py의 고르는 방식 그대로)."""
    return {"guard": guard_rows, "tutor": tutor_rows}.get(
        endpoint, lambda: guard_rows() + tutor_rows()
    )()


async def run(args: argparse.Namespace) -> int:
    if args.concurrency < 1 or args.reps < 1:
        say("사용법 오류: --concurrency와 --reps는 1 이상")
        return EXIT_USAGE
    try:
        probes = select_probes(args.probes, args.chair_efforts)
    except ValueError as exc:
        say(f"사용법 오류: {exc}")
        return EXIT_USAGE
    cfg = effort_config(args.effort, args.concurrency)
    rows = dev_rows(args.rows)
    if args.limit:  # 후보 답변이 있는 행을 앞에 둬 짧은 시험 실행이 모든 질문을 지나게 한다
        rows = sorted(rows, key=lambda r: r.candidate_answer is None)[: args.limit]
    kb = KbIndex.load(cfg.paths.kb, ngram=cfg.kb.ngram, k1=cfg.kb.k1, b=cfg.kb.b)
    ctxs = [c for r in rows if (c := context(r, cfg, kb)) is not None]

    out = Path(args.out)
    meta_path = out / "meta.json"
    meta = {
        "effort": args.effort, "fake": args.fake, "prompt_hash": PROMPT_HASH, "rows": args.rows,
        "reps": args.reps, "chair_efforts": args.chair_efforts,
    }  # fmt: skip
    if meta_path.is_file():
        old = json.loads(meta_path.read_text("utf-8"))
        diff = [k for k in ("effort", "fake", "prompt_hash") if old.get(k) != meta[k]]
        if diff:
            say(f"사용법 오류: {out}의 기존 실행과 {', '.join(diff)}가 다르다. 다른 --out을 써라")
            return EXIT_USAGE
    runner = Runner({}, cfg, out, args)
    runner.load_done()

    def jobs(chair: bool) -> list[tuple[Ctx, int, Probe]]:
        return [
            (c, rep, p)
            for c in ctxs
            for rep in range(args.reps)
            for p in probes
            if p.group != "span"
            and (p.group == "chair") == chair
            and ("answer" not in p.needs or c.row.candidate_answer is not None)
            and (c.row.id, p.name, rep) not in runner.results
        ]

    def no_span(row_id: str, rep: int) -> bool:  # 이미 판정했고 구간이 없던 칸
        base = runner.results.get((row_id, "span_base", rep))
        return base is not None and not base["spans"]

    first, second = jobs(False), jobs(True)
    span = [
        (c, rep, p)
        for p in probes
        if p.group == "span"
        for c in ctxs
        for rep in range(args.reps)
        if c.row.candidate_answer
        and (c.row.id, p.name, rep) not in runner.results
        and not no_span(c.row.id, rep)
    ]
    efforts = sorted(
        {args.effort} | {p.effort for p in probes if p.effort} | ({"low"} if span else set())
    )
    say(
        f"행 {len(ctxs)}, 질문 {len(probes)}개({', '.join(p.name for p in probes)}), 반복 {args.reps}, "
        f"추론 수준 {args.effort}" + (f", 의장 {args.chair_efforts}" if second else "")
    )
    say(
        f"보낼 호출 {len(first) + len(second)}회(이미 끝난 칸 {len(runner.results)}개는 건너뜀) "
        f"+ 시작 점검 약 {5 * len(efforts)}회, 동시 {args.concurrency}"
    )
    if span:
        say(
            f"span_tighten: 후보 답변 칸 {len(span)}개. 칸마다 출력 판정(low) 1회 + 구간이 나온 "
            f"칸만 구간당 1회 → 최소 {len(span)}회, 구간이 칸마다 하나면 최대 {2 * len(span)}회"
        )
    if not first and not second and not span:
        say("보낼 칸이 없다(모두 끝났다)")
        return 0
    if not (args.yes or args.fake):
        say("실제로 보내려면 --yes를 붙인다(호출 없이 끝냄). API 없이 시험하려면 --fake")
        return 0
    out.mkdir(parents=True, exist_ok=True)
    try:
        for e in efforts:
            runner.llms[e] = await start_llm(e, args.concurrency, args.fake)
    except ValueError as exc:  # 환경변수 없음·인증 거절·추론 수준 거절. 문구에 키는 없다
        say(f"시작 실패: {exc}")
        return EXIT_START
    meta["llm"] = {
        e: {"params": llm.params, "structured": llm.structured, "probe": dict(llm.probe_result)}
        for e, llm in runner.llms.items()
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    await runner.stage(first)
    await runner.stage(jobs(True))  # 전문가 답이 생긴 뒤에 의장 입력을 만든다
    if span:  # 지금 판의 출력 판정을 low로만(medium 병렬은 끈다) 불러 구간을 얻는다
        gcfg = load_config(overrides={"output.medium_parallel": False})
        runner.guard = await Guard.build(gcfg, llm=runner.llms["low"])
        await runner.stage(span, runner.span_row)
    say(f"쓴 줄 {runner.seq}, 오류 {dict(runner.errors) or '없음'} → {runner.path}")
    if runner.stop:
        say(f"중단: {runner.stop}. 같은 명령으로 다시 돌리면 남은 칸만 보낸다")
        return EXIT_STOPPED
    return 0


# --- analyze ---


def load_gold() -> tuple[dict[str, dict], frozenset[str]]:
    """개발 행(가드 + 튜터)의 정답 범주·겹·grounded. 홀드아웃은 id로만 걸러 읽지 않는다."""
    split = load_splits()
    gold = {
        r.id: {
            "category": r.labels["category"],
            "fold": split.fold[split.cluster[r.id]],
            "grounded": r.labels.get("grounded"),
            "endpoint": r.endpoint,
            # 구간 채점용(메모리에서만 쓰고 보고서에 싣지 않는다)
            "answer": r.candidate_answer,
            "unsupported": r.labels.get("unsupported") or [],
        }
        for r in dev_rows("all")
    }
    return gold, frozenset(load_config().block_categories)


def analyze(args: argparse.Namespace) -> int:
    runs = []
    for d in args.dir:
        path = Path(d) / "probes.jsonl"
        if not path.is_file():
            say(f"사용법 오류: {path}가 없다")
            return EXIT_USAGE
        records = [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]
        meta_path = Path(d) / "meta.json"
        meta = json.loads(meta_path.read_text("utf-8")) if meta_path.is_file() else {}
        runs.append(pa.Run(records, meta, label=f"{meta.get('effort', '?')}:{Path(d).name}"))
    gold, block = load_gold()
    names = ", ".join(Path(b).name for b in args.baseline)
    lines = [x for b in args.baseline for x in Path(b).read_text("utf-8").splitlines()]
    by_short, info = pa.read_baseline(lines, get_args(Category))
    base = {i: by_short[pa.short(i)] for i in gold if pa.short(i) in by_short}
    if not base:
        say(
            f"사용법 오류: 기준선 {names}의 {info['lines']}줄 중 개발 행과 맞는 것이 없다"
            f"(범주·결정을 읽은 줄 {info['used']}). 파일에서 본 키: {info['keys']}. "
            "필요한 것: id + (pred_category | category | pred | pred_cat) 또는 "
            "(pred_decision | decision | blocked). 자세한 것은 README의 analyze 절"
        )
        return EXIT_USAGE
    notes = [
        f"기준선 {names}: 줄 {info['lines']}, 쓴 줄 {info['used']}, 버린 줄 "
        f"{info['skipped']}, id {info['ids']}(여러 줄인 id {info['multi']}는 다수결), 개발 행과 맞은 것 "
        f"{len(base)}. 본 키: {info['keys']}",
        *(
            [
                f"주의: 범주 없이 결정만 있는 줄 {info['decision_only']}개는 block → "
                "jailbreak_injection, allow → normal로 채웠다(가정). 그만큼 범주 정확도와 "
                "'기준선 jailbreak' 조건(R1·veto)은 믿을 수 없다"
            ]
            if info["decision_only"]
            else []
        ),
        *(f"실행 {r.label}: 프롬프트 {r.meta.get('prompt_hash', '?')}, 반복 {len(r.reps)}, "
          f"fake={r.meta.get('fake', '?')}" for r in runs),
    ]  # fmt: skip
    text = pa.report(runs, gold, base, block, notes)
    text += pa.span_report(runs, gold, scoring.span_f1, scoring.normalize)
    if args.out:
        Path(args.out).write_text(text, "utf-8")
    say(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="질문을 보내 probes.jsonl에 쌓는다")
    r.add_argument("--out", required=True, help="실행 폴더(추론 수준마다 따로)")
    r.add_argument("--effort", choices=list(EFFORTS), default="minimal")
    r.add_argument(
        "--probes", default="battery,output", help="이름·묶음(battery,output,rubric,panel,span,all)"
    )
    r.add_argument("--chair-efforts", default="low", help="panel 의장의 추론 수준(쉼표)")
    r.add_argument("--rows", choices=["guard", "tutor", "all"], default="guard")
    r.add_argument("--reps", type=int, default=3)
    r.add_argument("--concurrency", type=int, default=32)
    r.add_argument("--limit", type=int, default=0, help="앞 N행만(시험 실행)")
    r.add_argument("--yes", action="store_true", help="실제 호출을 보낸다")
    r.add_argument("--fake", action="store_true", help="무작위 가짜 답(API 0회)")
    a = sub.add_parser("analyze", help="보고서(집계와 id 앞 8자만)")
    a.add_argument("--dir", action="append", required=True, help="실행 폴더(여러 번 줄 수 있다)")
    a.add_argument(
        "--baseline",
        action="append",
        required=True,
        help="현재 판의 행별 판정 JSONL(README 참고). 여러 번 주면 행마다 다수결",
    )
    a.add_argument("--out", help="보고서를 쓸 파일")
    args = ap.parse_args(argv)
    return asyncio.run(run(args)) if args.cmd == "run" else analyze(args)


if __name__ == "__main__":
    sys.exit(main())
