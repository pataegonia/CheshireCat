"""recall@k 측정(TU-03 수용 기준).

창 안의 학습자 발화로 찾은 섹션 상위 k개에 정답 섹션이 있는 비율.
"""

import pytest

from tutor.eval.recall import measure, query_parts


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
    assert report.rows >= 100  # 공개 튜터 200행 중 정답 근거가 있는 행(지금 119)
    assert report.recall[5] >= 0.95  # TU-03 수용 기준(교차 검증으로 보고)
    assert report.recall[1] <= report.recall[3] <= report.recall[5]
    assert set(report.by_fold) <= {"0", "1", "2", "3", "4", "holdout"}
    assert sum(n for _r, n in report.by_fold.values()) == report.rows
    assert {row_id for row_id, _gold, _top, _english in report.missed} <= set(report.row_ids)
    assert len(report.latency_ms) == report.rows


def test_measure_needs_rows():
    with pytest.raises(ValueError):
        measure([])


def test_english_query_is_flagged():
    from tutor.eval.recall import is_english

    assert is_english("Explain what a variable is in Python")
    assert not is_english("파이썬 변수 print 함수")
    assert not is_english("")


def test_main_prints_the_report(capsys):
    from tutor.eval.recall import main

    assert main() == 0
    out = capsys.readouterr().out
    assert "recall@1" in out and "@5" in out and "p95" in out and "놓친 행" in out
