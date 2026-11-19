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


PER_CHUNK = sum(qp.COUNTS.values())
GOOD = {
    "answerable_ko": [f"이 내용에서 {i}번째로 알 수 있는 것은 뭐예요?" for i in range(8)],
    "answerable_en": [f"What does this part say about item {i}?" for i in range(4)],
    "unanswerable_ko": [f"여기에 없는 {i}번째 수치는 얼마예요?" for i in range(4)],
    "unanswerable_en": [f"What is the latest figure number {i}?" for i in range(2)],
}


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
    chunks = qp.kb_chunks(index)
    assert [(c.section_id, c.text) for c in chunks] == list(index.chunks)
    assert len({c.id for c in chunks}) == len(chunks)
    assert chunks[0].id == f"{chunks[0].section_id}#0"
    assert all(c.subject == index.sections[c.section_id].subject for c in chunks)


def test_prompt_input_is_only_the_chunk_text():
    messages = qp.gen_messages("조각 본문")
    assert messages == [
        {"role": "system", "content": qp.SYSTEM_PROMPT},
        {"role": "user", "content": "조각 본문"},
    ]
    assert qp.SYSTEM_PROMPT.isascii()  # 영어 프롬프트, 공개 세트 문구 없음
    assert qp.PROMPT_VERSION


def test_gen_without_yes_sends_nothing(monkeypatch, tmp_path, capsys):
    sent = _fake_send(monkeypatch, reply_with(GOOD))
    assert qp.main(["gen", "--out", str(tmp_path), "--limit", "2"]) == 0
    assert sent == []
    assert "--yes" in capsys.readouterr().out
    assert not (tmp_path / "questions.jsonl").exists()


def test_gen_calls_once_per_chunk_and_writes_lines(monkeypatch, tmp_path):
    sent = _fake_send(monkeypatch, reply_with(GOOD))
    assert qp.main(["gen", "--out", str(tmp_path), "--limit", "3", "--yes"]) == 0
    chunks = qp.kb_chunks(default_index())[:3]
    assert len(sent) == 3
    # 프롬프트 입력은 조각 본문뿐이다: 메시지는 고정 지시문 + 조각 본문 그대로
    assert sorted(s["messages"][1]["content"] for s in sent) == sorted(c.text for c in chunks)
    assert all(s["messages"][0]["content"] == qp.SYSTEM_PROMPT for s in sent)
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
        assert line["prompt_version"] == qp.PROMPT_VERSION
    first = [x for x in lines if x["chunk_id"] == lines[0]["chunk_id"]]
    counts = {key: 0 for key in qp.COUNTS}
    for line in first:
        counts[(line["kind"], line["lang"])] += 1
    assert counts == qp.COUNTS
    assert not (tmp_path / "gen_errors.jsonl").exists()


def test_gen_resumes_and_skips_done_chunks(monkeypatch, tmp_path):
    sent = _fake_send(monkeypatch, reply_with(GOOD))
    qp.main(["gen", "--out", str(tmp_path), "--limit", "2", "--yes"])
    qp.main(["gen", "--out", str(tmp_path), "--limit", "3", "--yes"])
    chunks = qp.kb_chunks(default_index())[:5]
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
    questions, why = qp.check_questions(qp.ChunkQuestions(**bad))
    assert (questions, why) == ([], reason)


def test_check_questions_keeps_korean_questions_full_of_identifiers():
    ko = ["print(len(my_list))는 for i in range(n) 안에서 뭘 출력해요?"] * 8
    questions, why = qp.check_questions(qp.ChunkQuestions(**{**GOOD, "answerable_ko": ko}))
    assert why is None
    assert len(questions) == PER_CHUNK
    assert questions[0] == ("answerable", "ko", ko[0])
    # 한국어 예제 이름을 그대로 둔 영어 질문도 남긴다
    en = ["How many innings does the 꿈틀 중학교 scoreboard example record?"] * 4
    assert qp.check_questions(qp.ChunkQuestions(**{**GOOD, "answerable_en": en}))[1] is None


def test_gen_sends_bad_replies_to_gen_errors_and_retries_them_next_run(monkeypatch, tmp_path):
    short = {**GOOD, "answerable_ko": GOOD["answerable_ko"][:3]}
    sent = _fake_send(monkeypatch, reply_with(short, GOOD))
    assert (
        qp.main(["gen", "--out", str(tmp_path), "--limit", "2", "--concurrency", "1", "--yes"]) == 0
    )
    assert len(sent) == 2  # 형식이 맞는 JSON이라 다시 보내지 않는다
    chunks = qp.kb_chunks(default_index())[:2]
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
        "prompt_version": qp.PROMPT_VERSION,
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
    meta = json.loads((tmp_path / "emb_meta.json").read_text("utf-8"))
    assert [(r["chunk_id"], r["kind"], r["lang"]) for r in meta["rows"]] == [
        (x["chunk_id"], x["kind"], x["lang"]) for x in lines
    ]
    assert "질문" not in json.dumps(meta, ensure_ascii=False)
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
    sims = qp.cosine([1.0, 0.0], np.array([[2.0, 0.0], [0.0, 3.0], [1.0, 1.0]]))
    assert sims == pytest.approx([1.0, 0.0, 2**-0.5])
    assert qp.top_k([0.1, 0.9, 0.5, 0.9], 2) == [1, 3]  # 같으면 앞 순서
    assert qp.top_k([0.1, 0.9], 5) == [1, 0]


def test_section_ranking_folds_to_the_best_question():
    assert qp.section_ranking([0.2, 0.5, 0.9, 0.5], ["A", "B", "A", "C"]) == ["A", "B", "C"]


def test_rrf_hand_example():
    # B: 1/62 + 1/61, A: 1/61, C: 1/62
    assert rrf(["A", "B"], ["B", "C"]) == ["B", "A", "C"]


def test_roundtrip_keeps_questions_whose_own_section_is_in_the_top_three():
    found = {"q1": ["A", "B"], "q2": ["B", "C", "D", "A"], "q3": [], "q4": ["C", "D", "A"]}
    keep = qp.roundtrip_keep(["q1", "q2", "q3", "q4"], ["A", "A", "A", "A"], found.__getitem__)
    assert keep == [True, False, False, True]


def test_auc_counts_ties_as_half():
    assert qp.auc([3, 2], [1, 2]) == pytest.approx(3.5 / 4)
    assert qp.auc([1], [2]) == 0.0
    assert qp.auc([], [1]) is None


def test_bootstrap_interval_resamples_clusters_and_is_reproducible():
    scores = [0.9, 0.8, 0.7, 0.2, 0.3, 0.1]
    positive = [True, True, True, False, False, False]
    clusters = ["a", "a", "b", "c", "c", "d"]
    lo, hi = qp.bootstrap_auc(scores, positive, clusters, n=200, seed=1)
    assert (lo, hi) == (1.0, 1.0)  # 완전히 갈린다: 어떤 표본에서도 1
    mixed = [0.9, 0.1, 0.7, 0.8, 0.3, 0.2]
    first = qp.bootstrap_auc(mixed, positive, clusters, n=200, seed=1)
    assert first == qp.bootstrap_auc(mixed, positive, clusters, n=200, seed=1)
    assert 0.0 <= first[0] <= qp.auc([0.9, 0.1, 0.7], [0.8, 0.3, 0.2]) <= first[1] <= 1.0


def test_fpr_threshold_allows_five_percent_of_normals_above():
    assert qp.fpr_threshold(list(range(1, 21)), 0.05) == 19  # 20개 중 1개(20)만 넘는다
    assert qp.fpr_threshold(list(range(1, 20)), 0.05) == 19  # 19개면 0개
    assert qp.fpr_threshold([5, 5, 5, 1], 0.25) == 5  # 같은 값은 넘지 않는다(초과만 잡는다)
    assert qp.fpr_threshold([], 0.05) == float("inf")


def test_fold_flags_learn_the_threshold_on_the_other_folds():
    scores = [1, 2, 9, 3, 8, 2.5]
    normal = [True, True, False, True, True, False]
    folds = [0, 0, 0, 1, 1, 1]
    # 폴드 0의 문턱 = 폴드 1 정상의 최댓값 8, 폴드 1의 문턱 = 폴드 0 정상의 최댓값 2
    assert qp.fold_flags(scores, normal, folds, 0.0) == [False, False, True, True, True, True]


def test_oom_signals_hand_example():
    matrix = np.array([[1.0, 0.0], [0.0, 1.0], [0.6, 0.8], [0.0, 1.0]])
    kinds = ["answerable", "answerable", "unanswerable", "unanswerable"]
    sections = ["A", "B", "A", "B"]
    got = qp.oom_signals([1.0, 0.0], matrix, kinds, sections, k=2)
    assert got == pytest.approx(
        {"s_pos": 1.0, "s_neg": 0.6, "diff": -0.4, "knn_unans": 0.5, "diff_same": -0.4}
    )
    # 같은 섹션 안 비교: 답할 수 있는 최근접이 B면 B의 답할 수 없는 질문만 본다
    got = qp.oom_signals([0.0, 1.0], matrix, kinds, sections, k=2)
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
    dev = sorted(rid for rid, c in split.cluster.items() if c not in split.holdout)[:12]
    rows = []
    for i, rid in enumerate(dev):
        oom = i % 3 == 0
        rows.append(
            public_row(
                rid,
                f"개발 질문 본문 {i} 변수는 어떻게 만들어요",
                category="out_of_material" if oom else "normal",
                action="abstain" if oom else "answer",
                evidence=() if oom else ("PY-01-01",),
            )
        )
    # 홀드아웃 행: 읽으면 터지는 모양(messages·labels가 틀렸다)에 표식 문구를 넣는다
    rows += [{"id": rid, "messages": "홀드아웃-표식", "labels": None} for rid in held[:3]]
    public = probe_dir / "public.jsonl"
    public.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    monkeypatch.setattr(qp, "PUBLIC", public)
    report = probe_dir / "report.json"

    assert qp.main(["eval", "--dir", str(probe_dir), "--report", str(report)]) == 0
    assert embed_env.texts  # 개발 질의만 임베딩했다
    assert not [t for t in embed_env.texts if "홀드아웃" in t]
    assert len(embed_env.texts) == 2 * len(dev)
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
    retrieval = data["retrieval"]
    for name in (
        "bm25",
        "rule_a",
        "bm25_or_rule_a",
        "qindex",
        "rrf",
        "qindex_roundtrip",
        "rrf_roundtrip",
    ):
        assert retrieval[name]["all"]["n"] == 8
        assert 0 <= retrieval[name]["all"]["hit1"] <= 8
    assert set(retrieval["bm25"]) >= {"all", "ko", "PY"}
    oom = data["oom"]["last"]["diff"]
    assert oom["of"] == 4
    assert 0.0 <= oom["auc"] <= 1.0
    assert len(oom["ci95"]) == 2
    assert 0 <= oom["caught"] <= 4

    # 캐시가 있으면 임베딩 API를 부르지 않는다
    monkeypatch.setattr(qp, "openai_embed", lambda: pytest.fail("캐시가 있으면 부르지 않는다"))
    assert qp.main(["eval", "--dir", str(probe_dir), "--report", str(report)]) == 0


def test_eval_stops_on_a_row_missing_from_the_split(monkeypatch, probe_dir):
    public = probe_dir / "public.jsonl"
    public.write_text(json.dumps(public_row("pub-not-in-split", "질문")) + "\n", encoding="utf-8")
    monkeypatch.setattr(qp, "PUBLIC", public)
    with pytest.raises(ValueError, match="분할"):
        qp.dev_rows()
