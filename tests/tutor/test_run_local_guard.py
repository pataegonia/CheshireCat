"""run_local --guard(#83): 폴드마다 평가 묶음을 뺀 가드를 붙여 잰다(TU-10·TU-16).

운영진 API는 부르지 않는다. 가드 LLM·`Guard.build`는 가짜로 바꾸고, 실행 경로의 연결부
(`_own_state` → `_guard_setup` → `build_fold_guards` → `fold_states` → `_collect`의 `for_row`)는
그대로 지난다. 실제 `Guard.build`와의 호출 모양은 따로 확인한다.
"""

import asyncio
import json
from collections import Counter
from types import SimpleNamespace

import pytest
from guardrail.config import load_config as load_guard_config
from guardrail.data import labeled_rows, load_public
from guardrail.eval.cv import load_splits
from guardrail.guard import Guard
from guardrail.llm import LLM

from tests.tutor.tutor_testlib import block_openai
from tutor import TUTOR_DEFAULT
from tutor.eval import run_local
from tutor.judge.guard_merge import has_classify, has_judge_output


class FakeGuardLLM:
    """가드 LLM 자리. start(probe)를 한 루프에서 몇 번 불렀는지 기록한다."""

    made: list["FakeGuardLLM"] = []
    probe = {"base": None}  # 기동 시험 결과(맨 요청이 실패하면 오류 코드)

    def __init__(self, cfg):
        self.cfg = cfg
        self.calls: Counter[str] = Counter()
        self.max_calls = None
        self.loops = []
        self.probe_result = {}
        FakeGuardLLM.made.append(self)

    async def start(self):
        self.loops.append(asyncio.get_running_loop())
        self.probe_result = dict(FakeGuardLLM.probe)

    async def complete(self, role, error=None):
        """가드 판정이 부르는 자리. 상한이 0이면 보내지 않고 budget이다."""
        if self.max_calls is not None and sum(self.calls.values()) >= self.max_calls:
            return SimpleNamespace(error="budget")
        self.calls[role] += 1
        return SimpleNamespace(error=error)


class FakeGuard:
    """`Guard.build(cfg, *, llm, rows)`와 같은 모양. 받은 학습 행을 남긴다."""

    def __init__(self, llm, rows):
        self.llm = llm
        self.ids = frozenset(r.id for r in rows)
        self.endpoints = frozenset(r.endpoint for r in rows)

    @classmethod
    async def build(cls, cfg, *, llm=None, rows=None):
        return cls(llm, rows)


@pytest.fixture
def fakes(monkeypatch):
    FakeGuardLLM.made = []
    FakeGuardLLM.probe = {"base": None}
    monkeypatch.setattr(run_local, "LLM", FakeGuardLLM)
    monkeypatch.setattr(run_local, "Guard", FakeGuard)


def measure(monkeypatch, tmp_path, *argv, during=None):
    """main을 끝까지 돌리고 행마다 받은 state와 그때의 루프를 모은다.

    during(state)이 있으면 행마다 응답 전에 부른다(가드 호출·건너뜀을 흉내 낸다).
    """
    block_openai(monkeypatch)
    seen = {}

    async def respond(preset, row, state=None):
        seen[row["id"]] = (state, asyncio.get_running_loop())
        if during is not None:
            await during(state)
        return dict(TUTOR_DEFAULT)

    monkeypatch.setattr(run_local, "_respond", respond)
    code = run_local.main(["--preset", "tutor", "--out", str(tmp_path), *argv])
    return code, seen


def assert_guard_never_trained_on_the_row(seen):
    split = load_splits()
    held = set(split.holdout)
    # 홀드아웃 행은 재지 않는다(#84): 교차 검증 행만 폴드 가드로 잰다
    cv_rows = {row["id"] for row in run_local.public_tutor_rows()} - run_local.holdout_ids()
    assert set(seen) == cv_rows
    for row_id, (state, _loop) in seen.items():
        guard = state.guard
        clusters = {split.cluster[i] for i in guard.ids}
        assert split.cluster[row_id] not in clusters, row_id  # 자기 묶음이 가드 kNN·LR에 없다
        assert not clusters & held, row_id  # 홀드아웃 묶음은 늘 뺀다
        assert row_id not in guard.ids


def test_fold_guards_leave_out_the_eval_fold_and_holdout():
    rows = run_local.public_tutor_rows()
    split = load_splits()
    held = set(split.holdout)
    llm = object()
    guards = asyncio.run(run_local.build_fold_guards(rows, llm=llm, build=FakeGuard.build))
    assert set(guards) == {str(f) for f in range(split.folds)} | {"holdout"}
    for key, guard in guards.items():
        assert guard.llm is llm  # 가드 LLM 호출기 하나를 같이 쓴다
        assert guard.endpoints == {"guard", "tutor"}  # 공개 400행(가드·튜터)에서 고른다
        clusters = {split.cluster[i] for i in guard.ids}
        assert not clusters & held
        if key != "holdout":
            assert all(str(split.fold[c]) != key for c in clusters)
    # 폴드 가드는 홀드아웃과 자기 폴드만 빼고, 홀드아웃 가드는 홀드아웃만 뺀다
    every = {r.id for r in labeled_rows(load_public(load_guard_config().paths.public))}
    assert guards["holdout"].ids == {i for i in every if split.cluster[i] not in held}


def test_cli_guard_attaches_a_fold_guard_to_every_row(monkeypatch, tmp_path, capsys, fakes):
    code, seen = measure(monkeypatch, tmp_path, "--guard")
    assert code == 0
    assert_guard_never_trained_on_the_row(seen)
    (llm,) = FakeGuardLLM.made
    states = [state for state, _loop in seen.values()]
    assert len({id(s.guard) for s in states}) == load_splits().folds
    assert all(s.guard.llm is llm for s in states)
    # 가드 LLM은 응답을 모으는 루프에서 한 번 기동한다(클라이언트가 루프에 묶인다)
    loops = {id(loop) for _state, loop in seen.values()}
    assert len(llm.loops) == 1 and loops == {id(llm.loops[0])}
    # 호출 몫: probe 5 + 요청당 2회 × 160행 × 1.1을 튜터 몫에서 뗀다(합은 그대로). probe는
    # complete를 거치지 않아 calls에 세지 않으므로 가드 LLM 상한에서는 뺀다
    probe = run_local.guard_probe_calls(load_guard_config().llm)
    assert probe == 5
    assert llm.max_calls == 352
    assert states[0].tutor_llm.max_calls + llm.max_calls + probe == 5000
    out = capsys.readouterr().out
    assert "운영진 API 예상 호출 533회" in out  # 528 + probe 5
    assert "가드 LLM 호출: 0회 (상한 352회)" in out
    assert "가드 LLM 최종 오류: 없음" in out
    assert "가드 단계 건너뜀(모든 실행 합): 없음" in out
    (saved,) = tmp_path.glob("tutor-*.json")
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["tainted"] is False
    assert data["guard_run"] == {
        "calls": {},
        "max_calls": 352,
        "errors": {},
        "skips": {},
        "clean": True,
    }


def test_cli_guard_reports_the_gate_conditions_per_request(monkeypatch, tmp_path, capsys, fakes):
    """결과 화면·JSON에서 관문 조건(응함·지연·요청당 nano 최대)을 바로 읽는다(#83)."""

    async def during(state):
        state.tutor_llm.calls["main"] += 1  # 한 요청이 튜터 1회 + 가드 2회(classify·judge_output)
        await state.guard.llm.complete("classify")
        await state.guard.llm.complete("judge_output")

    code, seen = measure(monkeypatch, tmp_path, "--guard", during=during)
    assert code == 0
    (saved,) = tmp_path.glob("tutor-*.json")
    gate = json.loads(saved.read_text(encoding="utf-8"))["gate"]
    assert gate["nano_per_request"] == {"max": 3, "histogram": {"3": len(seen)}, "n": len(seen)}
    assert gate["limits"] == {"p50_s": 6.0, "p95_s": 12.0, "nano": 4}
    assert gate["pass"]["nano"] is True and gate["pass"]["p50"] is True
    assert gate["attack_complied_by_run"] is None  # --judge 없이는 응함을 재지 않는다
    out = capsys.readouterr().out
    assert "관문 조건(#83):" in out
    assert "요청당 nano 최대 3회(≤4) 통과" in out


def test_cli_guard_marks_skipped_or_failed_guard_rows_tainted(monkeypatch, tmp_path, fakes):
    """가드 단계를 건너뛰었거나 가드 호출이 오류로 끝나면 결과 JSON에 남고 비교 기준이 못 된다."""

    async def during(state):
        await state.guard.llm.complete("input", error="timeout")
        state.guard_skips[("classify", "missing")] += 1

    code, seen = measure(monkeypatch, tmp_path, "--guard", during=during)
    assert code == 1
    (saved,) = tmp_path.glob("tutor-*.json")
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["tainted"] is True
    run = data["guard_run"]
    assert run["errors"] == {"timeout": len(seen)}
    assert run["skips"] == {"classify missing": len(seen)}
    assert run["clean"] is False
    with pytest.raises(ValueError, match="tainted"):
        run_local.load_previous(saved, judge=False)


def test_cli_guard_rate_limit_stops_both_guard_and_tutor(monkeypatch, tmp_path, fakes):
    """가드 쪽 429가 이어져도 멈추고, 멈추면 튜터 LLM과 가드 LLM 모두 더 보내지 않는다."""

    async def during(state):
        await state.guard.llm.complete("input", error="rate_limit")

    code, seen = measure(monkeypatch, tmp_path, "--guard", during=during)
    assert code == run_local.EXIT_RATE_LIMITED
    state, _loop = next(iter(seen.values()))
    assert state.guard.llm.max_calls == 0
    assert state.tutor_llm.max_calls == 0


def test_cli_guard_stops_when_the_probe_base_request_fails(monkeypatch, tmp_path, fakes):
    """가드 probe의 맨 요청이 실패하면(429 등, 예외 없이 결과로 온다) 튜터를 부르기 전에 멈춘다."""
    FakeGuardLLM.probe = {"base": "rate_limit"}
    with pytest.raises(SystemExit) as stopped:
        measure(monkeypatch, tmp_path, "--guard")
    assert stopped.value.code == 2
    assert not list(tmp_path.glob("tutor-*.json"))


def test_guard_flag_is_only_for_the_public_set(monkeypatch, tmp_path):
    block_openai(monkeypatch)
    with pytest.raises(SystemExit) as stopped:
        run_local.main(["--preset", "tutor", "--guard", "--cases", "team", "--out", str(tmp_path)])
    assert stopped.value.code == 2


def test_cli_guard_with_examples_keeps_both_per_row(monkeypatch, tmp_path, fakes):
    code, seen = measure(monkeypatch, tmp_path, "--guard", "--tutor-set", "toggles.fewshot=knn")
    assert code == 0
    assert_guard_never_trained_on_the_row(seen)
    split = load_splits()
    for row_id, (state, _loop) in seen.items():
        clusters = {split.cluster[e.id] for e in state.tutor_examples.rows}
        assert split.cluster[row_id] not in clusters


def test_cli_refuses_a_guard_that_is_not_per_fold(monkeypatch, tmp_path, fakes):
    monkeypatch.setattr(run_local, "fold_states", lambda state, _cfg, _rows, **_kw: state)
    with pytest.raises(SystemExit) as stopped:
        measure(monkeypatch, tmp_path, "--guard")
    assert stopped.value.code == 2
    assert not list(tmp_path.glob("tutor-*.json"))


def test_guard_flag_is_only_for_the_tutor_preset(monkeypatch, tmp_path):
    block_openai(monkeypatch)
    with pytest.raises(SystemExit) as stopped:
        run_local.main(["--preset", "reference", "--guard", "--out", str(tmp_path)])
    assert stopped.value.code == 2


def test_real_guard_build_accepts_the_fold_call():
    """가짜 대신 실제 `Guard.build`가 같은 호출 모양(cfg, llm=, rows=)을 받는지 본다.

    LLM은 보내지 않는 가짜 전송을 주입한다(주입한 llm은 start하지 않는다).
    """

    async def never(_kwargs):
        raise AssertionError("기동 중에 LLM을 부르지 않는다")

    cfg = load_guard_config()
    llm = LLM(cfg.llm, send=never)
    rows = [
        r
        for r in run_local.public_tutor_rows()
        if run_local._fold_key(load_splits(), r["id"]) == "0"
    ]
    guards = asyncio.run(run_local.build_fold_guards(rows[:1], llm=llm))
    (guard,) = guards.values()
    assert isinstance(guard, Guard)
    # S2.11(#119) 전에는 튜터가 부르는 두 메서드가 없다: 튜터는 이 단계를 건너뛴다(missing)
    assert (has_classify(guard), has_judge_output(guard)) == (
        hasattr(Guard, "classify"),
        hasattr(Guard, "judge_output"),
    )
