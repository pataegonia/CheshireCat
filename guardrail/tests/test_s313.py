"""S3.13 실험 토글 4개(기본 끔): request_check·block_recheck·output.judge_mode·output.quote_repair.

문장·인용은 이 파일에서 직접 지은 것만 쓴다(공개 세트 문항 문구를 옮기지 않는다).
"""

import json
import shutil
from itertools import product

import pytest
from fakes import Ok, Timeout
from test_guard import MSGS, PING, ROOT, make, req

from guardrail import guard as guard_module
from guardrail.config import config_hash, load_config, load_prompt
from guardrail.decide import recheck_block, request_category
from guardrail.eval import run
from guardrail.eval.fake_llm import make_fake_llm
from guardrail.judges.output_judge import (
    flagged_sentences,
    repair_quote,
    sentence_span,
)
from guardrail.judges.schemas import strict_schema
from guardrail.judges.schemas_exp import OutputSentOut, RequestCheckOut
from guardrail.rules import sentences
from guardrail.schema import Message

pytestmark = pytest.mark.anyio
GROUNDED = {"answer_quote": "", "kb_quote": "", "grounded": True, "refuses": False}
NONSENSE = (Message("user", "qqq qqq qqq"),)  # KB에 걸리는 2-gram이 없는 발화
PRICE = "연필 값은 300원이에요."
ANS3 = f"먼저 가볍게 시작해 볼게요. {PRICE} 마지막으로 응원해 드려요."


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def no_rule(monkeypatch):
    monkeypatch.setattr(guard_module, "literal_rule", lambda *a, **k: None)


@pytest.fixture
def sent(tmp_path):
    """output_judge_sent 프롬프트가 든 임시 프롬프트 폴더를 가리키는 overrides(`__` 표기).

    저장소 프롬프트 대신 짧은 시험용 파일을 쓴다(테스트가 프롬프트 본문에 기대지 않게).
    """
    dst = tmp_path / "prompts"
    shutil.copytree(ROOT / "prompts", dst)
    (dst / "stub_sent.md").write_text(
        '---\nid: output_judge_sent\nversion: "t"\n---\nYou check each sentence.\n', "utf-8"
    )
    return {"paths__prompts": str(dst), "prompts__output_judge_sent": "stub_sent.md"}


# --- 응답 만들기 ---


def _role(kw) -> str:
    name = kw["response_format"]["json_schema"]["name"]
    for prefix, role in (
        ("JbCheck", "jb_check"),
        ("OomCheck", "oom_check"),
        ("RequestCheck", "request_check"),
        ("Input", "input"),
    ):
        if name.startswith(prefix):
            return role
    return "output"


def _asks(*kinds, verdict="not_asked"):
    asks = [{"quote": "q", "wants": "w", "kind": k} for k in kinds]
    return Ok(json.dumps({"asks": asks, "evidence": "e", "verdict": verdict}))


def _responder(
    category="normal", asks=("learn",), verdict="not_asked", output=GROUNDED, jb="study"
):
    def respond(kw):
        if kw["messages"] == PING:
            return Ok()
        role = _role(kw)
        if role == "input":
            return Ok(json.dumps({"evidence": "e", "category": category}))
        if role == "request_check":
            return asks if not isinstance(asks, tuple) else _asks(*asks, verdict=verdict)
        if role == "oom_check":
            return Ok(json.dumps({"evidence": "e", "verdict": verdict}))
        if role == "jb_check":
            return Ok(json.dumps({"evidence": "e", "verdict": jb}))
        return Ok(json.dumps(output, ensure_ascii=False))

    return respond


def _calls(fake, role):
    return [r for r in fake.requests if r["messages"] != PING and _role(r) == role]


def _sent_out(checks, quote="", grounded=True):
    cs = [{"n": n, "kb_phrase": "k", "verdict": v} for n, v in checks]
    return {"checks": cs, "answer_quote": quote, "kb_quote": "", "grounded": grounded,
            "refuses": False}  # fmt: skip


# --- 순수 함수 ---


@pytest.mark.parametrize("first", ["normal", "answer_seeking"])
@pytest.mark.parametrize(
    ("kinds", "want"),
    [
        (["learn", "finished_work_to_submit"], None),  # 1단: 범주를 바꾸지 않는다
        (["offcourse_output", "finished_work_to_submit"], "off_topic"),
        (["learn", "offcourse_output"], "off_topic"),
        (["learn", "other"], None),
        ([], None),
    ],
)
def test_request_category(first, kinds, want):
    assert request_category(first, kinds) == want


@pytest.mark.parametrize("first", ["off_topic", "harmful_content", "emotional_distress"])
def test_request_category_leaves_other_first_categories(first):
    assert request_category(first, ["finished_work_to_submit"]) is None


@pytest.mark.parametrize(
    ("kinds", "stays"),
    [
        (["learn"], False),
        (["learn", "other"], False),
        (["other"], True),  # learn이 없다
        ([], True),
        (["learn", "offcourse_output"], True),
        (["learn", "finished_work_to_submit"], True),
        (["finished_work_to_submit"], True),
    ],
)
def test_recheck_block(kinds, stays):
    assert recheck_block("off_topic", kinds) == ("off_topic" if stays else "normal")


@pytest.mark.parametrize("kinds", [["learn"], [], ["offcourse_output"], ["learn", "other"]])
def test_recheck_block_never_unblocks_academic_dishonesty(kinds):
    assert recheck_block("academic_dishonesty", kinds) == "academic_dishonesty"


def test_recheck_block_never_unblocks_other_categories():
    assert recheck_block("harmful_content", ["learn"]) == "harmful_content"


# --- 스키마 ---


def _objects(node, defs):
    """스키마 안의 모든 object 노드(`$ref`를 따라간다)."""
    if isinstance(node, dict):
        if "$ref" in node:
            yield from _objects(defs[node["$ref"].rsplit("/", 1)[1]], defs)
        if node.get("type") == "object":
            yield node
        for k, v in node.items():
            if k != "$defs":
                yield from _objects(v, defs)
    elif isinstance(node, list):
        for v in node:
            yield from _objects(v, defs)


@pytest.mark.parametrize("model", [RequestCheckOut, OutputSentOut])
def test_strict_schema_covers_nested_objects(model):
    schema = strict_schema(model)
    objs = list(_objects(schema, schema["$defs"]))
    assert len(objs) == 2  # 바깥 + 목록 안의 object
    for o in objs:
        assert o["additionalProperties"] is False and o["required"] == list(o["properties"])
    for d in schema["$defs"].values():  # $defs 안의 정의도 같은 규격
        assert d["additionalProperties"] is False and d["required"] == list(d["properties"])


def test_field_order_is_fixed():
    assert list(RequestCheckOut.model_fields) == ["asks", "evidence", "verdict"]
    assert list(RequestCheckOut.model_fields["asks"].annotation.__args__[0].model_fields) == [
        "quote", "wants", "kind",
    ]  # fmt: skip
    assert list(OutputSentOut.model_fields) == [
        "checks", "answer_quote", "kb_quote", "grounded", "refuses",
    ]  # fmt: skip


# --- 문장 나누기 ---


def _cut(a):
    return [a[s:e] for s, e in sentences(a)]


def test_sentences_keeps_decimals_and_splits_lines_and_trailing_space():
    assert _cut("값은 1.3배예요. 다음이에요! 끝?  ") == ["값은 1.3배예요.", "다음이에요!", "끝?"]
    assert _cut("첫 줄\n둘째 줄") == ["첫 줄\n", "둘째 줄"]
    assert _cut("마침표 없이 끝  ") == ["마침표 없이 끝  "]
    assert sentences("") == [] and sentences("  \n ") == []


def test_sentences_matches_the_replay_script():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "span_replay", ROOT / "scripts/r16/span_replay.py"
    )
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001 - 스크립트가 무거운 것을 읽어 못 올리면 이 대조만 건너뛴다
        pytest.skip("span_replay를 불러올 수 없다")
    for a in [ANS3, "a. b!\n c?(d) 1.5[x] z", "  ", "끝 공백 \n"]:
        assert sentences(a) == mod.sentences(a)


# --- flagged·문장 대체 구간 ---


def _checks(*pairs):
    return OutputSentOut.model_validate(_sent_out(pairs)).checks


def test_flagged_sentences_dedups_sorts_and_drops_out_of_range():
    cs = _checks((3, "conflicts"), (1, "matches"), (3, "not_in_material"), (0, "conflicts"),
                 (9, "conflicts"), (2, "general"))  # fmt: skip
    assert flagged_sentences(cs, 3) == [3]
    cs = _checks((2, "not_in_material"), (1, "conflicts"))
    assert flagged_sentences(cs, 3) == [1, 2]
    assert flagged_sentences([], 3) == []


def test_sentence_span_is_the_first_flagged_sentence_of_the_original_answer():
    assert sentence_span(ANS3, ANS3, [2, 3]) == PRICE
    assert sentence_span(ANS3, ANS3, [3]).startswith("마지막으로")
    assert sentence_span("줄 하나\n줄 둘\n", "줄 하나\n줄 둘\n", [2]) == "줄 둘"  # 줄바꿈은 뗀다


def test_sentence_span_needs_the_same_sentence_count():
    assert sentence_span("가. 나. 다.", "가. 나.", [1]) is None
    assert sentence_span(ANS3, ANS3, []) is None


# --- repair_quote ---


def test_repair_quote_finds_the_closest_stretch_of_the_answer():
    answer = f"첫 문장이에요. {PRICE} 끝."
    near = "연필 값은 300원이어요"  # 한 글자만 다르다
    assert repair_quote(near, answer, 8) == "연필 값은 300원이에요"


def test_repair_quote_returns_none_when_nothing_is_close():
    assert repair_quote("전혀 다른 문장의 구절입니다", ANS3, 8) is None
    assert repair_quote("", ANS3, 8) is None


def test_repair_quote_rejects_short_and_stretched_matches():
    assert repair_quote("연필 값은", f"x. {PRICE}", 8) is None  # 정규화 뒤 8자 미만
    spread = "연필 값은 300원" + "가나다라마바사아자차" * 6 + "이에요"
    assert repair_quote("연필 값은 300원이에요", spread, 8) is None  # 구간이 1.3배를 넘는다


def test_repair_quote_unescapes_entities_first():
    answer = "비교는 a < b 로 해요."
    assert repair_quote("비교는 a &lt; b 로 해요", answer, 8) == "비교는 a < b 로 해요"


# --- 설정 ---


def test_new_toggles_default_off_in_both_configs():
    for path in ("configs/default.yaml", "configs/baseline.yaml"):
        cfg = load_config(path)
        assert not (cfg.toggles.request_check or cfg.toggles.block_recheck)
        assert cfg.output.judge_mode == "quote" and cfg.output.quote_repair is False
        assert cfg.prompts["request_check"] == "request_check_en.md"
    assert config_hash(load_config())  # 프롬프트 머리말이 모두 맞다


def test_judge_mode_rejects_unknown_value():
    with pytest.raises(ValueError, match="judge_mode"):
        load_config(overrides={"output.judge_mode": "words"})


def test_per_role_accepts_request_check():
    cfg = load_config(overrides={"llm.per_role": {"request_check": {"reasoning_effort": "high"}}})
    assert cfg.llm.per_role["request_check"].reasoning_effort == "high"


# --- N1 request_check ---


async def test_request_check_off_by_default_makes_no_call(no_rule):
    guard, fake = await make(_responder(asks=("finished_work_to_submit",)))
    j = await guard.judge(req())
    assert (j.response.category, j.calls) == ("normal", 2)
    assert not _calls(fake, "request_check")


@pytest.mark.parametrize(
    ("first", "kinds", "want"),
    [
        ("normal", ("learn", "finished_work_to_submit"), "normal"),
        ("answer_seeking", ("offcourse_output",), "off_topic"),
        ("normal", ("learn",), "normal"),
        ("answer_seeking", ("learn", "other"), "answer_seeking"),
    ],
)
async def test_request_check_can_move_the_category_and_it_drives_the_output(
    no_rule, first, kinds, want
):
    guard, fake = await make(_responder(first, kinds), toggles__request_check=True)
    j = await guard.judge(req())
    assert (j.response.category, j.input_source, j.calls, j.errors) == (want, "nano", 3, ())
    assert len(_calls(fake, "request_check")) == 1 and not _calls(fake, "oom_check")
    blocked = want in guard.block
    json_in = j.response.to_json(guard.block)["input"]
    assert json_in["decision"] == ("block" if blocked else "allow")
    if blocked:  # 출력 단 ①(true/[])과 safe가 바뀐 범주로 계산된다
        assert j.output_tier == 1
        assert j.response.output.grounded is True and j.response.output.safe is False
    else:
        assert j.output_tier == 4 and j.response.output.safe is True


async def test_request_check_is_called_even_without_kb_hits(no_rule):
    guard, fake = await make(_responder(), toggles__request_check=True)
    j = await guard.judge(req(messages=NONSENSE))
    (sent_req,) = _calls(fake, "request_check")
    assert j.calls == 3 and "<kb" not in sent_req["messages"][1]["content"]


async def test_request_check_request_has_conversation_target_kb_and_no_examples(no_rule):
    guard, fake = await make(_responder(), toggles__request_check=True)
    await guard.judge(req(None, has=False))
    (sent_req,) = _calls(fake, "request_check")
    system, user = (m["content"] for m in sent_req["messages"])
    assert system == load_prompt(guard.cfg, "request_check")[1]
    assert "<target>" in user and "<conversation>" in user and '<kb id="' in user
    assert "<examples>" not in user


async def test_request_check_alone_never_writes_out_of_material(no_rule):
    guard, _ = await make(_responder(verdict="not_stated"), toggles__request_check=True)
    j = await guard.judge(req())
    assert j.response.category == "normal" and j.calls == 3


async def test_request_check_shares_the_oom_slot_when_oom_check_is_on(no_rule):
    guard, fake = await make(
        _responder(verdict="not_stated"), toggles__request_check=True, toggles__oom_check=True
    )
    j = await guard.judge(req())
    assert (j.response.category, j.calls) == ("out_of_material", 3)
    assert not _calls(fake, "oom_check")  # 같은 칸: oom_check는 따로 부르지 않는다
    assert j.response.to_json(guard.block)["input"]["decision"] == "allow"


async def test_request_check_verdict_is_ignored_without_oom_hits(no_rule):
    guard, _ = await make(
        _responder(verdict="not_stated"), toggles__request_check=True, toggles__oom_check=True
    )
    assert (await guard.judge(req(messages=NONSENSE))).response.category == "normal"


async def test_blocking_ask_beats_not_stated(no_rule):
    guard, _ = await make(
        _responder(asks=("offcourse_output",), verdict="not_stated"),
        toggles__request_check=True,
        toggles__oom_check=True,
    )
    assert (await guard.judge(req())).response.category == "off_topic"


async def test_finished_work_ask_does_not_move_normal_but_is_recorded(no_rule):
    guard, fake = await make(
        _responder("normal", ("finished_work_to_submit",)), toggles__request_check=True
    )
    j = await guard.judge(req())
    assert (j.response.category, j.calls, j.errors) == ("normal", 3, ())
    assert len(_calls(fake, "request_check")) == 1  # trace는 노출되지 않아 호출·무오류만 본다


@pytest.mark.parametrize("category", ["harmful_content", "pii_disclosure", "off_topic"])
async def test_request_check_skips_other_first_categories(no_rule, category):
    guard, fake = await make(
        _responder(category, ("finished_work_to_submit",)), toggles__request_check=True
    )
    j = await guard.judge(req())
    assert j.response.category == category and not _calls(fake, "request_check")


async def test_request_check_is_skipped_after_the_jailbreak_second_call(no_rule):
    guard, fake = await make(_responder("jailbreak_injection"), toggles__request_check=True)
    j = await guard.judge(req())
    assert (j.response.category, j.calls) == ("normal", 3)
    assert not _calls(fake, "request_check")


@pytest.mark.parametrize("step", [Timeout(), Ok("not json")])
async def test_request_check_failure_keeps_first_category_and_is_counted(no_rule, step):
    guard, _ = await make(_responder(asks=step), toggles__request_check=True)
    j = await guard.judge(req())
    assert j.response.category == "normal" and j.calls == 3
    assert "request_check" in j.errors and "oom_check" not in j.errors


async def test_request_check_failure_with_oom_on_does_not_report_oom(no_rule):
    guard, _ = await make(
        _responder(asks=Timeout()), toggles__request_check=True, toggles__oom_check=True
    )
    j = await guard.judge(req())
    assert j.errors.count("request_check") == 1 and "oom_check" not in j.errors


async def test_classify_does_not_run_request_check_or_search_the_kb(no_rule):
    guard, fake = await make(
        _responder(asks=("finished_work_to_submit",)),
        toggles__request_check=True,
        toggles__block_recheck=True,
    )
    ranked = []

    class Counting:
        def __getattr__(self, name):
            return getattr(guard.kb, name)

        def rank(self, query):
            ranked.append(query)
            return guard.kb.rank(query)

    guard.kb = Counting()
    assert await guard.classify(MSGS) == "normal"
    assert ranked == [] and not _calls(fake, "request_check")


# --- N2 block_recheck ---


async def test_block_recheck_turns_a_learn_only_block_back_to_normal(no_rule):
    guard, fake = await make(_responder("off_topic", ("learn",)), toggles__block_recheck=True)
    j = await guard.judge(req())
    assert (j.response.category, j.calls, j.errors) == ("normal", 3, ())
    assert j.response.to_json(guard.block)["input"]["decision"] == "allow"
    assert j.output_tier == 4 and len(_calls(fake, "request_check")) == 1


async def test_block_recheck_does_not_call_for_academic_dishonesty(no_rule):
    guard, fake = await make(
        _responder("academic_dishonesty", ("learn",)), toggles__block_recheck=True
    )
    j = await guard.judge(req())
    assert (j.response.category, j.calls) == ("academic_dishonesty", 2)
    assert not _calls(fake, "request_check")


@pytest.mark.parametrize(
    "kinds", [("finished_work_to_submit",), ("offcourse_output", "learn"), ("other",)]
)
async def test_block_recheck_keeps_the_block_unless_learn_alone(no_rule, kinds):
    guard, _ = await make(_responder("off_topic", kinds), toggles__block_recheck=True)
    assert (await guard.judge(req())).response.category == "off_topic"


async def test_block_recheck_failure_keeps_first_and_is_counted(no_rule):
    guard, _ = await make(_responder("off_topic", asks=Timeout()), toggles__block_recheck=True)
    j = await guard.judge(req())
    assert (j.response.category, j.calls) == ("off_topic", 3) and "request_check" in j.errors


async def test_block_recheck_is_independent_of_request_check_and_oom(no_rule):
    guard, fake = await make(
        _responder("off_topic", ("learn",), verdict="not_stated"),
        toggles__block_recheck=True,
        toggles__oom_check=True,
    )
    j = await guard.judge(req())
    assert j.response.category == "normal"  # 이 경로는 oom_check 칸을 쓰지 않는다
    assert not _calls(fake, "oom_check") and j.calls == 3


async def test_each_flag_only_handles_its_own_side(no_rule):
    only_block, fake1 = await make(
        _responder("normal", ("finished_work_to_submit",)), toggles__block_recheck=True
    )
    assert (await only_block.judge(req())).response.category == "normal"
    assert not _calls(fake1, "request_check")
    only_req, fake2 = await make(_responder("off_topic", ("learn",)), toggles__request_check=True)
    assert (await only_req.judge(req())).response.category == "off_topic"
    assert not _calls(fake2, "request_check")


async def test_block_recheck_alone_still_gives_kb_context(no_rule):
    guard, fake = await make(_responder("off_topic", ("learn",)), toggles__block_recheck=True)
    await guard.judge(req(None, has=False))
    (sent_req,) = _calls(fake, "request_check")
    assert '<kb id="' in sent_req["messages"][1]["content"]


async def test_block_recheck_without_kb_hits_still_calls(no_rule):
    guard, fake = await make(_responder("off_topic", ("learn",)), toggles__block_recheck=True)
    j = await guard.judge(req(messages=NONSENSE))
    (sent_req,) = _calls(fake, "request_check")
    assert "<kb" not in sent_req["messages"][1]["content"] and j.response.category == "normal"


_CATS = ["normal", "answer_seeking", "off_topic", "academic_dishonesty", "jailbreak_injection",
         "harmful_content"]  # fmt: skip


@pytest.mark.parametrize("category", _CATS)
@pytest.mark.parametrize(
    "flags",
    list(product([False, True], repeat=4)),
    ids=lambda f: "".join("1" if x else "0" for x in f),
)
async def test_no_combination_exceeds_three_calls(no_rule, category, flags):
    jb, oom, rc, br = flags
    guard, fake = await make(
        _responder(category, ("finished_work_to_submit",), verdict="not_stated"),
        toggles__jb_check=jb,
        toggles__oom_check=oom,
        toggles__request_check=rc,
        toggles__block_recheck=br,
    )
    j = await guard.judge(req())
    sent_n = len([r for r in fake.requests if r["messages"] != PING])
    assert sent_n <= 3 and j.calls == sent_n


# --- N3 judge_mode ---


async def test_quote_mode_is_unchanged_and_sends_no_sentence_block(no_rule):
    guard, fake = await make(_responder())
    await guard.judge(req(ANS3))
    (sent_req,) = _calls(fake, "output")
    system, user = (m["content"] for m in sent_req["messages"])
    assert system == load_prompt(guard.cfg, "output_judge")[1] and "<sentences>" not in user
    assert sent_req["response_format"]["json_schema"]["name"] == "OutputOut"


async def test_sentences_mode_sends_numbered_escaped_sentences_after_the_answer(no_rule, sent):
    guard, fake = await make(
        _responder(output=_sent_out([])), output__judge_mode="sentences", **sent
    )
    answer = "첫 줄 <b>태그</b>예요.\n둘째 줄은 1.3배예요! 셋째예요."
    await guard.judge(req(answer))
    (sent_req,) = _calls(fake, "output")
    system, user = (m["content"] for m in sent_req["messages"])
    assert system == "You check each sentence.\n"
    assert sent_req["response_format"]["json_schema"]["name"] == "OutputSentOut"
    block = "[1] 첫 줄 &lt;b&gt;태그&lt;/b&gt;예요.\n[2] 둘째 줄은 1.3배예요!\n[3] 셋째예요."
    assert f"</answer>\n\n<sentences>\n{block}\n</sentences>" in user


async def test_sentences_mode_caps_the_block_at_twenty(no_rule, sent):
    guard, fake = await make(
        _responder(output=_sent_out([])), output__judge_mode="sentences", **sent
    )
    await guard.judge(req("".join(f"문장 {i}번이에요. " for i in range(1, 31))))
    user = _calls(fake, "output")[0]["messages"][1]["content"]
    assert "[20] 문장 20번이에요." in user and "[21]" not in user


async def test_baseline_mode_ignores_judge_mode(no_rule):
    base = {"reason": "r", "grounded": True, "safe": True}
    guard, fake = await make(
        _responder(output=base), output__mode="baseline", output__judge_mode="sentences"
    )
    await guard.judge(req(ANS3))
    (sent_req,) = _calls(fake, "output")
    assert "<sentences>" not in sent_req["messages"][1]["content"]
    assert sent_req["response_format"]["json_schema"]["name"] == "OutputBaselineOut"


async def test_flagged_sentence_makes_it_ungrounded_even_if_grounded_is_true(no_rule, sent):
    out = _sent_out([(1, "general"), (2, "conflicts"), (3, "general")], quote=PRICE[:-1])
    guard, _ = await make(
        _responder(output=out), output__judge_mode="sentences", output__span_tidy=False, **sent
    )
    j = await guard.judge(req(ANS3))
    assert (j.output_tier, j.nano_grounded, j.answer_quote_ok) == (3, False, True)
    assert j.response.output.grounded is False and j.response.output.unsupported == (PRICE[:-1],)


async def test_all_clear_checks_stay_grounded(no_rule, sent):
    out = _sent_out([(1, "general"), (2, "matches"), (9, "conflicts")])  # 9번은 범위 밖
    guard, _ = await make(_responder(output=out), output__judge_mode="sentences", **sent)
    j = await guard.judge(req(ANS3))
    assert (j.output_tier, j.nano_grounded) == (4, True)


async def test_failed_quote_falls_back_to_the_first_flagged_sentence(no_rule, sent):
    out = _sent_out([(2, "not_in_material"), (3, "conflicts")], quote="전혀 없는 구절이에요 정말")
    guard, _ = await make(
        _responder(output=out), output__judge_mode="sentences", output__span_tidy=False, **sent
    )
    j = await guard.judge(req(ANS3))
    assert j.answer_quote_ok is False  # 인용 자체의 검증 결과를 그대로 둔다
    assert j.output_tier == 3 and j.response.output.unsupported == (PRICE,)


async def test_sentence_fallback_needs_a_flagged_sentence(no_rule, sent):
    out = _sent_out([(1, "general")], quote="전혀 없는 구절이에요 정말", grounded=False)
    guard, _ = await make(_responder(output=out), output__judge_mode="sentences", **sent)
    j = await guard.judge(req(ANS3))
    assert j.output_tier == 4 and j.response.output.unsupported == ()  # 인용 미달: 기본값 ④


async def test_valid_quote_is_kept_over_the_sentence(no_rule, sent):
    quote = "연필 값은 300원"
    out = _sent_out([(2, "conflicts")], quote=quote)
    guard, _ = await make(
        _responder(output=out), output__judge_mode="sentences", output__span_tidy=False, **sent
    )
    assert (await guard.judge(req(ANS3))).response.output.unsupported == (quote,)


# --- N4 quote_repair ---

NEAR = "연필 값은 300원이어요"  # PRICE와 한 글자 다르다
BAD = {"answer_quote": NEAR, "kb_quote": "", "grounded": False, "refuses": False}


async def test_quote_repair_turns_a_near_miss_into_the_span(no_rule):
    on, _ = await make(_responder(output=BAD), output__quote_repair=True, output__span_tidy=False)
    off, _ = await make(_responder(output=BAD), output__span_tidy=False)
    a, b = await on.judge(req(ANS3)), await off.judge(req(ANS3))
    assert (a.output_tier, a.quote_repaired, a.answer_quote_ok) == (3, True, False)
    assert a.response.output.unsupported == ("연필 값은 300원이에요",)
    assert (b.output_tier, b.quote_repaired, b.response.output.unsupported) == (4, False, ())


async def test_exact_quote_does_not_call_repair(no_rule, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("repair_quote를 부르면 안 된다")

    monkeypatch.setattr(guard_module, "repair_quote", boom)
    out = {**BAD, "answer_quote": "연필 값은 300원이에요"}
    guard, _ = await make(_responder(output=out), output__quote_repair=True)
    j = await guard.judge(req(ANS3))
    assert (j.output_tier, j.quote_repaired) == (3, False)


async def test_unrepairable_quote_keeps_the_default_tier(no_rule):
    out = {**BAD, "answer_quote": "전혀 다른 문장의 구절입니다"}
    guard, _ = await make(_responder(output=out), output__quote_repair=True)
    j = await guard.judge(req(ANS3))
    assert (j.output_tier, j.quote_repaired) == (4, False)


async def test_repair_runs_before_the_sentence_fallback(no_rule, sent):
    out = _sent_out([(3, "conflicts")], quote=NEAR)
    guard, _ = await make(
        _responder(output=out),
        output__judge_mode="sentences",
        output__quote_repair=True,
        output__span_tidy=False,
        **sent,
    )
    j = await guard.judge(req(ANS3))
    assert j.quote_repaired is True
    assert j.response.output.unsupported == ("연필 값은 300원이에요",)  # 3번 문장이 아니다


# --- 평가 도구 ---


def test_calls_per_row_counts_at_most_one_extra_input_call():
    base = {"toggles.jb_check": False, "toggles.oom_check": False}
    assert run.calls_per_row(load_config(overrides=base), True) == 2
    for key in ("toggles.request_check", "toggles.block_recheck"):
        cfg = load_config(overrides={**base, key: True})
        assert (run.calls_per_row(cfg, True), run.calls_per_row(cfg, False)) == (3, 2)
    both = load_config(
        overrides={**base, "toggles.request_check": True, "toggles.block_recheck": True}
    )
    assert run.calls_per_row(both, True) == 3


async def test_fake_llm_answers_the_new_schemas():
    llm = make_fake_llm(load_config().llm)
    await llm.start()
    for schema, role in ((RequestCheckOut, "request_check"), (OutputSentOut, "output_judge")):
        res = await llm.complete(role, [{"role": "user", "content": "x"}], schema, deadline=1e12)
        assert isinstance(res.parsed, schema)


async def test_fake_run_drives_all_new_toggles_end_to_end(tmp_path, monkeypatch, sent):
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setattr(run, "HOLDOUT_VIEWS", tmp_path / "holdout_views.json")
    sets = {
        "toggles.request_check": "true", "toggles.block_recheck": "true",
        "output.judge_mode": "sentences", "output.quote_repair": "true",
        "paths.prompts": sent["paths__prompts"],
        "prompts.output_judge_sent": sent["prompts__output_judge_sent"],
    }  # fmt: skip
    argv = ["--config", str(ROOT / "configs/default.yaml"), "--name", "t", "--fake",
            "--out", str(tmp_path / "runs")]  # fmt: skip
    for k, v in sets.items():
        argv += ["--set", f"{k}={v}"]
    assert await run.amain(argv) == 0
    out = tmp_path / "runs" / "t"
    summary = json.loads((out / "summary.json").read_text("utf-8"))
    recs = [json.loads(x) for x in (out / "responses.jsonl").read_text("utf-8").splitlines()]
    score = summary["configs"]["t"]["scores"][0]
    assert score["errors"] == {} and "request_check" in score["llm_by_role"]
    assert all("quote_repaired" in r for r in recs) and max(r["calls"] for r in recs) <= 3
