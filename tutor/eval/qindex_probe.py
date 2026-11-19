"""질문 색인 측정(T4.1). 저장소 루트에서 `python -m tutor.eval.qindex_probe <gen|embed|eval>`.

조각마다 nano로 "이 조각으로 답할 수 있는 질문"과 "주제는 같지만 답할 수 없는 질문"을 미리 만들어
임베딩하고, 개발 행의 질의 임베딩과 비교해 (1) 검색 1위·5위 적중과 (2) 자료 밖 신호를 잰다.
서버 동작은 바꾸지 않는다(서버가 읽지 않는 측정 도구). 안내서: `tutor/eval/QINDEX_PROBE.md`.

- gen: KB 조각(서버 색인의 조각 그대로)마다 nano 1회. 생성 입력은 **교재 조각 본문뿐**이다
  (공개 세트·채점 질문은 프롬프트에 넣지 않는다). `--yes` 없이는 예상 호출 수만 낸다.
- embed: questions.jsonl의 질문을 그 순서대로 임베딩한다. 같은 파일이면 다시 부르지 않는다.
- eval: 공개 세트의 개발 행만 쓴다(홀드아웃 묶음 행은 id만 보고 건너뛴다, TU-16). 개발 질의
  임베딩은 dev_query_emb.*에 두고, 있으면 임베딩 API를 부르지 않는다.
화면·보고서·eval_rows.jsonl에는 행 id·섹션 id·수만 남긴다(질문 본문 없음, TU-15).
"""

import argparse
import asyncio
import hashlib
import json
import math
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from guardrail.eval.cv import load_splits
from pydantic import BaseModel

from tutor.eval.case_drafts import DEADLINE_S, RETRIES, TIMEOUT_S, make_llm
from tutor.eval.recall import PUBLIC, missing_embedding_env
from tutor.eval.run_local import EXIT_RATE_LIMITED, RateLimitStop, holdout_ids
from tutor.judge.conversation import tidy
from tutor.retrieval import default_index
from tutor.retrieval.embedding import BATCH, Embed, _unit_rows, is_english, openai_embed, rrf
from tutor.retrieval.index import KbIndex
from tutor.runtime.config import PROXY_MAX_COMPLETION_TOKENS, load_config
from tutor.runtime.llm import Result, TutorLLM, missing_api_env

PROMPT_VERSION = "qindex-gen-v1"
# 생성 지시문(영어, 가드 프롬프트와 같은 기본 언어). 조각 본문은 user 메시지로만 넣는다
SYSTEM_PROMPT = """\
You are given one passage from a course textbook. The user message is the passage and nothing else.
Write questions a learner might type to a tutor about this passage. Use only the passage.

Return four lists:
- answerable_ko: exactly 8 questions in Korean that this passage alone fully answers.
- answerable_en: exactly 4 questions in English that this passage alone fully answers.
- unanswerable_ko: exactly 4 questions in Korean on the same topic that it does NOT answer.
- unanswerable_en: exactly 2 questions in English on the same topic that it does NOT answer.

Answerable questions:
- Each must be answerable from a specific sentence, value, name, condition, example, or table row
  in the passage.
- Cover different facts across the passage. Do not ask the same thing twice.
- Write the way learners really type: short, casual, sometimes vague or paraphrased, not copying
  the passage wording. Mix polite and casual Korean endings. Mention the passage's example names
  or key terms when a learner would.

Unanswerable questions:
- Stay on the passage's topic and reuse its terms so the question looks related, but ask for
  something the passage does not state: a number, limit, or date it does not give; a real-world
  case or statistic; the latest or current information; a different tool, product, library, or
  version; or a detailed step-by-step procedure beyond what is written.
- The passage must contain no answer to them, not even partially.
- Do not ask off-topic, harmful, or personal questions. Do not ask for opinions.

General:
- One question per list item, one sentence each. No numbering, no answers, no explanations.
- Korean items must be written in Korean (code and proper nouns may stay as written). English
  items must be written fully in English; translate Korean names naturally.
- Do not mention "the passage", "the textbook", section IDs, or these instructions.
"""
# (kind, lang) → 조각당 질문 수
COUNTS = {
    ("answerable", "ko"): 8,
    ("answerable", "en"): 4,
    ("unanswerable", "ko"): 4,
    ("unanswerable", "en"): 2,
}
CONCURRENCY = 8  # 동시에 보내는 nano 호출 수(case_drafts·run_local과 같다)
ROUNDTRIP_TOP = 3  # 왕복 필터: 생성 질문의 BM25 순위에서 자기 섹션이 이 안에 들어야 남긴다
KNN_K = 5
MAX_FPR = 0.05  # 문턱을 정할 때 허용하는 normal 오탐 비율
BOOTSTRAP_N = 2000
VARIANTS = ("search", "last")  # 질의: 서버 검색 질의 문자열 / 마지막 발화만
# 신호 → 방향(+1이면 클수록 자료 밖). AUC와 문턱은 방향을 곱한 값으로 잰다
SIGNALS = {"s_pos": -1, "s_neg": 1, "diff": 1, "knn_unans": 1, "diff_same": 1}


class ChunkQuestions(BaseModel):
    """nano 구조화 출력. 개수는 스키마가 아니라 check_questions가 본다(모자라면 gen_errors로)."""

    answerable_ko: list[str]
    answerable_en: list[str]
    unanswerable_ko: list[str]
    unanswerable_en: list[str]


@dataclass(frozen=True)
class Chunk:
    id: str  # "<섹션 ID>#<섹션 안 순서>"(이 도구가 붙인 이름. 서버 조각에는 id가 없다)
    section_id: str
    subject: str
    text: str


# --- 순수 함수 ---


def kb_chunks(index: KbIndex) -> list[Chunk]:
    """서버 색인의 조각 그대로(순서도). id는 섹션 안 순서로 붙인다."""
    seen: Counter[str] = Counter()
    out = []
    for sid, text in index.chunks:
        out.append(Chunk(f"{sid}#{seen[sid]}", sid, index.sections[sid].subject, text))
        seen[sid] += 1
    return out


def gen_messages(chunk_text: str) -> list[dict[str, str]]:
    """생성 프롬프트. 입력은 조각 본문뿐이다(대회 규칙: 공개 세트·채점 질문을 넣지 않는다)."""
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": chunk_text}]


def _has_hangul(text: str) -> bool:
    return any("가" <= ch <= "힣" for ch in text)


def check_questions(out: ChunkQuestions) -> tuple[list[tuple[str, str, str]], str | None]:
    """응답 검증 → ((kind, lang, 질문) 목록, 버린 까닭). 하나라도 틀리면 조각 전체를 버린다.

    까닭: `count:<칸>`(수 부족), `empty:<칸>`(빈 문자열), `lang:<칸>`(언어 뒤바뀜). 한국어 칸은
    한글이 있으면 되고(식별자가 많아도 된다) 영어 칸은 글자 대부분이 라틴 문자면 된다(한국어
    예제 이름을 그대로 둔 영어 질문은 남긴다). 수가 넘치면 앞에서부터 쓴다.
    """
    questions: list[tuple[str, str, str]] = []
    for (kind, lang), count in COUNTS.items():
        name = f"{kind}_{lang}"
        items = [item.strip() for item in getattr(out, name)[:count]]
        if len(items) < count:
            return [], f"count:{name}"
        if not all(items):
            return [], f"empty:{name}"
        if not all(_has_hangul(x) if lang == "ko" else is_english(x) for x in items):
            return [], f"lang:{name}"
        questions += [(kind, lang, item) for item in items]
    return questions, None


def cosine(query: Sequence[float], matrix: np.ndarray) -> list[float]:
    """질의와 행마다의 코사인 유사도."""
    q = _unit_rows(np.asarray(query, dtype=np.float64))
    return (_unit_rows(np.asarray(matrix, dtype=np.float64)) @ q).tolist()


def top_k(sims: Sequence[float], k: int) -> list[int]:
    """유사도 높은 순으로 k개의 위치(같으면 앞 순서)."""
    return sorted(range(len(sims)), key=lambda i: (-sims[i], i))[:k]


def section_ranking(sims: Sequence[float], sections: Sequence[str]) -> list[str]:
    """질문 유사도를 섹션 최고값으로 접은 섹션 순위(같으면 ID 순, BM25와 같은 접기)."""
    best: dict[str, float] = {}
    for sid, sim in zip(sections, sims, strict=True):
        if sid not in best or sim > best[sid]:
            best[sid] = sim
    return sorted(best, key=lambda sid: (-best[sid], sid))


def roundtrip_keep(
    questions: Sequence[str],
    sections: Sequence[str],
    search: Callable[[str], Sequence[str]],
    top: int = ROUNDTRIP_TOP,
) -> list[bool]:
    """왕복 필터: 질문으로 검색(search → 섹션 순위)했을 때 자기 섹션이 top위 안이면 남긴다."""
    return [sid in search(q)[:top] for q, sid in zip(questions, sections, strict=True)]


def auc(positive: Sequence[float], negative: Sequence[float]) -> float | None:
    """양성 점수가 음성 점수보다 클 확률(같으면 0.5). 한쪽이 비면 None."""
    if not len(positive) or not len(negative):
        return None
    p = np.asarray(positive, dtype=np.float64)[:, None]
    n = np.asarray(negative, dtype=np.float64)[None, :]
    return float(((p > n).sum() + 0.5 * (p == n).sum()) / (p.size * n.size))


def bootstrap_auc(
    scores: Sequence[float],
    positive: Sequence[bool],
    clusters: Sequence[str],
    *,
    n: int = BOOTSTRAP_N,
    seed: int = 0,
) -> tuple[float, float] | None:
    """AUC의 부트스트랩 95% 구간. 묶음 단위로 다시 뽑는다(바꿔 쓴 문항이 같은 묶음이라서)."""
    members: dict[str, list[int]] = {}
    for i, cluster in enumerate(clusters):
        members.setdefault(cluster, []).append(i)
    groups = list(members.values())
    rng = np.random.default_rng(seed)
    values = []
    for _ in range(n):
        picked = [i for g in rng.integers(len(groups), size=len(groups)) for i in groups[g]]
        value = auc(
            [scores[i] for i in picked if positive[i]],
            [scores[i] for i in picked if not positive[i]],
        )
        if value is not None:
            values.append(value)
    if not values:
        return None
    lo, hi = np.percentile(values, [2.5, 97.5])
    return float(lo), float(hi)


def fpr_threshold(normal_scores: Sequence[float], max_fpr: float = MAX_FPR) -> float:
    """점수가 이 값을 넘으면(초과) 잡을 때 normal 오탐이 max_fpr 이하가 되는 가장 낮은 문턱."""
    if not len(normal_scores):
        return math.inf
    ordered = sorted(normal_scores, reverse=True)
    return ordered[math.floor(max_fpr * len(ordered))]


def fold_flags(
    scores: Sequence[float],
    normal: Sequence[bool],
    folds: Sequence[int],
    max_fpr: float = MAX_FPR,
) -> list[bool]:
    """행마다: 그 행의 겹을 뺀 나머지 겹의 normal 행으로 문턱을 정해 넘으면(초과) True."""
    threshold = {
        fold: fpr_threshold(
            [s for s, ok, f in zip(scores, normal, folds, strict=True) if ok and f != fold],
            max_fpr,
        )
        for fold in set(folds)
    }
    return [s > threshold[f] for s, f in zip(scores, folds, strict=True)]


def oom_signals(
    query: Sequence[float],
    matrix: np.ndarray,
    kinds: Sequence[str],
    sections: Sequence[str],
    k: int = KNN_K,
) -> dict[str, float]:
    """자료 밖 신호. s_pos·s_neg: 답할 수 있는·없는 질문과의 최근접 유사도, diff: s_neg − s_pos,
    knn_unans: 최근접 k개 중 답할 수 없는 질문 비율, diff_same: 답할 수 있는 최근접 질문의 섹션
    안에서만 본 s_neg − s_pos(그 섹션에 답할 수 없는 질문이 없으면 s_neg를 -1로 둔다).
    """
    sims = cosine(query, matrix)
    pos = [i for i, kind in enumerate(kinds) if kind == "answerable"]
    neg = [i for i, kind in enumerate(kinds) if kind != "answerable"]
    best = max(pos, key=lambda i: (sims[i], -i))
    s_pos = sims[best]
    s_neg = max((sims[i] for i in neg), default=-1.0)
    same = max((sims[i] for i in neg if sections[i] == sections[best]), default=-1.0)
    near = top_k(sims, k)
    return {
        "s_pos": s_pos,
        "s_neg": s_neg,
        "diff": s_neg - s_pos,
        "knn_unans": sum(kinds[i] != "answerable" for i in near) / len(near),
        "diff_same": same - s_pos,
    }


# --- 파일 ---


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _append(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """한 번의 write로 덧붙인다(조각 하나의 줄이 반만 남지 않게)."""
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


def _sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


async def _embed_all(embed: Embed, texts: Sequence[str]) -> np.ndarray:
    """BATCH개씩 임베딩한다(float32). 받은 수가 보낸 수와 다르면 ValueError."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH):
        sent = list(texts[start : start + BATCH])
        got = await embed(sent)
        if len(got) != len(sent):
            raise ValueError("임베딩 수가 보낸 문장 수와 다르다")
        vectors.extend(got)
    matrix = np.asarray(vectors, dtype=np.float32)
    _unit_rows(matrix)  # 영벡터·NaN이면 여기서 멈춘다
    return matrix


def _embed_or_exit(ap: argparse.ArgumentParser, texts: Sequence[str]) -> np.ndarray:
    """운영진 임베딩 API를 부른다. 부르기 전에 환경변수와 예상 호출 수(상한)를 본다."""
    if missing := missing_embedding_env():
        ap.error(f"임베딩 API를 부른다: {'·'.join(missing)}이 필요하다(런박스)")
    calls, cap = math.ceil(len(texts) / BATCH), load_config().eval.max_calls
    sys.stdout.write(f"임베딩 예상 호출 {calls}회({len(texts)}문장, 상한 {cap}회)\n")
    if calls > cap:
        ap.error(f"임베딩 예상 호출 {calls}회가 상한 {cap}회를 넘는다")
    try:
        return asyncio.run(_embed_all(openai_embed(), texts))
    except Exception as exc:  # 메시지는 내지 않는다(TU-15)
        ap.error(f"임베딩을 만들지 못했다: {type(exc).__name__}")


# --- gen ---


async def _gen_all(
    llm: TutorLLM,
    chunks: Sequence[Chunk],
    concurrency: int,
    on_result: Callable[[Chunk, Result], None],
) -> None:
    gate = asyncio.Semaphore(concurrency)
    loop = asyncio.get_running_loop()

    async def one(chunk: Chunk) -> None:
        async with gate:
            result = await llm.complete(
                "qindex_gen",
                gen_messages(chunk.text),
                ChunkQuestions,
                deadline=loop.time() + DEADLINE_S,
                timeout_s=TIMEOUT_S,
                retries=RETRIES,
            )
        on_result(chunk, result)

    await asyncio.gather(*(one(chunk) for chunk in chunks))


def run_gen(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.concurrency < 1:
        ap.error("--concurrency는 1 이상이어야 한다")
    out_dir = Path(args.out)
    questions_path, errors_path = out_dir / "questions.jsonl", out_dir / "gen_errors.jsonl"
    done = {row["chunk_id"] for row in read_jsonl(questions_path)}
    chunks = kb_chunks(default_index())
    todo = [chunk for chunk in chunks if chunk.id not in done]
    if args.limit is not None:
        todo = todo[: args.limit]
    cfg = load_config(overrides={"llm.max_completion_tokens": PROXY_MAX_COMPLETION_TOKENS})
    worst = len(todo) * (1 + RETRIES)
    sys.stdout.write(
        f"조각 {len(chunks)}개 중 끝난 조각 {len(chunks) - len(todo)}개(--limit 포함)\n"
    )
    sys.stdout.write(f"nano 예상 호출 {len(todo)}회(실패하면 한 번씩 다시 보내 최대 {worst}회")
    sys.stdout.write(f", 상한 {cfg.eval.max_calls}회)\n")
    if worst > cfg.eval.max_calls:
        ap.error(f"최대 호출 {worst}회가 상한 {cfg.eval.max_calls}회를 넘는다")
    if not todo:
        return 0
    if not args.yes:
        sys.stdout.write("부르지 않았다. 실제로 부르려면 같은 명령에 --yes를 붙인다\n")
        return 0
    if missing := missing_api_env():
        ap.error(f"운영진 API를 부른다: {'·'.join(missing)}이 필요하다(런박스)")
    out_dir.mkdir(parents=True, exist_ok=True)
    llm = make_llm(cfg)
    llm.max_calls = worst
    stop = RateLimitStop()
    stop.watch_llm(llm)
    failed: Counter[str] = Counter()
    written = 0

    def keep(chunk: Chunk, result: Result) -> None:  # 받는 대로 쓴다(끊겨도 받은 조각은 남는다)
        nonlocal written
        questions, why = [], result.error
        if isinstance(result.parsed, ChunkQuestions):
            questions, why = check_questions(result.parsed)
        base = {"chunk_id": chunk.id, "section_id": chunk.section_id}
        if why is not None:
            failed[why] += 1
            extra = {"error": why, "finish_reason": result.finish_reason}
            _append(errors_path, [{**base, **extra, "prompt_version": PROMPT_VERSION}])
            return
        base = {**base, "subject": chunk.subject}
        tail = {"prompt_version": PROMPT_VERSION}
        _append(
            questions_path,
            [
                {**base, "kind": kind, "lang": lang, "question": q, **tail}
                for kind, lang, q in questions
            ],
        )
        written += 1

    try:
        asyncio.run(_gen_all(llm, todo, args.concurrency, keep))
    except KeyboardInterrupt:
        sys.stdout.write(f"\n멈췄다. 그때까지 받은 조각 {written}개는 {questions_path}에 있다\n")
        return 130
    sys.stdout.write(f"nano 호출 {sum(llm.calls.values())}회 · 쓴 조각 {written}개\n")
    if failed:
        causes = ", ".join(f"{k} {n}" for k, n in sorted(failed.items()))
        sys.stdout.write(f"실패한 조각 {sum(failed.values())}개({causes}): {errors_path}\n")
        sys.stdout.write("같은 명령을 다시 돌리면 실패한 조각만 다시 한다\n")
    if stop.tripped:
        sys.stderr.write(f"{stop.reason()}. 남은 호출은 보내지 않았다\n")
        return EXIT_RATE_LIMITED
    return 0 if written else 1


# --- embed ---


def run_embed(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    folder = Path(args.dir)
    questions_path = folder / "questions.jsonl"
    lines = read_jsonl(questions_path)
    if not lines:
        ap.error(f"질문이 없다: {questions_path}(먼저 gen)")
    sha = _sha(questions_path.read_bytes())
    meta_path = folder / "emb_meta.json"
    if meta_path.exists() and (folder / "emb.npy").exists():
        if json.loads(meta_path.read_text(encoding="utf-8")).get("sha256") == sha:
            sys.stdout.write("emb.npy가 지금 questions.jsonl로 만든 것이라 다시 부르지 않았다\n")
            return 0
    matrix = _embed_or_exit(ap, [line["question"] for line in lines])
    np.save(folder / "emb.npy", matrix)
    keys = ("chunk_id", "section_id", "subject", "kind", "lang")
    meta = {
        "sha256": sha,  # 이 임베딩을 만든 questions.jsonl(eval이 맞춰 본다)
        "count": len(lines),
        "dim": int(matrix.shape[1]),
        "rows": [{key: line[key] for key in keys} for line in lines],  # emb.npy 행 순서
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    sys.stdout.write(
        f"저장: {folder / 'emb.npy'} ({matrix.shape[0]} × {matrix.shape[1]}, float32)\n"
    )
    return 0


@dataclass(frozen=True)
class QuestionIndex:
    lines: tuple[dict[str, Any], ...]  # questions.jsonl 줄(emb 행 순서)
    matrix: np.ndarray  # 단위 벡터(질문 수 × 차원)


def load_question_index(folder: Path) -> QuestionIndex:
    """questions.jsonl과 emb.npy. 임베딩이 지금 questions.jsonl로 만든 것이 아니면 ValueError."""
    questions_path = folder / "questions.jsonl"
    lines = read_jsonl(questions_path)
    meta_path = folder / "emb_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    if not lines or meta.get("sha256") != _sha(questions_path.read_bytes()):
        raise ValueError("emb.npy가 지금 questions.jsonl과 맞지 않는다: embed를 다시 돌린다")
    matrix = np.load(folder / "emb.npy")
    if matrix.shape[0] != len(lines):
        raise ValueError("emb.npy 행 수가 questions.jsonl과 다르다: embed를 다시 돌린다")
    return QuestionIndex(tuple(lines), _unit_rows(matrix.astype(np.float64)))


# --- eval ---


@dataclass(frozen=True)
class DevRow:
    id: str
    fold: int
    cluster: str
    category: str
    action: str
    gold: tuple[str, ...]  # 정답 섹션(없으면 빈 튜플)
    english: bool  # 마지막 발화가 영어 위주
    search: str  # 서버 검색 질의 문자열(retrieve가 만드는 것과 같다)
    last: str  # 마지막 발화만
    rule_a: str  # 규칙 A 질의(튜터 발화까지, toggles.top1_dialog_query)


def dev_rows() -> list[DevRow]:
    """공개 튜터 세트의 개발 행. 홀드아웃 묶음 행은 id만 보고 건너뛴다(본문·정답을 읽지 않는다).

    분할에 없는 행이 있으면 ValueError(조용히 개발 행으로 섞이지 않게).
    """
    split, held, cut = load_splits(), holdout_ids(), default_index().max_query_chars
    rows = []
    for line in PUBLIC.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if raw["id"] in held:
            continue
        if raw["id"] not in split.cluster:
            raise ValueError("공유 분할에 없는 행이 있다: 개발 행인지 알 수 없다")
        conv, labels = tidy(raw["messages"]), raw["labels"]
        query, history = conv.search_query()
        _, dialog = conv.search_query(all_roles=True)
        cluster = split.cluster[raw["id"]]
        rows.append(
            DevRow(
                id=raw["id"],
                fold=split.fold[cluster],
                cluster=cluster,
                category=labels["category"],
                action=labels["action"],
                gold=tuple(labels.get("evidence") or ()),
                english=is_english(query),
                search="\n".join([*history, query])[-cut:],
                last=query,
                rule_a="\n".join([*dialog, query])[-cut:],
            )
        )
    return rows


def dev_query_vectors(
    ap: argparse.ArgumentParser, folder: Path, rows: Sequence[DevRow]
) -> dict[str, np.ndarray]:
    """개발 질의 임베딩(질의 종류 → 행 수 × 차원). dev_query_emb.*에 두고 맞으면 다시 부르지 않는다.

    메타에는 행 id·질의 종류·질의 해시만 둔다(본문 없음). 하나라도 다르면 전부 다시 만든다.
    """
    texts = [getattr(row, variant) or " " for variant in VARIANTS for row in rows]
    keys = [[row.id, variant] for variant in VARIANTS for row in rows]
    keys = [[*key, _sha(text)] for key, text in zip(keys, texts, strict=True)]
    npy, meta_path = folder / "dev_query_emb.npy", folder / "dev_query_emb.json"
    if npy.exists() and meta_path.exists():
        if json.loads(meta_path.read_text(encoding="utf-8")).get("keys") == keys:
            matrix = np.load(npy)
        else:
            matrix = None
    else:
        matrix = None
    if matrix is None:
        matrix = _embed_or_exit(ap, texts)
        np.save(npy, matrix)
        meta_path.write_text(json.dumps({"keys": keys}), encoding="utf-8")
    n = len(rows)
    return {variant: matrix[i * n : (i + 1) * n] for i, variant in enumerate(VARIANTS)}


def _hit_table(rows: Sequence[DevRow], tops: Sequence[tuple[bool, bool]]) -> dict[str, Any]:
    """(1위 적중, 5위 적중) 목록을 전체·언어·과목별로 센다."""
    groups: dict[str, list[tuple[bool, bool]]] = {}
    for row, hit in zip(rows, tops, strict=True):
        for name in ("all", "en" if row.english else "ko", row.gold[0].split("-")[0]):
            groups.setdefault(name, []).append(hit)
    return {
        name: {"n": len(hits), "hit1": sum(h[0] for h in hits), "hit5": sum(h[1] for h in hits)}
        for name, hits in sorted(groups.items())
    }


def evaluate(
    rows: Sequence[DevRow],
    qindex: QuestionIndex,
    vectors: Mapping[str, np.ndarray],
    index: KbIndex,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(보고서, 행별 신호). 보고서·행에는 id·섹션 id·수만 담는다."""
    lines = qindex.lines
    kinds = [line["kind"] for line in lines]
    sections = [line["section_id"] for line in lines]
    pos = [i for i, kind in enumerate(kinds) if kind == "answerable"]

    def bm25(text: str) -> list[str]:
        return [sid for sid, _score in index.search(text)]

    keep = roundtrip_keep([lines[i]["question"] for i in pos], [sections[i] for i in pos], bm25)
    kept = [i for i, ok in zip(pos, keep, strict=True) if ok]
    kept_by_lang = Counter(lines[i]["lang"] for i in kept)

    def hit(gold: Sequence[str], ranked: Sequence[str]) -> tuple[bool, bool]:
        return any(g in ranked[:1] for g in gold), any(g in ranked[:5] for g in gold)

    tops: dict[str, list[tuple[bool, bool]]] = {}
    out_rows = []
    for n, row in enumerate(rows):
        sims = {
            v: (qindex.matrix @ _unit_rows(vectors[v][n].astype(np.float64))).tolist()
            for v in VARIANTS
        }
        signals = {v: oom_signals(vectors[v][n], qindex.matrix, kinds, sections) for v in VARIANTS}
        ranked = {"bm25": bm25(row.search), "rule_a": bm25(row.rule_a)}
        for v, tag in zip(VARIANTS, ("", "_last"), strict=True):
            for idx, name in ((pos, "qindex"), (kept, "qindex_roundtrip")):
                if not idx:
                    ranked[name + tag] = []
                    continue
                ranked[name + tag] = section_ranking(
                    [sims[v][i] for i in idx], [sections[i] for i in idx]
                )
            ranked["rrf" + tag] = rrf(ranked["bm25"], ranked["qindex" + tag])
            ranked["rrf_roundtrip" + tag] = rrf(ranked["bm25"], ranked["qindex_roundtrip" + tag])
        out_rows.append(
            {
                "id": row.id,
                "fold": row.fold,
                "category": row.category,
                "action": row.action,
                "english": row.english,
                "gold": list(row.gold),
                "top1": {name: r[0] if r else None for name, r in ranked.items()},
                "signals": signals,
            }
        )
        if row.gold:
            for name, r in ranked.items():
                tops.setdefault(name, []).append(hit(row.gold, r))
            a, b = hit(row.gold, ranked["bm25"]), hit(row.gold, ranked["rule_a"])
            tops.setdefault("bm25_or_rule_a", []).append((a[0] or b[0], a[1] or b[1]))
    gold_rows = [row for row in rows if row.gold]
    retrieval = {name: _hit_table(gold_rows, hits) for name, hits in tops.items()}

    oom: dict[str, Any] = {}
    in_auc = [i for i, row in enumerate(rows) if row.category in ("out_of_material", "normal")]
    is_oom = [row.category == "out_of_material" for row in rows]
    for v in VARIANTS:
        oom[v] = {}
        for name, sign in SIGNALS.items():
            scores = [sign * out["signals"][v][name] for out in out_rows]
            flags = fold_flags(
                scores, [row.category == "normal" for row in rows], [r.fold for r in rows]
            )
            for out, flag in zip(out_rows, flags, strict=True):
                out.setdefault("flag", {}).setdefault(v, {})[name] = flag
            oom[v][name] = {
                "direction": sign,
                "auc": auc(
                    [scores[i] for i in in_auc if is_oom[i]],
                    [scores[i] for i in in_auc if not is_oom[i]],
                ),
                "ci95": bootstrap_auc(
                    [scores[i] for i in in_auc],
                    [is_oom[i] for i in in_auc],
                    [rows[i].cluster for i in in_auc],
                ),
                "caught": sum(f and o for f, o in zip(flags, is_oom, strict=True)),
                "of": sum(is_oom),
                "flagged_answer": sum(
                    f and row.action == "answer" for f, row in zip(flags, rows, strict=True)
                ),
                "flagged": sum(flags),
            }
    report = {
        "prompt_versions": sorted({line.get("prompt_version", "") for line in lines}),
        "rows": len(rows),
        "rows_with_gold": len(gold_rows),
        "categories": dict(Counter(row.category for row in rows)),
        "questions": {
            "count": dict(Counter(f"{line['kind']}_{line['lang']}" for line in lines)),
            "chunks": len({line["chunk_id"] for line in lines}),
            "roundtrip_kept": {lang: kept_by_lang[lang] for lang in ("ko", "en")},
        },
        "max_fpr": MAX_FPR,
        "retrieval": retrieval,
        "oom": oom,
    }
    return report, out_rows


def _show(report: Mapping[str, Any]) -> None:
    out = sys.stdout
    q = report["questions"]
    out.write(f"개발 행 {report['rows']}개(정답 섹션 있음 {report['rows_with_gold']}개)")
    out.write(f" · 조각 {q['chunks']}개의 질문 {sum(q['count'].values())}개\n")
    kept = q["roundtrip_kept"]
    out.write(f"왕복 필터 뒤 남은 답할 수 있는 질문: 한국어 {kept['ko']} · 영어 {kept['en']}\n")
    out.write("검색 적중(1위/5위/행 수). 전체 | 그룹별 1위\n")
    for name, table in report["retrieval"].items():
        total = table["all"]
        parts = [f"{g} {t['hit1']}/{t['n']}" for g, t in table.items() if g != "all"]
        out.write(f"- {name}: {total['hit1']}/{total['hit5']}/{total['n']} | {', '.join(parts)}\n")
    out.write(
        f"자료 밖 신호(out_of_material 대 normal, 문턱: normal 오탐 {report['max_fpr']:.0%} 이하)\n"
    )
    for variant, signals in report["oom"].items():
        for name, s in signals.items():
            ci = s["ci95"]
            auc_text = "없음" if s["auc"] is None else f"{s['auc']:.3f}"
            ci_text = "" if ci is None else f" [{ci[0]:.3f}, {ci[1]:.3f}]"
            out.write(f"- {variant}.{name}(방향 {s['direction']:+d}): AUC {auc_text}{ci_text}")
            out.write(f" · 잡은 자료 밖 {s['caught']}/{s['of']}")
            out.write(f" · 잘못 잡은 answer {s['flagged_answer']}(잡은 행 {s['flagged']})\n")


def run_eval(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    folder = Path(args.dir)
    try:
        qindex = load_question_index(folder)
    except (OSError, ValueError) as exc:
        ap.error(str(exc))
    rows = dev_rows()
    if not rows:
        ap.error("개발 행이 없다")
    vectors = dev_query_vectors(ap, folder, rows)
    report, out_rows = evaluate(rows, qindex, vectors, default_index())
    rows_path = folder / "eval_rows.jsonl"
    rows_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in out_rows), encoding="utf-8"
    )
    report_path = Path(args.report) if args.report else folder / "eval_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _show(report)
    sys.stdout.write(f"저장: {report_path} · {rows_path}\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="질문 색인 측정(T4.1)")
    sub = ap.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("gen", help="조각마다 nano 1회로 질문을 만든다")
    gen.add_argument("--out", required=True, help="출력 폴더(questions.jsonl·gen_errors.jsonl)")
    gen.add_argument("--limit", type=int, help="이번에 할 조각 수(끝난 조각은 세지 않는다)")
    gen.add_argument("--concurrency", type=int, default=CONCURRENCY, help="동시 호출 수")
    gen.add_argument("--yes", action="store_true", help="예상 호출 수를 확인했고 실제로 부른다")
    gen.set_defaults(run=run_gen)
    embed = sub.add_parser("embed", help="questions.jsonl을 임베딩한다")
    embed.add_argument("--dir", required=True, help="gen의 출력 폴더")
    embed.set_defaults(run=run_embed)
    ev = sub.add_parser("eval", help="개발 행으로 검색 적중과 자료 밖 신호를 잰다")
    ev.add_argument("--dir", required=True, help="gen·embed의 출력 폴더")
    ev.add_argument("--report", help="보고서 JSON 경로(기본: <폴더>/eval_report.json)")
    ev.set_defaults(run=run_eval)
    args = ap.parse_args(argv)
    return args.run(ap, args)


if __name__ == "__main__":
    raise SystemExit(main())
