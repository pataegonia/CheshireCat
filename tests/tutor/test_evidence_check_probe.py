"""#162 근거 대조 칸 진단: 주 호출 칸 값 기록(측정 전용)과 run_local 집계."""

import json
import re
import types

import pytest

from tests.tutor import tutor_testlib
from tutor.eval import run_local
from tutor.judge.conversation import tidy
from tutor.judge.main_call import (
    EVIDENCE_CHECK_PROBE,
    CheckedMainCallOut,
    MainCallOut,
    MainCallPrompt,
    SentenceOut,
    run_main_call,
)

TABLE = {"normal": ["answer"], "out_of_material": ["abstain"]}
SENT = [SentenceOut(text="자료에 없어요.", kind="non_claim", evidence=[])]


class _Complete:
    def __init__(self, parsed):
        self.parsed = parsed

    async def __call__(self, messages, schema, *, deadline):
        return types.SimpleNamespace(parsed=self.parsed, error=None)


def _conv():
    return tidy([{"role": "user", "content": "질문"}])


CHECKED = CheckedMainCallOut(
    asked_in_evidence=False, category="out_of_material", action="abstain", sentences=SENT
)


# --- 주 호출 기록 ---


@pytest.mark.anyio
async def test_probe_records_value_only_when_measurement_sets_list():
    on = MainCallPrompt.load(TABLE, evidence_check=True)
    assert EVIDENCE_CHECK_PROBE.get() is None  # 서버 기본: 기록하지 않는다
    assert await run_main_call(_Complete(CHECKED), on, _conv(), [], deadline=1) is not None

    seen: list[bool] = []
    token = EVIDENCE_CHECK_PROBE.set(seen)
    try:
        await run_main_call(_Complete(CHECKED), on, _conv(), [], deadline=1)
    finally:
        EVIDENCE_CHECK_PROBE.reset(token)
    assert seen == [False]


@pytest.mark.anyio
async def test_probe_ignores_plain_schema():
    # 칸을 끈 프롬프트에서는 기록할 칸이 없다
    off = MainCallPrompt.load(TABLE)
    plain = MainCallOut(category="normal", action="answer", sentences=SENT)
    seen: list[bool] = []
    token = EVIDENCE_CHECK_PROBE.set(seen)
    try:
        await run_main_call(_Complete(plain), off, _conv(), [], deadline=1)
    finally:
        EVIDENCE_CHECK_PROBE.reset(token)
    assert seen == []


# --- 집계 ---


def _row(row_id, category, action):
    return {"id": row_id, "labels": {"category": category, "action": action}}


def test_summary_counts_by_category_and_simulates_flip():
    rows = [
        _row("a", "out_of_material", "abstain"),
        _row("b", "out_of_material", "abstain"),
        _row("c", "normal", "answer"),
        _row("d", "answer_seeking", "scaffold"),
    ]
    responses = [
        {
            "a": {"action": "answer"},
            "b": {"action": "answer"},
            "c": {"action": "answer"},
            "d": {"action": "answer"},
        },
        {"a": {"action": "abstain"}, "b": None, "c": {"action": "answer"}, "d": {"action": "x"}},
    ]
    checks = [
        {"a": False, "b": True, "c": False, "d": False},
        {"a": False, "b": None, "c": True, "d": False},
    ]
    s = run_local.evidence_check_summary(rows, responses, checks)
    assert s.by_category == {
        "answer_seeking": {"false": 2},
        "normal": {"false": 1, "true": 1},
        "out_of_material": {"false": 2, "none": 1, "true": 1},
    }
    assert s.abstain_answered == {"false": 1, "true": 1}  # 1회 a·b. 2회 a는 abstain, b는 응답 없음
    assert s.flip_gain == 1  # 1회 a
    assert s.flip_loss == {"answer": 1, "scaffold": 1}  # 1회 c·d(2회 d는 answer가 아님)


def test_show_evidence_check_prints_counts_only(capsys):
    s = run_local.EvidenceCheckSummary(
        by_category={"normal": {"true": 3, "false": 1}, "out_of_material": {"false": 2}},
        abstain_answered={"true": 4, "none": 1},
        flip_gain=2,
        flip_loss={"answer": 1},
    )
    run_local.show_evidence_check(s, __import__("sys").stdout)
    assert capsys.readouterr().out.splitlines() == [
        "근거 대조 칸(asked_in_evidence, 모든 실행 합) 정답 범주별 true / false / 없음:",
        "- normal 3 / 1 / 0",
        "- out_of_material 0 / 2 / 0",
        "정답 abstain에 answer를 낸 응답의 칸 true / false / 없음: 4 / 0 / 1",
        "칸 false인 answer를 abstain으로 바꾸면(모의, 바꾸지 않음): 정답 abstain 2개가 맞게 "
        "바뀌고, 정답이 다른 1개가 바뀐다(answer 1)",
    ]


# --- CLI 연결 ---

_TARGET = re.compile(r'판단대상="예">(.*?)</학습자 발화>', re.S)


def _marked(text: str) -> bool:
    return "?" in text


def _reply_by_target(kwargs):
    """판단 대상 발화에 물음표가 있으면 true. 행마다 다른 값이 행에 맞게 붙는지 본다."""
    reply = tutor_testlib._ok_reply(kwargs)
    if kwargs["response_format"]["json_schema"]["name"] != "CheckedMainCallOut":
        return reply
    user = kwargs["messages"][-1]["content"]
    body = json.loads(reply.choices[0].message.content)
    body["asked_in_evidence"] = _marked(_TARGET.search(user).group(1))
    reply.choices[0].message.content = json.dumps(body, ensure_ascii=False)
    return reply


def test_cli_records_check_per_row_under_concurrency(monkeypatch, tmp_path, capsys):
    tutor_testlib._fake_send(monkeypatch, _reply_by_target)
    code = run_local.main(
        [
            "--preset", "tutor", "--runs", "2", "--out", str(tmp_path),
            "--tutor-set", "toggles.evidence_check=true",
        ]
    )  # fmt: skip
    assert code == 0
    data = json.loads(next(tmp_path.glob("tutor-*.json")).read_text(encoding="utf-8"))
    by_run = data["evidence_check"]["by_run"]
    held = run_local.holdout_ids()
    rows = [r for r in run_local.public_tutor_rows() if r["id"] not in held]  # 홀드아웃은 안 잰다
    assert len(by_run) == 2 and all(set(seen) == {r["id"] for r in rows} for seen in by_run)
    recorded = 0
    for row in rows:
        last = [m for m in row["messages"] if m["role"] == "user"][-1]["content"]
        for seen in by_run:
            value = seen[row["id"]]
            if value is not None:  # 주 호출 전에 끝난 요청은 칸이 없다
                recorded += 1
                assert value is _marked(last), row["id"]  # 동시 8개여도 제 행 값이다
    assert recorded > 300
    assert {True, False} <= {v for seen in by_run for v in seen.values()}
    assert data["evidence_check"]["summary"]["by_category"]
    out = capsys.readouterr().out
    assert "근거 대조 칸(asked_in_evidence, 모든 실행 합) 정답 범주별" in out


def test_cli_without_toggle_records_nothing(monkeypatch, tmp_path, capsys):
    tutor_testlib.answer_openai(monkeypatch)
    assert run_local.main(["--preset", "tutor", "--out", str(tmp_path)]) == 0
    data = json.loads(next(tmp_path.glob("tutor-*.json")).read_text(encoding="utf-8"))
    assert "evidence_check" not in data
    assert "근거 대조 칸" not in capsys.readouterr().out
