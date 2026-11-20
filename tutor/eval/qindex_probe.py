"""질문 색인 측정(T4.1). 저장소 루트에서 `python -m tutor.eval.qindex_probe <gen|embed|eval>`.

조각마다 nano로 "이 조각으로 답할 수 있는 질문"과 "주제는 같지만 답할 수 없는 질문"을 미리 만들어
임베딩하고, 개발 행의 질의 임베딩과 비교해 (1) 검색 1위·5위 적중과 (2) 자료 밖 신호를 잰다.
서버 동작은 바꾸지 않는다(서버가 읽지 않는 측정 도구). 안내서: `tutor/eval/QINDEX_PROBE.md`.
순수 함수는 `tutor.eval.qindex.pure`, 계산·출력은 `tutor.eval.qindex.report`에 있다.

- gen: KB 조각(서버 색인의 조각 그대로)마다 nano 1회. 생성 입력은 **교재 조각 본문뿐**이다
  (공개 세트·채점 질문은 프롬프트에 넣지 않는다). `--yes` 없이는 예상 호출 수만 낸다.
  인증 오류가 AUTH_STREAK회, 429가 RATE_LIMIT_STREAK회 이어지면 남은 조각을 보내지 않는다.
- embed: questions.jsonl의 질문을 그 순서대로 임베딩한다. 같은 파일이면 다시 부르지 않는다.
- eval: 공개 세트의 개발 행만 쓴다(홀드아웃 묶음 행은 id만 보고 건너뛴다, TU-16). 개발 질의
  임베딩(dev_query_emb.*)과 KB 조각 임베딩(chunk_emb.*)은 캐시가 맞으면 API를 부르지 않는다.
  세 임베딩의 모델 이름이 다르면 멈춘다.
화면·보고서·eval_rows.jsonl에는 행 id·섹션 id·수만 남긴다(질문 본문 없음, TU-15).
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from guardrail.eval.cv import load_splits

from tutor.eval.case_drafts import DEADLINE_S, RETRIES, TIMEOUT_S, make_llm
from tutor.eval.qindex.pure import (
    AUTH_STREAK,
    CONCURRENCY,
    PER_CHUNK,
    PROMPT_VERSION,
    VARIANTS,
    Chunk,
    ChunkQuestions,
    check_questions,
    gen_messages,
    kb_chunks,
)
from tutor.eval.qindex.report import DevRow, QuestionIndex, evaluate, show
from tutor.eval.recall import PUBLIC, CountedEmbed, missing_embedding_env
from tutor.eval.run_local import EXIT_RATE_LIMITED, RateLimitStop, holdout_ids
from tutor.judge.conversation import tidy
from tutor.retrieval import default_index, retrieve
from tutor.retrieval.embedding import (
    BATCH,
    BUILD_TIMEOUT_S,
    Embed,
    EmbeddingSearch,
    _unit_rows,
    is_english,
    openai_embed,
)
from tutor.retrieval.index import KbIndex
from tutor.runtime.config import PROXY_MAX_COMPLETION_TOKENS, load_config
from tutor.runtime.llm import Result, TutorLLM, missing_api_env

# --- 파일 ---


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """JSONL 줄들. 깨진 마지막 줄(쓰다 끊긴 줄)은 무시하고, 가운데 줄이 깨졌으면 ValueError."""
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = []
    for n, line in enumerate(lines):
        try:
            rows.append(json.loads(line))
        except ValueError:
            if n != len(lines) - 1:
                raise
    return rows


def _dump(rows: Sequence[Mapping[str, Any]]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def _append(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """한 번의 write로 덧붙인다(조각 하나의 줄이 반만 남지 않게)."""
    with path.open("a", encoding="utf-8") as f:
        f.write(_dump(rows))


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


def _cached_embedding(
    ap: argparse.ArgumentParser,
    npy: Path,
    texts: Sequence[str],
    key: Any,
    *,
    model: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> tuple[np.ndarray, str]:
    """(임베딩 행렬, 모델 이름). `<npy>`와 메타(`.json`)가 key와 맞으면 API를 부르지 않는다.

    메타에는 key(무엇을 임베딩했는지: 해시·id)와 모델 이름을 둔다. model을 주면 캐시의 모델도 새로
    부를 모델(EMBEDDING_MODEL)도 그것과 같아야 한다(다른 모델의 벡터끼리 비교하지 않게).
    부르기 전에 환경변수와 예상 호출 수(상한 eval.max_calls)를 본다.
    """
    meta_path = npy.with_suffix(".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    if npy.exists() and meta.get("key") == key:
        if model is not None and meta.get("model") != model:
            ap.error(f"{npy.name}의 임베딩 모델이 질문 임베딩과 다르다: 지우고 다시 만든다")
        return np.load(npy), meta.get("model", "")
    if missing := missing_embedding_env():
        ap.error(f"임베딩 API를 부른다: {'·'.join(missing)}이 필요하다(런박스)")
    env_model = os.environ["EMBEDDING_MODEL"].strip()
    if model is not None and env_model != model:
        ap.error("EMBEDDING_MODEL이 질문 임베딩을 만든 모델과 다르다(embed를 다시 돌리거나 맞춘다)")
    calls, cap = math.ceil(len(texts) / BATCH), load_config().eval.max_calls
    sys.stdout.write(f"임베딩 예상 호출 {calls}회({len(texts)}문장, 상한 {cap}회): {npy.name}\n")
    if calls > cap:
        ap.error(f"임베딩 예상 호출 {calls}회가 상한 {cap}회를 넘는다")
    try:
        matrix = asyncio.run(_embed_all(openai_embed(), texts))
    except Exception as exc:  # 메시지는 내지 않는다(TU-15)
        ap.error(f"임베딩을 만들지 못했다: {type(exc).__name__}")
    np.save(npy, matrix)
    body = {"model": env_model, "key": key, "count": len(texts), **(extra or {})}
    meta_path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return matrix, env_model


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
    lines = read_jsonl(questions_path)
    per_chunk = Counter(line["chunk_id"] for line in lines)
    done = {chunk_id for chunk_id, n in per_chunk.items() if n == PER_CHUNK}  # 다 쓰인 조각만
    chunks = kb_chunks(default_index())
    todo = [chunk for chunk in chunks if chunk.id not in done]
    if args.limit is not None:
        todo = todo[: args.limit]
    cfg = load_config(overrides={"llm.max_completion_tokens": PROXY_MAX_COMPLETION_TOKENS})
    worst = len(todo) * (1 + RETRIES)
    sys.stdout.write(
        f"조각 {len(chunks)}개 중 끝난 조각 {len(done)}개 · 이번에 할 조각 {len(todo)}개\n"
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
    clean = _dump([line for line in lines if line["chunk_id"] in done])
    if questions_path.exists() and questions_path.read_text(encoding="utf-8") != clean:
        # 줄 수가 모자란 조각과 깨진 끝줄을 버린다(그 조각은 다시 만든다. 중복이 남지 않게)
        tmp = questions_path.with_suffix(".tmp")
        tmp.write_text(clean, encoding="utf-8")
        tmp.replace(questions_path)
    llm = make_llm(cfg)
    llm.max_calls = worst
    stop = RateLimitStop()
    stop.watch_llm(llm)
    failed: Counter[str] = Counter()
    state = {"written": 0, "auth": 0, "auth_stop": False}

    def keep(chunk: Chunk, result: Result) -> None:  # 받는 대로 쓴다(끊겨도 받은 조각은 남는다)
        if result.error == "budget":  # 멈춘 뒤라 보내지 않은 조각: 오류로 적지 않는다
            return
        state["auth"] = state["auth"] + 1 if result.error == "auth" else 0
        if state["auth"] >= AUTH_STREAK:  # 429 멈춤과 같은 방식: 뒤의 호출은 budget이 된다
            state["auth_stop"], llm.max_calls = True, 0
        questions, why = [], result.error
        if isinstance(result.parsed, ChunkQuestions):
            questions, why = check_questions(result.parsed)
        base = {"chunk_id": chunk.id, "section_id": chunk.section_id}
        tail = {"prompt_version": PROMPT_VERSION}
        if why is not None:
            failed[why] += 1
            extra = {"error": why, "finish_reason": result.finish_reason}
            _append(errors_path, [{**base, **extra, **tail}])
            return
        base = {**base, "subject": chunk.subject}
        rows = [
            {**base, "kind": k, "lang": lang, "question": q, **tail} for k, lang, q in questions
        ]
        _append(questions_path, rows)
        state["written"] += 1

    try:
        asyncio.run(_gen_all(llm, todo, args.concurrency, keep))
    except KeyboardInterrupt:
        sys.stdout.write(f"\n멈췄다. 그때까지 받은 조각 {state['written']}개는 남아 있다\n")
        return 130
    sys.stdout.write(f"nano 호출 {sum(llm.calls.values())}회 · 쓴 조각 {state['written']}개\n")
    if failed:
        causes = ", ".join(f"{k} {n}" for k, n in sorted(failed.items()))
        sys.stdout.write(f"실패한 조각 {sum(failed.values())}개({causes}): {errors_path}\n")
        sys.stdout.write("같은 명령을 다시 돌리면 실패한 조각만 다시 한다\n")
    if state["auth_stop"]:
        sys.stderr.write(f"인증 오류가 {AUTH_STREAK}회 이어져 멈췄다. 남은 호출은 보내지 않았다")
        sys.stderr.write("(OPENAI_API_KEY·OPENAI_BASE_URL 확인)\n")
        return 1
    if stop.tripped:
        sys.stderr.write(f"{stop.reason()}. 남은 호출은 보내지 않았다\n")
        return EXIT_RATE_LIMITED
    return 0 if state["written"] else 1


# --- embed ---


def run_embed(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    folder = Path(args.dir)
    questions_path = folder / "questions.jsonl"
    lines = read_jsonl(questions_path)
    if not lines:
        ap.error(f"질문이 없다: {questions_path}(먼저 gen)")
    keys = ("chunk_id", "section_id", "subject", "kind", "lang")
    rows = [{key: line[key] for key in keys} for line in lines]  # emb.npy 행 순서
    matrix, model = _cached_embedding(
        ap,
        folder / "emb.npy",
        [line["question"] for line in lines],
        _sha(questions_path.read_bytes()),  # 이 임베딩을 만든 questions.jsonl(eval이 맞춰 본다)
        extra={"rows": rows},
    )
    sys.stdout.write(f"{folder / 'emb.npy'}: {matrix.shape[0]} × {matrix.shape[1]}, 모델 {model}\n")
    return 0


def load_question_index(folder: Path) -> QuestionIndex:
    """questions.jsonl과 emb.npy. 임베딩이 지금 questions.jsonl로 만든 것이 아니면 ValueError."""
    questions_path = folder / "questions.jsonl"
    lines = read_jsonl(questions_path)
    meta_path = folder / "emb.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    if not lines or meta.get("key") != _sha(questions_path.read_bytes()):
        raise ValueError("emb.npy가 지금 questions.jsonl과 맞지 않는다: embed를 다시 돌린다")
    matrix = np.load(folder / "emb.npy")
    if matrix.shape[0] != len(lines):
        raise ValueError("emb.npy 행 수가 questions.jsonl과 다르다: embed를 다시 돌린다")
    return QuestionIndex(tuple(lines), _unit_rows(matrix.astype(np.float64)), meta.get("model", ""))


# --- eval ---


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
                history=tuple(history),
            )
        )
    return rows


def dev_query_vectors(
    ap: argparse.ArgumentParser, folder: Path, rows: Sequence[DevRow], model: str
) -> dict[str, np.ndarray]:
    """개발 질의 임베딩(질의 종류 → 행 수 × 차원). dev_query_emb.*에 두고 맞으면 다시 부르지 않는다.

    메타에는 행 id·질의 종류·질의 해시만 둔다(본문 없음). 하나라도 다르면 전부 다시 만든다.
    """
    texts = [getattr(row, variant) for variant in VARIANTS for row in rows]
    ids = [[row.id, variant] for variant in VARIANTS for row in rows]
    key = [[*pair, _sha(text)] for pair, text in zip(ids, texts, strict=True)]
    matrix, _ = _cached_embedding(ap, folder / "dev_query_emb.npy", texts, key, model=model)
    n = len(rows)
    return {variant: matrix[i * n : (i + 1) * n] for i, variant in enumerate(VARIANTS)}


def chunk_search(
    ap: argparse.ArgumentParser, folder: Path, index: KbIndex, model: str, embed: Embed
) -> EmbeddingSearch:
    """KB 조각 임베딩(chunk_emb.*에 캐시)으로 만든 서버의 임베딩 검색. embed는 질의 임베딩 함수."""
    texts = [text for _sid, text in index.chunks]
    key = _sha("\x00".join(texts))
    matrix, _ = _cached_embedding(ap, folder / "chunk_emb.npy", texts, key, model=model)
    chunk_ids = tuple(sid for sid, _text in index.chunks)
    return EmbeddingSearch(embed, chunk_ids, _unit_rows(matrix.astype(np.float64)), BUILD_TIMEOUT_S)


async def _server_tops(
    rows: Sequence[DevRow], search: EmbeddingSearch, index: KbIndex
) -> list[list[str]]:
    """현재 서버 방식(`retrieve`, toggles.english_embedding 켬): 영어 질의는 조각 임베딩 순위만."""
    tops = []
    for row in rows:
        found = await retrieve(
            row.last, row.history, k=5, embedding=search, english_only_embedding=True, index=index
        )
        tops.append([section.id for section in found])
    return tops


def run_eval(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    folder = Path(args.dir)
    try:
        qindex = load_question_index(folder)
    except (OSError, ValueError) as exc:
        ap.error(str(exc))
    every = dev_rows()
    rows = [row for row in every if row.search.strip() and row.last.strip()]  # 빈 질의는 뺀다
    if not rows:
        ap.error("개발 행이 없다")
    index = default_index()
    vectors = dev_query_vectors(ap, folder, rows, qindex.model)
    # retrieve가 부르는 질의 임베딩은 캐시에서 준다(없는 질의면 실패로 세고, 아래에서 멈춘다)
    cached = {row.search: vectors["search"][n].tolist() for n, row in enumerate(rows)}

    async def lookup(texts: Sequence[str]) -> list[list[float]]:
        return [cached[text] for text in texts]

    counted = CountedEmbed(lookup)
    search = chunk_search(ap, folder, index, qindex.model, counted)
    server = asyncio.run(_server_tops(rows, search, index))
    if counted.failures:  # retrieve가 조용히 BM25로 떨어졌다: 서버 방식 줄을 믿을 수 없다
        ap.error(f"서버 방식 재현에서 질의 임베딩 {counted.failures}개를 쓰지 못했다")
    report, out_rows = evaluate(
        rows, qindex, vectors, index, embed_rank=search.ranking, server=server
    )
    report["skipped_blank"] = len(every) - len(rows)
    rows_path = folder / "eval_rows.jsonl"
    rows_path.write_text(_dump(out_rows), encoding="utf-8")
    report_path = Path(args.report) if args.report else folder / "eval_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    show(report, sys.stdout)
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
