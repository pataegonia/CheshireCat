"""걸러 보기 대조 호출 측정(harness/oom_probe, 이슈 #162). 운영진 API 없이 가짜 전송으로 본다.

입력 문장은 지어낸 것이다(공개 세트 · KB 문구를 옮기지 않는다).
"""

import asyncio
import contextlib
import io
import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from harness import oom_probe as op
from tutor.runtime import llm as tutor_llm
from tutor.runtime.llm import TutorLLM

ASKED = "가짜대조질문표식"
QUOTE = "가짜대조인용표식"
BUNDLE = '<섹션 id="X-1">\n고양이  식당은\n오전 열 시에   문을 엽니다.\n</섹션>'


def reply(found=True, quote=QUOTE, *, finish="stop", content=None, reasoning=7):
    body = json.dumps({"asked": ASKED, "quote": quote, "found": found}, ensure_ascii=False)
    message = SimpleNamespace(content=body if content is None else content, refusal=None)
    usage = SimpleNamespace(
        completion_tokens=42, completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning)
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish, message=message)], usage=usage
    )


class FakeSend:
    """행 id(user 메시지에 든 표식)별 응답을 내는 가짜 전송. 보낸 요청을 남긴다."""

    def __init__(self, default=None, by_mark=None):
        self.default = default or (lambda: reply())
        self.by_mark = by_mark or {}
        self.sent: list[dict] = []

    async def __call__(self, kwargs):
        self.sent.append(kwargs)
        user = kwargs["messages"][-1]["content"]
        for mark, make in self.by_mark.items():
            if mark in user:
                return make()
        return self.default()


def make_llm(send) -> TutorLLM:
    base = SimpleNamespace(
        params={"reasoning_effort": "low", "verbosity": None, "max_completion_tokens": 1600},
        structured="json_schema",
    )
    overrides = {"reasoning_effort": "medium", "max_completion_tokens": 2000}
    return TutorLLM(
        op.capture_usage(send),
        contextlib.nullcontext,
        model="fake-model",
        base=base,
        overrides=overrides,
    )


def item(row_id="r1", gold="abstain", bundle=BUNDLE) -> op.Prepared:
    turns = f'<대화>\n<학습자 발화 판단대상="예">{row_id}</학습자 발화>\n</대화>'
    user = op.build_user(bundle, turns)
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": user}]
    return op.Prepared(row_id, "out_of_material", gold, messages, bundle)


def run_probe(items, send, **kw):
    return asyncio.run(op.probe(items, make_llm(send), timeout_s=5, **kw))


ROW = {
    "id": "t-made-1",
    "labels": {"category": "out_of_material", "action": "abstain"},
    "messages": [
        {"role": "user", "content": "달팽이 우체국은 몇 시에 닫아?"},
        {"role": "assistant", "content": "어느 지점을 말하는지 알려 줄래요?"},
        {"role": "user", "content": "그 우체국 말고 토끼 도서관은 언제 닫아?"},
    ],
}


# --- 입력과 요청 모양 ---


def test_request_has_ordered_strict_schema_two_blocks_and_the_file_system_text():
    system = op.load_system()
    prepared = asyncio.run(op.prepare_row(ROW, system, k=5))
    send = FakeSend()
    rows, stopped = run_probe([prepared], send)
    assert not stopped and rows[0]["failure"] is None
    (kwargs,) = send.sent
    fmt = kwargs["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    assert list(schema["properties"]) == ["asked", "quote", "found"]
    assert schema["required"] == ["asked", "quote", "found"]
    assert kwargs["max_completion_tokens"] == 2000 and kwargs["reasoning_effort"] == "medium"
    # system은 파일에서 첫 줄 주석만 뺀 글자 그대로다
    raw = op.PROMPT_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    first, _, rest = raw.partition("\n")
    assert first.startswith("<!--") and "#162" in first and "pataegonia 2026-11-15" in first
    assert kwargs["messages"][0] == {"role": "system", "content": rest.strip("\n")}
    assert "<!--" not in kwargs["messages"][0]["content"]
    assert kwargs["messages"][0]["content"].startswith("당신은 학습자의 마지막 질문을")
    # user는 근거 묶음과 대화 두 블록만이고, 판단 대상 표시는 마지막 학습자 발화에 한 번이다
    user = kwargs["messages"][1]["content"]
    assert [m["role"] for m in kwargs["messages"]] == ["system", "user"]
    assert user.startswith("<근거 묶음>\n") and user.endswith("</대화>")
    bundle, sep, turns = user.partition("\n</근거 묶음>\n\n")
    assert sep and turns.startswith("<대화>\n")
    assert bundle.removeprefix("<근거 묶음>\n") == prepared.bundle
    for absent in ("<신호>", "<판단 예시>", "<다시 확인할 규칙>"):
        assert absent not in user
    assert user.count('판단대상="예"') == 1
    assert '판단대상="예">그 우체국 말고 토끼 도서관은 언제 닫아?</학습자 발화>' in turns


def test_prepare_row_stops_when_the_target_mark_is_not_once():
    row = {**ROW, "messages": [{"role": "assistant", "content": "안녕하세요"}]}
    with pytest.raises(ValueError, match="판단대상"):
        asyncio.run(op.prepare_row(row, "s", k=5))


# --- quote 확인 ---


@pytest.mark.parametrize(
    ("found", "quote", "expected"),
    [
        (True, "고양이 식당은 오전 열 시에 문을 엽니다.", "ok"),  # 공백 · 줄바꿈만 다르다
        (True, "  고양이 식당은\n오전 열 시에 ", "ok"),
        (True, "고양이 식당은 오전 열한 시에 문을 엽니다.", "확인 안 됨"),  # 글자가 다르다
        (True, "", "확인 안 됨"),  # found=true인데 quote가 비었다
        (True, "   ", "확인 안 됨"),
        (False, "", None),
        (False, "아무 글", None),
    ],
)
def test_quote_check(found, quote, expected):
    assert op.quote_check(found, quote, BUNDLE) == expected


# --- 실패 갈래 ---


def test_truncation_and_broken_json_are_failures_not_found():
    send = FakeSend(
        by_mark={
            "cut-parsed": lambda: reply(found=False, finish="length"),  # 파싱돼도 잘림
            "cut-broken": lambda: reply(finish="length", content='{"asked": "가'),
            "broken": lambda: reply(content="{not json"),
            "ok-false": lambda: reply(found=False, quote=""),
        }
    )
    items = [item(i) for i in ("cut-parsed", "cut-broken", "broken", "ok-false")]
    rows, _ = run_probe(items, send)
    by = {r["id"]: r for r in rows}
    assert by["cut-parsed"]["failure"] == by["cut-broken"]["failure"] == "length 잘림"
    assert by["broken"]["failure"] == "JSON 깨짐"
    for key in ("cut-parsed", "cut-broken", "broken"):
        assert by[key]["found"] is None and by[key]["quote_check"] is None
        assert op.outcome(by[key]) == "실패"
    assert by["ok-false"]["failure"] is None and by["ok-false"]["found"] is False
    summary = op.calls_summary(rows)
    assert summary["calls"] == 4 and summary["ok"] == 1
    assert summary["failures"] == {
        "시간 초과": 0, "length 잘림": 2, "JSON 깨짐": 1, "429": 0, "그 밖": 0
    }  # fmt: skip
    assert summary["truncation_pct"] == 50.0
    # 모의에서도 실패는 "없음"이 아니다
    main = {r["id"]: "answer" for r in rows}
    assert op.simulate(rows, main, strict=True)["right"] == 1  # ok-false 하나만


def test_row_record_has_tokens_and_no_text_without_keep_text():
    rows, _ = run_probe([item("a")], FakeSend())
    (row,) = rows
    assert row["found"] is True and row["quote_check"] == "확인 안 됨"
    assert (row["asked_chars"], row["quote_chars"]) == (len(ASKED), len(QUOTE))
    assert (row["completion_tokens"], row["reasoning_tokens"]) == (42, 7)
    assert row["finish_reason"] == "stop" and row["latency_ms"] >= 0
    assert "asked" not in row and "quote" not in row
    kept, _ = run_probe([item("a")], FakeSend(), keep_text=True)
    assert (kept[0]["asked"], kept[0]["quote"]) == (ASKED, QUOTE)


def _status_error(status: int) -> Exception:
    request = httpx.Request("POST", "http://fake.invalid/v1")
    response = httpx.Response(status, request=request)
    return openai.APIStatusError("fake", response=response, body=None)


def test_three_429_in_a_row_stop_sending_the_rest():
    def limited():
        raise _status_error(429)

    send = FakeSend(default=limited)
    items = [item(f"r{i}") for i in range(10)]
    rows, stopped = asyncio.run(op.probe(items, make_llm(send), timeout_s=5, workers=1))
    assert stopped and len(send.sent) == 3
    assert [r["failure"] for r in rows[:3]] == ["429"] * 3
    assert all(r["failure"] == "멈춤으로 안 보냄" for r in rows[3:])
    summary = op.calls_summary(rows)
    assert summary["calls"] == 3 and summary["not_sent"] == 7


def test_timeout_and_other_errors_are_counted_by_branch():
    class Send(FakeSend):
        async def __call__(self, kwargs):
            user = kwargs["messages"][-1]["content"]
            if "slow" in user:
                await asyncio.sleep(1)
            if "auth" in user:
                raise _status_error(401)
            return await super().__call__(kwargs)

    rows, _ = asyncio.run(
        op.probe([item("slow"), item("auth"), item("fine")], make_llm(Send()), timeout_s=0.05)
    )
    assert [r["failure"] for r in rows] == ["시간 초과", "그 밖", None]
    assert rows[1]["error"] == "auth"


# --- 요약 · 모의 · 채택 기준 ---


def rec(row_id, gold, found=None, quote_check=None, failure=None, latency_ms=1000.0, finish="stop"):
    return {
        "id": row_id,
        "gold_action": gold,
        "found": found,
        "quote_check": quote_check,
        "failure": failure,
        "latency_ms": latency_ms,
        "finish_reason": finish,
    }


RECORDS = [
    rec("a1", "abstain", found=False),  # (가)(나) 맞게
    rec("a2", "abstain", found=True, quote_check="확인 안 됨"),  # (나)만 맞게
    rec("a3", "abstain", failure="시간 초과"),  # 실패: 바꾸지 않음
    rec("a4", "abstain", found=True, quote_check="ok"),
    rec("n1", "answer", found=False),  # (가)(나) 틀리게
    rec("n2", "scaffold", found=True, quote_check="확인 안 됨"),  # (나)만 틀리게
    rec("n3", "answer", failure="JSON 깨짐"),
    rec("o1", "refuse", found=False),  # 그 밖
    rec("x1", "abstain", found=False),  # 주 호출이 answer가 아님: 대상 아님
]
MAIN = {r["id"]: "answer" for r in RECORDS} | {"x1": "abstain", "gone": "answer"}


def test_simulation_counts_for_both_rules():
    loose = op.simulate(RECORDS, MAIN, strict=False)
    assert loose == {
        "targets": 9, "no_probe": 1, "right": 1, "wrong": 1, "other": 1, "net": 0
    }  # fmt: skip
    strict = op.simulate(RECORDS, MAIN, strict=True)
    assert strict == {
        "targets": 9, "no_probe": 1, "right": 2, "wrong": 2, "other": 1, "net": 0
    }  # fmt: skip


def test_failed_probe_never_changes_the_main_answer():
    rows = [rec(f"f{i}", "abstain", failure=f) for i, f in enumerate(op.FAILURES)]
    main = {r["id"]: "answer" for r in rows}
    for strict in (False, True):
        s = op.simulate(rows, main, strict=strict)
        assert (s["right"], s["wrong"], s["other"]) == (0, 0, 0)


def test_confusion_table():
    table = op.confusion(RECORDS)
    assert table["abstain"] == {"found=true(ok)": 1, "확인 안 됨": 1, "found=false": 2, "실패": 1}
    assert table["answer·scaffold"] == {
        "found=true(ok)": 0, "확인 안 됨": 1, "found=false": 1, "실패": 1
    }  # fmt: skip
    assert table["그 밖"] == {"found=true(ok)": 0, "확인 안 됨": 0, "found=false": 1, "실패": 0}


def test_adoption_verdicts():
    calls = {"truncation_pct": 4.9, "p95": 3.0}
    ok = op.adoption(calls, {"net": 5, "wrong": 3}, main_p95=3.1)
    assert list(ok.values()) == [True, True, True, True]
    bad = op.adoption({"truncation_pct": 5.0, "p95": 3.1}, {"net": 4, "wrong": 4}, main_p95=3.1)
    assert list(bad.values()) == [False, False, False, False]
    unknown = op.adoption(calls, {"net": 9, "wrong": 0}, main_p95=None)
    assert unknown["대조 p95 < 주 호출 p95"] is None


def test_show_prints_counts_only():
    out = io.StringIO()
    op.show(RECORDS, out, main_actions=MAIN, main_p50=2.0, main_p95=9.0)
    text = out.getvalue()
    assert "1. 호출 9 · 성공 7" in text and "시간 초과 1" in text and "JSON 깨짐 1" in text
    loose = "(가) found=false: 대상 9(대조 기록 없음 1) · 맞게 1 · 틀리게 1 · 그 밖 1 · 순이득 +0"
    assert loose in text
    assert "주 호출 지연 p50 2.00 · p95 9.00" in text
    assert "5. 채택 기준 (가)" in text and "5. 채택 기준 (나)" in text
    assert "순이득 +5 이상 미달" in text and "대조 p95 < 주 호출 p95 통과" in text


# --- run_local 결과 JSON ---


def write_main(tmp_path, data):
    path = tmp_path / "main.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_main_json_actions_by_run(tmp_path):
    data = {"report": {"cv_emitted_by_run": [{"a": {"action": "answer"}}, {"a": {"action": None}}]}}
    path = write_main(tmp_path, data)
    assert op.load_main_actions(path) == {"a": "answer"}
    assert op.load_main_actions(path, 1) == {"a": None}


@pytest.mark.parametrize(
    ("data", "run"),
    [
        ({"report": {}}, 0),
        ({"report": {"cv_emitted_by_run": []}}, 0),
        ({"report": {"cv_emitted_by_run": [{"a": {"action": "answer"}}]}}, 1),
        ({"report": {"cv_emitted_by_run": [{"a": {"citations": 1}}]}}, 0),  # 행별 action 없음
        ({"report": {"cv_emitted_by_run": [["a"]]}}, 0),
    ],
)
def test_main_json_of_unexpected_shape_stops(tmp_path, data, run):
    with pytest.raises(op.MainJsonError):
        op.load_main_actions(write_main(tmp_path, data), run)


# --- 실행 전체(가짜 전송, 교차 검증 160행) ---


@pytest.fixture
def fake_api(monkeypatch):
    send = FakeSend()
    for name, value in (
        ("OPENAI_API_KEY", "sk-fake-test-key"),
        ("OPENAI_BASE_URL", "http://127.0.0.1:9/v1"),
        ("OPENAI_MODEL", "fake-model"),
    ):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(tutor_llm, "_openai_send", lambda: send)
    return send


@pytest.mark.parametrize("keep", [False, True])
def test_main_writes_160_rows_and_keeps_text_only_on_request(fake_api, tmp_path, capsys, keep):
    argv = ["--effort", "low", "--out", str(tmp_path)] + (["--keep-text"] if keep else [])
    assert op.main(argv) == 0
    assert len(fake_api.sent) == op.CV_ROWS
    assert {k["reasoning_effort"] for k in fake_api.sent} == {"low"}
    (path,) = tmp_path.glob("oom-low-*.json")
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert len(data["rows"]) == op.CV_ROWS and data["summary"]["calls"] == op.CV_ROWS
    assert (ASKED in raw, QUOTE in raw) == (keep, keep)
    out = capsys.readouterr().out
    assert ASKED not in out and QUOTE not in out and "sk-fake-test-key" not in out
    assert "1. 호출 160 · 성공 160" in out


def test_main_stops_before_calling_on_a_bad_main_json(fake_api, tmp_path):
    bad = write_main(tmp_path, {"report": {"cv_emitted_by_run": [{"a": {}}]}})
    with pytest.raises(SystemExit):
        op.main(["--effort", "low", "--out", str(tmp_path), "--main", str(bad)])
    assert fake_api.sent == []
