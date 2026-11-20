"""질문 색인 측정 도구(tutor.eval.qindex_probe). 운영진 API는 부르지 않는다(가짜 전송·임베딩)."""

import hashlib
import json
import types

import numpy as np
import pytest
import yaml
from guardrail.eval.cv import load_splits

from tests.tutor.tutor_testlib import FAKE_OPENAI_ENV, _fake_send
from tutor.eval import qindex_probe as qp
from tutor.eval.qindex import pure
from tutor.eval.qindex import report as qreport
from tutor.eval.run_local import holdout_ids, public_tutor_rows
from tutor.retrieval import default_index
from tutor.retrieval.embedding import rrf
from tutor.runtime import config as tutor_config


@pytest.fixture(autouse=True)
def _loadable_config(_env, monkeypatch, tmp_path_factory):
    """conftest 기준 설정(oom_gate off)에서 english_embedding도 끈 사본을 쓴다.

    기준 판(5bda8be)의 저장소 설정은 english_embedding을 켰는데 conftest 기준 설정은 oom_gate를
    꺼서 그대로는 로드되지 않는다. 이 도구는 두 토글을 읽지 않는다(eval.max_calls·llm만 읽는다).
    """
    raw = yaml.safe_load((tutor_config.ROOT / tutor_config.DEFAULT_CONFIG).read_text("utf-8"))
    raw["toggles"].update(english_embedding=False, oom_gate="off")
    path = tmp_path_factory.mktemp("qindex_config") / "tutor.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv(tutor_config.CONFIG_ENV, str(path))


PER_CHUNK = sum(pure.COUNTS.values())
GOOD = {
    "answerable_ko": [f"이 내용에서 {i}번째로 알 수 있는 것은 뭐예요?" for i in range(8)],
    "answerable_en": [f"What does this part say about item {i}?" for i in range(4)],
    "unanswerable_ko": [f"여기에 없는 {i}번째 수치는 얼마예요?" for i in range(4)],
    "unanswerable_en": [f"What is the latest figure number {i}?" for i in range(2)],
}


def good_questions():
    return pure.check_questions(pure.ChunkQuestions(**GOOD))[0]


def reply_with(*bodies):
    """호출 순서대로 bodies를 돌려주는 가짜 nano 응답(모자라면 마지막 것을 되풀이)."""
    count = iter(range(10**9))

    def reply(_kwargs):
        body = bodies[min(next(count), len(bodies) - 1)]
        choice = types.SimpleNamespace(
            message=types.SimpleNamespace(content=json.dumps(body, ensure_ascii=False)),
            finish_reason="stop",
        )
        return types.SimpleNamespace(
            choices=[choice], usage=types.SimpleNamespace(completion_tokens=5)
        )

    return reply


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def fake_vector(text):
    return [b / 255 + 0.01 for b in hashlib.sha256(text.encode()).digest()[:8]]


class FakeEmbed:
    def __init__(self):
        self.batches = []

    async def __call__(self, texts):
        self.batches.append(list(texts))
        return [fake_vector(t) for t in texts]

    @property
    def texts(self):
        return [t for batch in self.batches for t in batch]


@pytest.fixture
def embed_env(monkeypatch):
    fake = FakeEmbed()
    for name in ("EMBEDDING_API_KEY", "EMBEDDING_MODEL"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(qp, "openai_embed", lambda: fake)
    return fake


# --- gen ---


def test_chunks_are_the_server_chunks_with_ids_per_section():
    index = default_index()
    chunks = pure.kb_chunks(index)
    assert [(c.section_id, c.text) for c in chunks] == list(index.chunks)
    assert len({c.id for c in chunks}) == len(chunks)
    assert chunks[0].id == f"{chunks[0].section_id}#0"
    assert all(c.subject == index.sections[c.section_id].subject for c in chunks)


def test_prompt_input_is_only_the_chunk_text():
    messages = pure.gen_messages("조각 본문")
    assert messages == [
        {"role": "system", "content": pure.SYSTEM_PROMPT},
        {"role": "user", "content": "조각 본문"},
    ]
    assert pure.SYSTEM_PROMPT.isascii()  # 영어 프롬프트, 공개 세트 문구 없음
    assert pure.PROMPT_VERSION


def test_gen_without_yes_sends_nothing(monkeypatch, tmp_path, capsys):
    sent = _fake_send(monkeypatch, reply_with(GOOD))
    assert qp.main(["gen", "--out", str(tmp_path), "--limit", "2"]) == 0
    assert sent == []
    assert "--yes" in capsys.readouterr().out
    assert not (tmp_path / "questions.jsonl").exists()


def test_gen_calls_once_per_chunk_and_writes_lines(monkeypatch, tmp_path):
    sent = _fake_send(monkeypatch, reply_with(GOOD))
    assert qp.main(["gen", "--out", str(tmp_path), "--limit", "3", "--yes"]) == 0
    chunks = pure.kb_chunks(default_index())[:3]
    assert len(sent) == 3
    # 프롬프트 입력은 조각 본문뿐이다: 메시지는 고정 지시문 + 조각 본문 그대로
    assert sorted(s["messages"][1]["content"] for s in sent) == sorted(c.text for c in chunks)
    assert all(s["messages"][0]["content"] == pure.SYSTEM_PROMPT for s in sent)
    assert all(len(s["messages"]) == 2 for s in sent)
    prompts = "\n".join(m["content"] for s in sent for m in s["messages"])
    lasts = [
        [m["content"] for m in row["messages"] if m["role"] == "user"][-1]
        for row in public_tutor_rows()
        if row["id"] not in holdout_ids()
    ]
    assert not [text for text in lasts if len(text) >= 10 and text in prompts]
    assert sent[0]["response_format"]["json_schema"]["name"] == "ChunkQuestions"

    lines = read_jsonl(tmp_path / "questions.jsonl")
    assert len(lines) == 3 * PER_CHUNK
    by_id = {c.id: c for c in chunks}
    for line in lines:
        chunk = by_id[line["chunk_id"]]
        assert line["section_id"] == chunk.section_id
        assert line["subject"] == chunk.subject
        assert line["kind"] in ("answerable", "unanswerable")
        assert line["lang"] in ("ko", "en")
        assert line["question"].strip()
        assert line["prompt_version"] == pure.PROMPT_VERSION
    first = [x for x in lines if x["chunk_id"] == lines[0]["chunk_id"]]
    counts = {key: 0 for key in pure.COUNTS}
    for line in first:
        counts[(line["kind"], line["lang"])] += 1
    assert counts == pure.COUNTS
    assert not (tmp_path / "gen_errors.jsonl").exists()


def test_gen_resumes_and_skips_done_chunks(monkeypatch, tmp_path):
    sent = _fake_send(monkeypatch, reply_with(GOOD))
    qp.main(["gen", "--out", str(tmp_path), "--limit", "2", "--yes"])
    qp.main(["gen", "--out", str(tmp_path), "--limit", "3", "--yes"])
    chunks = pure.kb_chunks(default_index())[:5]
    assert len(sent) == 5
    assert sorted(s["messages"][1]["content"] for s in sent) == sorted(c.text for c in chunks)
    lines = read_jsonl(tmp_path / "questions.jsonl")
    assert len(lines) == 5 * PER_CHUNK
    assert {x["chunk_id"] for x in lines} == {c.id for c in chunks}


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ({**GOOD, "answerable_ko": GOOD["answerable_ko"][:7]}, "count:answerable_ko"),
        ({**GOOD, "unanswerable_en": ["What is it?", "  "]}, "empty:unanswerable_en"),
        (
            {
                **GOOD,
                "answerable_ko": GOOD["answerable_en"] * 2,
                "answerable_en": GOOD["answerable_ko"][:4],
            },
            "lang:answerable_ko",
        ),
        ({**GOOD, "answerable_en": GOOD["answerable_ko"][:4]}, "lang:answerable_en"),
    ],
)
def test_check_questions_rejects_bad_replies(bad, reason):
    questions, why = pure.check_questions(pure.ChunkQuestions(**bad))
    assert (questions, why) == ([], reason)


def test_check_questions_keeps_korean_questions_full_of_identifiers():
    ko = ["print(len(my_list))는 for i in range(n) 안에서 뭘 출력해요?"] * 8
    questions, why = pure.check_questions(pure.ChunkQuestions(**{**GOOD, "answerable_ko": ko}))
    assert why is None
    assert len(questions) == PER_CHUNK
    assert questions[0] == ("answerable", "ko", ko[0])
    # 한국어 예제 이름을 그대로 둔 영어 질문도 남긴다
    en = ["How many innings does the 꿈틀 중학교 scoreboard example record?"] * 4
    assert pure.check_questions(pure.ChunkQuestions(**{**GOOD, "answerable_en": en}))[1] is None


def test_gen_sends_bad_replies_to_gen_errors_and_retries_them_next_run(monkeypatch, tmp_path):
    short = {**GOOD, "answerable_ko": GOOD["answerable_ko"][:3]}
    sent = _fake_send(monkeypatch, reply_with(short, GOOD))
    assert (
        qp.main(["gen", "--out", str(tmp_path), "--limit", "2", "--concurrency", "1", "--yes"]) == 0
    )
    assert len(sent) == 2  # 형식이 맞는 JSON이라 다시 보내지 않는다
    chunks = pure.kb_chunks(default_index())[:2]
    errors = read_jsonl(tmp_path / "gen_errors.jsonl")
    assert [(e["chunk_id"], e["error"]) for e in errors] == [(chunks[0].id, "count:answerable_ko")]
    assert "question" not in errors[0]
    lines = read_jsonl(tmp_path / "questions.jsonl")
    assert {x["chunk_id"] for x in lines} == {chunks[1].id}
    qp.main(["gen", "--out", str(tmp_path), "--limit", "1", "--yes"])  # 실패한 조각을 다시 한다
    assert sent[-1]["messages"][1]["content"] == chunks[0].text
    assert len(read_jsonl(tmp_path / "questions.jsonl")) == 2 * PER_CHUNK


def test_gen_records_llm_failures(monkeypatch, tmp_path):
    import openai

    def refuse(_kwargs):
        raise openai.OpenAIError("막은 전송")

    _fake_send(monkeypatch, refuse)
    assert qp.main(["gen", "--out", str(tmp_path), "--limit", "1", "--yes"]) == 1
    assert [e["error"] for e in read_jsonl(tmp_path / "gen_errors.jsonl")] == ["auth"]
    assert not (tmp_path / "questions.jsonl").exists()


def test_gen_stops_after_three_auth_errors_in_a_row(monkeypatch, tmp_path, capsys):
    import openai

    def refuse(_kwargs):
        raise openai.OpenAIError("막은 전송")

    sent = _fake_send(monkeypatch, refuse)
    argv = ["gen", "--out", str(tmp_path), "--limit", "10", "--concurrency", "1", "--yes"]
    assert qp.main(argv) == 1
    assert len(sent) == pure.AUTH_STREAK == 3  # 남은 7조각은 보내지 않는다
    errors = read_jsonl(tmp_path / "gen_errors.jsonl")
    assert [e["error"] for e in errors] == ["auth"] * 3  # 보내지 않은 조각은 오류로 적지 않는다
    assert "인증" in capsys.readouterr().err


def test_an_auth_error_between_successes_does_not_stop_gen(monkeypatch, tmp_path):
    import openai

    count = iter(range(10**9))
    ok = reply_with(GOOD)

    def reply(kwargs):
        if next(count) % 2 == 0:
            raise openai.OpenAIError("막은 전송")
        return ok(kwargs)

    sent = _fake_send(monkeypatch, reply)
    argv = ["gen", "--out", str(tmp_path), "--limit", "8", "--concurrency", "1", "--yes"]
    assert qp.main(argv) == 0
    assert len(sent) == 8


def test_gen_redoes_a_half_written_chunk_and_ignores_a_broken_last_line(monkeypatch, tmp_path):
    chunks = pure.kb_chunks(default_index())[:2]
    whole = [question_line(chunks[0].id, k, lang, q) for k, lang, q in good_questions()]
    half = [question_line(chunks[1].id, k, lang, q) for k, lang, q in good_questions()][:5]
    text = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in [*whole, *half])
    (tmp_path / "questions.jsonl").write_text(text + '{"chunk_id": "PY-0', encoding="utf-8")
    assert len(qp.read_jsonl(tmp_path / "questions.jsonl")) == PER_CHUNK + 5  # 깨진 끝줄은 뺀다

    sent = _fake_send(monkeypatch, reply_with(GOOD))
    assert qp.main(["gen", "--out", str(tmp_path), "--limit", "1", "--yes"]) == 0
    assert [s["messages"][1]["content"] for s in sent] == [chunks[1].text]  # 모자란 조각만 다시
    lines = read_jsonl(tmp_path / "questions.jsonl")  # 모든 줄이 읽힌다(깨진 줄·반쪽 조각 없음)
    assert len(lines) == 2 * PER_CHUNK
    assert {x["chunk_id"]: 0 for x in lines}.keys() == {chunks[0].id, chunks[1].id}
    assert sum(x["chunk_id"] == chunks[1].id for x in lines) == PER_CHUNK


def test_read_jsonl_stops_on_a_broken_line_in_the_middle(tmp_path):
    path = tmp_path / "x.jsonl"
    path.write_text('{"a": 1}\n{"a": \n{"a": 3}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        qp.read_jsonl(path)


def test_gen_needs_the_api_env(monkeypatch, tmp_path):
    for name in FAKE_OPENAI_ENV:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit):
        qp.main(["gen", "--out", str(tmp_path), "--limit", "1", "--yes"])


# --- embed ---


def question_line(chunk_id, kind, lang, question):
    sid = chunk_id.split("#")[0]
    return {
        "chunk_id": chunk_id,
        "section_id": sid,
        "subject": "python",
        "kind": kind,
        "lang": lang,
        "question": question,
        "prompt_version": pure.PROMPT_VERSION,
    }


def write_questions(folder, lines):
    text = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines)
    (folder / "questions.jsonl").write_text(text, encoding="utf-8")


def test_embed_keeps_the_order_of_questions(monkeypatch, tmp_path, embed_env):
    monkeypatch.setattr(qp, "BATCH", 4)
    lines = [question_line(f"PY-01-01#{i % 3}", "answerable", "ko", f"질문 {i}") for i in range(10)]
    write_questions(tmp_path, lines)
    assert qp.main(["embed", "--dir", str(tmp_path)]) == 0
    assert [len(b) for b in embed_env.batches] == [4, 4, 2]
    matrix = np.load(tmp_path / "emb.npy")
    assert matrix.dtype == np.float32
    assert np.allclose(matrix, [fake_vector(x["question"]) for x in lines], atol=1e-6)
    meta = json.loads((tmp_path / "emb.json").read_text("utf-8"))
    assert [(r["chunk_id"], r["kind"], r["lang"]) for r in meta["rows"]] == [
        (x["chunk_id"], x["kind"], x["lang"]) for x in lines
    ]
    assert "질문" not in json.dumps(meta, ensure_ascii=False)
    assert meta["model"] == "x"  # 임베딩 모델 이름(eval이 질의 캐시와 맞춰 본다)
    # 같은 questions면 다시 부르지 않는다
    assert qp.main(["embed", "--dir", str(tmp_path)]) == 0
    assert len(embed_env.batches) == 3


def test_embeddings_made_from_other_questions_are_refused(tmp_path, embed_env):
    lines = [question_line("PY-01-01#0", "answerable", "ko", f"질문 {i}") for i in range(3)]
    write_questions(tmp_path, lines)
    qp.main(["embed", "--dir", str(tmp_path)])
    write_questions(tmp_path, [*lines, question_line("PY-01-01#1", "answerable", "ko", "새 질문")])
    with pytest.raises(ValueError, match="embed"):
        qp.load_question_index(tmp_path)


# --- 순수 함수: 손계산 예 ---


def test_cosine_and_top_k():
    sims = pure.cosine([1.0, 0.0], np.array([[2.0, 0.0], [0.0, 3.0], [1.0, 1.0]]))
    assert sims == pytest.approx([1.0, 0.0, 2**-0.5])
    assert pure.top_k([0.1, 0.9, 0.5, 0.9], 2) == [1, 3]  # 같으면 앞 순서
    assert pure.top_k([0.1, 0.9], 5) == [1, 0]


def test_section_ranking_folds_to_the_best_question():
    assert pure.section_ranking([0.2, 0.5, 0.9, 0.5], ["A", "B", "A", "C"]) == ["A", "B", "C"]


def test_rrf_hand_example():
    # B: 1/62 + 1/61, A: 1/61, C: 1/62
    assert rrf(["A", "B"], ["B", "C"]) == ["B", "A", "C"]


def test_roundtrip_keeps_questions_whose_own_section_is_in_the_top_three():
    found = {"q1": ["A", "B"], "q2": ["B", "C", "D", "A"], "q3": [], "q4": ["C", "D", "A"]}
    keep = pure.roundtrip_keep(["q1", "q2", "q3", "q4"], ["A", "A", "A", "A"], found.__getitem__)
    assert keep == [True, False, False, True]


def test_auc_counts_ties_as_half():
    assert pure.auc([3, 2], [1, 2]) == pytest.approx(3.5 / 4)
    assert pure.auc([1], [2]) == 0.0
    assert pure.auc([], [1]) is None


def test_bootstrap_interval_resamples_clusters_and_is_reproducible():
    scores = [0.9, 0.8, 0.7, 0.2, 0.3, 0.1]
    positive = [True, True, True, False, False, False]
    clusters = ["a", "a", "b", "c", "c", "d"]
    lo, hi = pure.bootstrap_auc(scores, positive, clusters, n=200, seed=1)
    assert (lo, hi) == (1.0, 1.0)  # 완전히 갈린다: 어떤 표본에서도 1
    mixed = [0.9, 0.1, 0.7, 0.8, 0.3, 0.2]
    first = pure.bootstrap_auc(mixed, positive, clusters, n=200, seed=1)
    assert first == pure.bootstrap_auc(mixed, positive, clusters, n=200, seed=1)
    assert 0.0 <= first[0] <= pure.auc([0.9, 0.1, 0.7], [0.8, 0.3, 0.2]) <= first[1] <= 1.0


def test_fpr_threshold_allows_five_percent_of_normals_above():
    assert pure.fpr_threshold(list(range(1, 21)), 0.05) == 19  # 20개 중 1개(20)만 넘는다
    assert pure.fpr_threshold(list(range(1, 20)), 0.05) == 19  # 19개면 0개
    assert pure.fpr_threshold([5, 5, 5, 1], 0.25) == 5  # 같은 값은 넘지 않는다(초과만 잡는다)
    assert pure.fpr_threshold([], 0.05) == float("inf")


def test_fold_flags_learn_the_threshold_on_the_other_folds():
    scores = [1, 2, 9, 3, 8, 2.5]
    normal = [True, True, False, True, True, False]
    folds = [0, 0, 0, 1, 1, 1]
    # 폴드 0의 문턱 = 폴드 1 정상의 최댓값 8, 폴드 1의 문턱 = 폴드 0 정상의 최댓값 2
    assert pure.fold_flags(scores, normal, folds, 0.0) == [False, False, True, True, True, True]


def test_oom_signals_hand_example():
    matrix = np.array([[1.0, 0.0], [0.0, 1.0], [0.6, 0.8], [0.0, 1.0]])
    kinds = ["answerable", "answerable", "unanswerable", "unanswerable"]
    sections = ["A", "B", "A", "B"]
    got = pure.oom_signals([1.0, 0.0], matrix, kinds, sections, k=2)
    assert got == pytest.approx(
        {"s_pos": 1.0, "s_neg": 0.6, "diff": -0.4, "knn_unans": 0.5, "diff_same": -0.4}
    )
    # 같은 섹션 안 비교: 답할 수 있는 최근접이 B면 B의 답할 수 없는 질문만 본다
    got = pure.oom_signals([0.0, 1.0], matrix, kinds, sections, k=2)
    assert got["diff_same"] == pytest.approx(0.0)
    assert got["s_neg"] == pytest.approx(1.0)


# --- eval ---


def public_row(row_id, text, category="normal", action="answer", evidence=("PY-01-01",)):
    return {
        "id": row_id,
        "endpoint": "tutor",
        "messages": [{"role": "user", "content": text}],
        "labels": {"action": action, "category": category, "evidence": list(evidence)},
    }


@pytest.fixture
def probe_dir(tmp_path, embed_env):
    lines = []
    for sid in ("PY-01-01", "PY-01-02", "FIN-01-01"):
        lines += [
            question_line(f"{sid}#0", "answerable", "ko", f"{sid} 답할 수 있는 질문 {i}")
            for i in range(3)
        ]
        lines += [
            question_line(f"{sid}#0", "answerable", "en", f"{sid} answerable {i}") for i in range(2)
        ]
        lines += [
            question_line(f"{sid}#0", "unanswerable", "ko", f"{sid} 답할 수 없는 질문 {i}")
            for i in range(2)
        ]
    write_questions(tmp_path, lines)
    qp.main(["embed", "--dir", str(tmp_path)])
    embed_env.batches.clear()
    return tmp_path


def test_eval_never_reads_holdout_rows_and_caches_query_embeddings(
    monkeypatch, probe_dir, embed_env, capsys
):
    split = load_splits()
    held = sorted(holdout_ids())
    dev_ids = sorted(rid for rid, c in split.cluster.items() if c not in split.holdout)
    dev, blank_id = dev_ids[:12], dev_ids[12]
    rows = []
    for i, rid in enumerate(dev):
        oom = i % 3 == 0
        text = f"개발 질문 본문 {i} 변수는 어떻게 만들어요"
        if i == 1:  # 영어 행: 현재 서버 방식(조각 임베딩 순위)으로 간다
            text = "dev question body: how do I make a variable in Python"
        rows.append(
            public_row(
                rid,
                text,
                category="out_of_material" if oom else "normal",
                action="abstain" if oom else "answer",
                evidence=() if oom else ("PY-01-01",),
            )
        )
    rows.append(public_row(blank_id, "   "))  # 빈 질의: 임베딩하지 않고 건너뛴다
    # 홀드아웃 행: 읽으면 터지는 모양(messages·labels가 틀렸다)에 표식 문구를 넣는다
    rows += [{"id": rid, "messages": "홀드아웃-표식", "labels": None} for rid in held[:3]]
    public = probe_dir / "public.jsonl"
    public.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    monkeypatch.setattr(qp, "PUBLIC", public)
    report = probe_dir / "report.json"

    assert qp.main(["eval", "--dir", str(probe_dir), "--report", str(report)]) == 0
    assert not [t for t in embed_env.texts if "홀드아웃" in t]
    chunk_texts = [text for _sid, text in default_index().chunks]
    queries = [t for t in embed_env.texts if t not in chunk_texts]
    assert len(queries) == 2 * len(dev)  # 개발 질의만(빈 질의·홀드아웃 없음)
    assert all(t.strip() for t in embed_env.texts)
    assert [t for t in embed_env.texts if t in chunk_texts] == chunk_texts  # 조각 임베딩 캐시
    assert (probe_dir / "chunk_emb.npy").exists()
    out_rows = read_jsonl(probe_dir / "eval_rows.jsonl")
    assert [r["id"] for r in out_rows] == dev
    assert not set(held) & {r["id"] for r in out_rows}
    dumped = (probe_dir / "eval_rows.jsonl").read_text("utf-8") + report.read_text("utf-8")
    assert "개발 질문 본문" not in dumped and "홀드아웃" not in dumped
    assert "개발 질문 본문" not in capsys.readouterr().out
    assert set(out_rows[0]["signals"]) == {"search", "last"}
    assert set(out_rows[0]["signals"]["last"]) == {
        "s_pos",
        "s_neg",
        "diff",
        "knn_unans",
        "diff_same",
    }

    data = json.loads(report.read_text("utf-8"))
    assert data["rows"] == len(dev)
    assert data["skipped_blank"] == 1
    assert data["embedding_model"] == "x"
    retrieval = data["retrieval"]
    assert retrieval["server"]["all"]["n"] == 8
    assert retrieval["server"]["en"]["n"] == 1  # 영어 행의 현재 서버 방식 적중
    assert set(retrieval["bm25"]) >= {"all", "ko", "en", "PY"}
    assert data["questions"]["before"] == {
        "answerable_ko": 9,
        "answerable_en": 6,
        "unanswerable_ko": 6,
    }
    assert set(data["questions"]["roundtrip_kept"]) == set(data["questions"]["before"])
    assert data["oom"]["last"]["diff"]["of"] == 4

    # 캐시가 있으면 임베딩 API를 부르지 않는다
    monkeypatch.setattr(qp, "openai_embed", lambda: pytest.fail("캐시가 있으면 부르지 않는다"))
    assert qp.main(["eval", "--dir", str(probe_dir), "--report", str(report)]) == 0
    # 임베딩 모델이 질문 임베딩을 만든 모델과 다르면 멈춘다(캐시가 있어도, 새로 만들 때도)
    meta_path = probe_dir / "dev_query_emb.json"
    meta = json.loads(meta_path.read_text("utf-8"))
    meta_path.write_text(json.dumps({**meta, "model": "other"}), encoding="utf-8")
    with pytest.raises(SystemExit):
        qp.main(["eval", "--dir", str(probe_dir), "--report", str(report)])
    meta_path.unlink()
    monkeypatch.setenv("EMBEDDING_MODEL", "other")
    with pytest.raises(SystemExit):
        qp.main(["eval", "--dir", str(probe_dir), "--report", str(report)])


# --- evaluate: 직교 단위 벡터로 만든 작은 색인의 값 검사 ---

A, B, C = "PY-01-01", "PY-01-02", "FIN-01-01"


def unit(i, dim=6):
    return [1.0 if j == i else 0.0 for j in range(dim)]


def dev_row(row_id, fold, category, gold, text):
    return qreport.DevRow(
        id=row_id,
        fold=fold,
        cluster=row_id,
        category=category,
        action="answer" if category == "normal" else "abstain",
        gold=gold,
        english=False,
        search=text,
        last=text + " 마지막",
        rule_a=text + " 대화",
        history=(),
    )


def small_case():
    """질문 6개(차원 6): A·B·C에 답할 수 있는 질문, A에 영어 하나, A·B에 답할 수 없는 질문."""
    spec = [
        (A, "answerable", "ko", "qa"),  # e0
        (B, "answerable", "ko", "qb"),  # e1
        (A, "unanswerable", "ko", "ua"),  # e2
        (B, "unanswerable", "ko", "ub"),  # e3
        (C, "answerable", "ko", "qc"),  # e4: BM25 왕복에서 떨어진다
        (A, "answerable", "en", "en-a"),  # e5: 영어는 임베딩 왕복으로 남는다
    ]
    lines = [question_line(f"{sid}#0", kind, lang, q) for sid, kind, lang, q in spec]
    qindex = qreport.QuestionIndex(tuple(lines), np.eye(6), "m")
    rows = [
        dev_row("r1", 0, "normal", (A,), "t1"),
        dev_row("r2", 1, "normal", (B,), "t2"),
        dev_row("r3", 0, "out_of_material", (), "t3"),
        dev_row("r4", 1, "out_of_material", (), "t4"),
    ]
    vectors = {
        # r1의 last는 B 질문과 같다(search와 다른 벡터). r3의 last는 답할 수 있는 A 질문과 같다
        "search": np.array([unit(0), unit(1), unit(2), unit(3)]),
        "last": np.array([unit(1), unit(1), unit(0), unit(3)]),
    }
    found = {
        "t1": [B, A],  # BM25 1위는 틀리고 2위가 정답
        "t2": [B],
        "t1 대화": [A],
        "t2 대화": [B],
        "qa": [A],
        "qb": [B],
        "ua": [A],
        "ub": [C, C, C, B],  # 자기 섹션이 4위: 떨어진다
        "qc": [],
    }
    index = types.SimpleNamespace(search=lambda text: [(s, 1.0) for s in found.get(text, [])])

    def embed_rank(vector):  # 영어 질문(e5)만 온다. 조각 임베딩 순위 자리
        assert list(vector) == unit(5)
        return [B, A]

    server = [[B], [B], [], []]
    return rows, qindex, vectors, index, embed_rank, server


def test_evaluate_gives_the_hand_computed_hits():
    rows, qindex, vectors, index, embed_rank, server = small_case()
    report, out = qreport.evaluate(
        rows, qindex, vectors, index, embed_rank=embed_rank, server=server
    )
    hits = {name: (t["all"]["hit1"], t["all"]["hit5"]) for name, t in report["retrieval"].items()}
    assert report["retrieval"]["bm25"]["all"]["n"] == 2
    assert hits["bm25"] == (1, 2)
    assert hits["rule_a"] == (2, 2)
    assert hits["bm25_or_rule_a"] == (2, 2)
    assert hits["server"] == (1, 1)
    # 질의 변형이 뒤바뀌지 않는다: search는 r1·r2 모두 맞고, last는 r1이 B로 가서 틀린다
    assert hits["qindex"] == (2, 2)
    assert hits["qindex_last"] == (1, 2)
    assert out[0]["top1"]["qindex"] == A and out[0]["top1"]["qindex_last"] == B
    # RRF는 BM25(search 질의)와 같은 변형의 질문 색인 순위를 섞는다.
    # r1: BM25 [B, A] + 질문 색인 [A, C, B] → A(1/62 + 1/61) > B(1/61 + 1/63)
    # r1 last: BM25 [B, A] + 질문 색인 [B, C, A] → B
    assert out[0]["top1"]["rrf"] == A and out[0]["top1"]["rrf_last"] == B
    assert hits["rrf"] == (2, 2)
    assert hits["rrf_last"] == (1, 2)
    # 왕복 필터: C의 한국어 질문과 B의 답할 수 없는 질문이 떨어지고 영어 질문은 임베딩으로 남는다
    assert report["questions"]["before"] == {
        "answerable_ko": 3,
        "unanswerable_ko": 2,
        "answerable_en": 1,
    }
    assert report["questions"]["roundtrip_kept"] == {
        "answerable_ko": 2,
        "unanswerable_ko": 1,
        "answerable_en": 1,
    }
    assert out[0]["top1"]["qindex_roundtrip"] == A
    assert C not in (out[1]["top1"]["qindex_roundtrip"], out[1]["top1"]["rrf_roundtrip"])


def test_evaluate_separates_out_of_material_rows_per_variant():
    rows, qindex, vectors, index, embed_rank, server = small_case()
    report, out = qreport.evaluate(
        rows, qindex, vectors, index, embed_rank=embed_rank, server=server
    )
    search, last = report["oom"]["search"], report["oom"]["last"]
    # search: 자료 밖 질의가 답할 수 없는 질문과 같다 → s_neg 1 대 0, diff 1 대 -1
    assert search["s_neg"]["auc"] == 1.0
    assert search["diff"]["auc"] == 1.0
    assert search["s_pos"]["auc"] == 1.0  # 부호를 뒤집어 잰다(자료 밖은 s_pos 0)
    assert out[2]["signals"]["search"] == pytest.approx(
        {"s_pos": 0.0, "s_neg": 1.0, "diff": 1.0, "knn_unans": 0.4, "diff_same": 1.0}
    )
    # 문턱(다른 겹의 normal 최댓값 -1)을 넘는 것은 자료 밖 두 행뿐이다
    assert (search["diff"]["caught"], search["diff"]["of"]) == (2, 2)
    assert (search["diff"]["flagged_answer"], search["diff"]["flagged"]) == (0, 2)
    assert [o["flag"]["search"]["diff"] for o in out] == [False, False, True, True]
    # last: r3의 질의가 답할 수 있는 질문과 같아 s_neg가 normal과 같다 → (0.5 + 0.5 + 1 + 1) / 4
    assert last["s_neg"]["auc"] == 0.75
    assert out[2]["signals"]["last"]["s_pos"] == pytest.approx(1.0)


def test_eval_stops_on_a_row_missing_from_the_split(monkeypatch, probe_dir):
    public = probe_dir / "public.jsonl"
    public.write_text(json.dumps(public_row("pub-not-in-split", "질문")) + "\n", encoding="utf-8")
    monkeypatch.setattr(qp, "PUBLIC", public)
    with pytest.raises(ValueError, match="분할"):
        qp.dev_rows()
