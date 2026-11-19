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


@dataclass(frozen=True)
class Probe:
    name: str
    group: str  # battery | output | rubric | panel | chair
    system: str
    schema: type[_Out]
    task: str = ""  # user 메시지 맨 앞의 역할 블록
    needs: tuple[str, ...] = ()  # kb | answer | panel
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


def build_messages(p: Probe, c: Ctx, cfg: GuardConfig, panel: dict | None = None) -> list[dict]:
    """[system(질문마다 고정), user(역할 블록 → 자료 → 전문가 보고 → 대화 → 대상 발화)]."""
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

    def load_done(self) -> None:
        if not self.path.is_file():
            return
        for line in self.path.read_text("utf-8").splitlines():
            r = json.loads(line)
            if r.get("error") is None and r.get("answer") is not None:
                self.results[(r["id"], r["probe"], r["rep"])] = r["answer"]

    async def one(self, c: Ctx, rep: int, p: Probe) -> None:
        effort = p.effort or self.args.effort
        llm = self.llms[effort]
        panel = None
        if "panel" in p.needs:
            panel = {s: self.results.get((c.row.id, s, rep)) for s in pp.SPECIALISTS}
        messages = build_messages(p, c, self.cfg, panel)
        async with self.sem:
            if self.stop:
                return
            box: dict = {}
            USAGE.set(box)
            started = time.monotonic()
            # 재시도 0: 지연을 한 번의 호출로 재고, 실패한 칸은 다시 돌릴 때(resume) 채운다
            r = await llm.complete(
                "input_judge",
                messages,
                p.schema,
                deadline=started + llm.cfg.timeout_s.read + 10,
                retries=0,
            )
        answer = r.parsed.model_dump() if r.parsed is not None else None
        self.seq += 1
        rec = {
            "id": c.row.id, "probe": p.name, "rep": rep, "effort": effort, "answer": answer,
            "error": r.error, "finish_reason": r.finish_reason, "latency_ms": round(r.latency_ms, 1),
            "attempts": r.attempts, "completion_tokens": r.completion_tokens,
            "prompt_tokens": box.get("prompt_tokens"), "cached_tokens": box.get("cached_tokens"),
            "seq": self.seq, "t_s": round(started - self.t0, 3),
        }  # fmt: skip
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if r.error is None:
            self.results[(c.row.id, p.name, rep)] = answer
            self.streak = 0
            return
        self.errors[r.error] += 1
        if r.error != "bad_json":  # 형식 오류는 모델 쪽이라 멈출 이유가 아니다
            self.streak += 1
        if r.error == "auth":
            self.stop = "인증 오류(401·403)가 나서 멈췄다"
        elif self.streak >= STREAK:
            self.stop = f"호출 실패가 {STREAK}회 이어져 멈췄다(마지막: {r.error})"

    async def stage(self, jobs: list[tuple[Ctx, int, Probe]]) -> None:
        """첫 호출은 혼자 보내 접두 캐시를 덥힌 뒤 나머지를 동시에 보낸다."""
        if jobs and not self.stop:
            await self.one(*jobs[0])
            await asyncio.gather(*(self.one(*j) for j in jobs[1:]))


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
            if (p.group == "chair") == chair
            and ("answer" not in p.needs or c.row.candidate_answer is not None)
            and (c.row.id, p.name, rep) not in runner.results
        ]

    first, second = jobs(False), jobs(True)
    efforts = sorted({args.effort} | {p.effort for p in probes if p.effort})
    say(
        f"행 {len(ctxs)}, 질문 {len(probes)}개({', '.join(p.name for p in probes)}), 반복 {args.reps}, "
        f"추론 수준 {args.effort}" + (f", 의장 {args.chair_efforts}" if second else "")
    )
    say(
        f"보낼 호출 {len(first) + len(second)}회(이미 끝난 칸 {len(runner.results)}개는 건너뜀) "
        f"+ 시작 점검 약 {5 * len(efforts)}회, 동시 {args.concurrency}"
    )
    if not first and not second:
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
    by_short, info = pa.read_baseline(
        Path(args.baseline).read_text("utf-8").splitlines(), get_args(Category)
    )
    base = {i: by_short[pa.short(i)] for i in gold if pa.short(i) in by_short}
    if not base:
        say(
            f"사용법 오류: 기준선 {info['lines']}줄 중 개발 행과 맞는 것이 없다"
            f"(범주를 읽은 줄 {info['used']}). 필요한 칸은 README의 analyze 절"
        )
        return EXIT_USAGE
    notes = [
        f"기준선 {Path(args.baseline).name}: 줄 {info['lines']}, 쓴 줄 {info['used']}, 버린 줄 "
        f"{info['skipped']}, id {info['ids']}(여러 줄인 id {info['multi']}는 다수결), 개발 행과 맞은 것 "
        f"{len(base)}",
        *(f"실행 {r.label}: 프롬프트 {r.meta.get('prompt_hash', '?')}, 반복 {len(r.reps)}, "
          f"fake={r.meta.get('fake', '?')}" for r in runs),
    ]  # fmt: skip
    text = pa.report(runs, gold, base, block, notes)
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
        "--probes", default="battery,output", help="이름·묶음(battery,output,rubric,panel,all)"
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
    a.add_argument("--baseline", required=True, help="현재 판의 행별 판정 JSONL(README 참고)")
    a.add_argument("--out", help="보고서를 쓸 파일")
    args = ap.parse_args(argv)
    return asyncio.run(run(args)) if args.cmd == "run" else analyze(args)


if __name__ == "__main__":
    sys.exit(main())
