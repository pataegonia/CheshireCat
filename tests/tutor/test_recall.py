"""recall@k 측정(TU-03 수용 기준).

창 안의 학습자 발화로 찾은 섹션 상위 k개에 정답 섹션이 있는 비율.
"""

import json

import pytest
from guardrail.eval.cv import load_splits

from tutor.eval.recall import PUBLIC, measure, public_rows, query_parts


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


def test_query_is_the_last_learner_turn_and_history_the_window():
    messages = [user("q1"), assistant("a")] + [user("q2"), assistant("a")]
    messages += [user("q3"), assistant("a"), user("q4"), assistant("a"), user("q5")]
    # 창 = 첫 학습자 발화 + 마지막 메시지 6개 → q1, q3, q4, q5 (q2는 창 밖)
    assert query_parts(messages) == ("q5", ["q1", "q3", "q4"])


def test_query_parts_skips_what_it_cannot_read():
    assert query_parts([assistant("a"), "x", {"role": "user", "content": 3}]) == ("", [])
    assert query_parts([user("only")]) == ("only", [])


def test_public_recall_meets_the_bar():
    report = measure()
    # 공개 튜터 200행 중 정답 근거가 있는 119행에서 홀드아웃 묶음을 뺀 교차 검증 행(지금 97)
    assert report.rows >= 90
    assert report.recall[5] >= 0.95  # TU-03 수용 기준(교차 검증으로 보고)
    assert report.recall[1] <= report.recall[3] <= report.recall[5]
    assert set(report.by_fold) <= {"0", "1", "2", "3", "4"}  # 홀드아웃은 재지 않는다(#84)
    assert sum(n for _r, n in report.by_fold.values()) == report.rows
    assert {row_id for row_id, _gold, _top, _english in report.missed} <= set(report.row_ids)
    assert len(report.latency_ms) == report.rows


def test_measure_needs_rows():
    with pytest.raises(ValueError):
        measure([])


def test_holdout_rows_are_never_measured():
    """TU-16·#84: 홀드아웃 묶음 행은 넘겨도 재지 않는다(통합 하네스에서 최대 3회)."""
    split = load_splits()
    lines = PUBLIC.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    rows = [row for row in rows if row["labels"].get("evidence")]
    held = {row["id"] for row in rows if split.cluster[row["id"]] in split.holdout}
    assert held  # 근거가 있는 홀드아웃 행이 실제로 있다
    assert not held & {row["id"] for row in public_rows()}
    report = measure(rows)
    assert not held & set(report.row_ids)
    assert report.rows == len(rows) - len(held)
    with pytest.raises(ValueError):  # 홀드아웃 행만 넘기면 잴 행이 없다
        measure([row for row in rows if row["id"] in held])


def test_english_query_is_flagged():
    from tutor.eval.recall import is_english

    assert is_english("Explain what a variable is in Python")
    assert not is_english("파이썬 변수 print 함수")
    assert not is_english("")


def test_main_prints_the_report(capsys):
    from tutor.eval.recall import main

    assert main([]) == 0
    out = capsys.readouterr().out
    assert "recall@1" in out and "@5" in out and "p95" in out and "놓친 행" in out


EMBEDDING_ENV = {
    "EMBEDDING_API_KEY": "k",
    "EMBEDDING_MODEL": "m",
    "EMBEDDING_BASE_URL": "http://127.0.0.1:9",
}


@pytest.fixture
def embedding_env(monkeypatch):
    for name, value in EMBEDDING_ENV.items():
        monkeypatch.setenv(name, value)


@pytest.mark.usefixtures("embedding_env")
def test_embedding_run_counts_calls_before_and_after(monkeypatch, capsys):
    from tests.tutor.test_embedding import FakeEmbed, assert_no_holdout_queries
    from tutor.eval import recall

    fake = FakeEmbed()  # 운영진 API 대신(런박스에서는 openai_embed)
    monkeypatch.setattr(recall, "openai_embed", lambda: fake)
    assert recall.main(["--embedding"]) == 0
    out = capsys.readouterr().out
    # KB 조각 244개 = 64개씩 4번 + 정답 근거가 있는 교차 검증 행의 영어 질의 11개(홀드아웃 제외)
    assert "임베딩 예상 호출 15회" in out and "임베딩 호출 15회" in out
    assert len(fake.calls) == 15
    assert_no_holdout_queries(fake)  # 보낸 질의 텍스트에도 홀드아웃 행이 없다


@pytest.mark.usefixtures("embedding_env")
def test_embedding_run_flags_query_failures(monkeypatch, capsys):
    from tests.tutor.test_embedding import FakeEmbed
    from tutor.eval import recall

    class FailQueries(FakeEmbed):  # KB 묶음은 되고 질의(1개씩)만 실패: 결과는 BM25와 같아진다
        async def __call__(self, texts):
            if len(texts) == 1:
                raise RuntimeError("본문")
            return await super().__call__(texts)

    monkeypatch.setattr(recall, "openai_embed", FailQueries)
    assert recall.main(["--embedding"]) == 1  # 임베딩 효과로 읽으면 안 되는 결과
    out = capsys.readouterr().out
    assert "실패 11회" in out and "임베딩 효과가 아니다" in out and "본문" not in out


@pytest.mark.usefixtures("embedding_env")
def test_embedding_run_stops_when_the_kb_embedding_fails(monkeypatch):
    from tests.tutor.test_embedding import FakeEmbed
    from tutor.eval import recall

    monkeypatch.setattr(recall, "openai_embed", lambda: FakeEmbed(fail=RuntimeError("본문")))
    with pytest.raises(SystemExit):
        recall.main(["--embedding"])


@pytest.mark.parametrize(
    "missing, value",
    [(name, None) for name in EMBEDDING_ENV] + [("EMBEDDING_BASE_URL", "  ")],
)
@pytest.mark.usefixtures("embedding_env")
def test_embedding_run_needs_the_embedding_env(monkeypatch, capsys, missing, value):
    """주소가 비면 SDK 기본 주소(공개 OpenAI)로 나가므로 부르기 전에 멈춘다."""
    from tutor.eval import recall

    if value is None:
        monkeypatch.delenv(missing)
    else:
        monkeypatch.setenv(missing, value)
    monkeypatch.setattr(recall, "openai_embed", lambda: pytest.fail("부르기 전에 멈춘다"))
    with pytest.raises(SystemExit):
        recall.main(["--embedding"])
    assert missing in capsys.readouterr().err  # 무엇이 빠졌는지 알린다
