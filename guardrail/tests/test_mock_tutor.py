"""S0.6 모의 튜터: BM25 순위, 검색 적중, 생성 메시지 구성, FakeLLM으로 끝까지."""

import pytest
from fakes import FakeLLM, Ok, Timeout, fake_config

from guardrail import Guard
from guardrail.eval.mock_tutor import (
    BASE_INSTRUCTION,
    BM25,
    DEFAULT_TEXTBOOK,
    NOT_FOUND,
    MockTutor,
    Section,
    TutorError,
    bm25_top_k,
    load_textbook,
)
from guardrail.types import Chunk, Turn

pytestmark = pytest.mark.anyio

TOY = [
    Section("t-1", "물", "물은 0도에서 얼고 100도에서 끓는다."),
    Section("t-2", "밀도", "밀도는 질량을 부피로 나눈 값이다. 밀도 단위는 g/cm³이다."),
    Section("t-3", "광합성", "광합성은 엽록체에서 일어난다."),
]


# --- 검색 ---


def test_bm25_toy_ranking_is_fixed():
    got = bm25_top_k("밀도는 어떻게 구해?", TOY, k=3)
    assert [c.source for c in got][0] == "t-2 밀도"
    assert [c.sid for c in got] == [f"S{i}" for i in range(1, len(got) + 1)]
    assert got[0].text == TOY[1].text


def test_bm25_drops_zero_score_sections():
    assert bm25_top_k("엽록체", TOY, k=3) == [Chunk("S1", "t-3 광합성", TOY[2].text)]
    assert bm25_top_k("zzz", TOY, k=3) == []


def test_bm25_ignores_whitespace_and_case():
    index = BM25(TOY)
    assert index.search("밀 도", 1) == index.search("밀도", 1)
    assert index.search("G/CM³", 1)[0].source == "t-2 밀도"


def test_bm25_parameters():
    index = BM25(TOY)
    assert (index.k1, index.b) == (1.2, 0.75)


def test_textbook_loads_about_40_sections():
    sections = load_textbook(DEFAULT_TEXTBOOK)
    assert 36 <= len(sections) <= 44
    assert all(200 <= len(s.text) <= 350 for s in sections)
    assert len({s.source for s in sections}) == len(sections)


# 손으로 쓴 10문항: 정답 절이 top-3에 들어야 한다(≥8/10, 이 슬라이스 자체 기준)
HAND_WRITTEN = [
    ("얼음이 녹는 온도를 뭐라고 해?", "sci-03"),
    ("물은 몇 도에서 끓어?", "sci-04"),
    ("드라이아이스가 바로 기체가 되는 현상은?", "sci-05"),
    ("밀도 공식이 뭐야?", "sci-09"),
    ("54 g이고 20 cm³인 금속의 밀도는?", "sci-10"),
    ("광합성의 화학 반응식을 알려 줘", "sci-16"),
    ("기공은 무슨 역할을 해?", "sci-19"),
    ("무게 = 질량 × 9.8 은 어떻게 써?", "sci-24"),
    ("조작 변인과 통제 변인의 차이는?", "sci-29"),
    ("알코올램프 불은 어떻게 꺼?", "sci-35"),
]


def test_hand_written_top3_hits_at_least_8_of_10():
    tutor = MockTutor(FakeLLM())
    hits = 0
    for question, source in HAND_WRITTEN:
        chunks = tutor.index.search(question, 3)
        hits += any(c.source.startswith(source + " ") for c in chunks)
    assert hits >= 8


async def test_retrieve_returns_top3_chunks():
    tutor = MockTutor(FakeLLM())
    chunks = await tutor.retrieve("승화가 뭐야?", [Turn("student", "안녕")])
    assert 1 <= len(chunks) <= 3
    assert chunks[0].sid == "S1"


# --- 생성 ---


async def test_generate_message_layout():
    llm = FakeLLM(script=[Ok("답이다. [S1]")])
    tutor = MockTutor(llm)
    history = [Turn("student", f"q{i}") if i % 2 == 0 else Turn("tutor", f"a{i}") for i in range(6)]
    chunks = [
        Chunk("S1", "sci-01 a", "첫 청크."),
        Chunk("S2", "sci-02 b", "둘째."),
        Chunk("S3", "c", "셋째."),
    ]
    answer = await tutor.generate(
        "질문?", history, chunks, system_extra="가드 문구", instruction="개념 설명으로 돌려라"
    )
    assert answer == "답이다. [S1]"
    msgs = llm.requests[0]["messages"]
    system = msgs[0]["content"]
    assert msgs[0]["role"] == "system"
    assert system.startswith(BASE_INSTRUCTION)
    assert "\n\n가드 문구" in system and "추가 지시: 개념 설명으로 돌려라" in system
    assert "[S1]" in BASE_INSTRUCTION and NOT_FOUND in BASE_INSTRUCTION
    # 최근 4턴만: student → user, tutor → assistant
    assert [m["role"] for m in msgs[1:-1]] == ["user", "assistant", "user", "assistant"]
    assert msgs[1]["content"] == "q2"
    user = msgs[-1]["content"]
    assert user.startswith(
        "[자료]\n[S1] (sci-01 a) 첫 청크.\n[S2] (sci-02 b) 둘째.\n[S3] (c) 셋째."
    )
    assert user.endswith("\n\n[질문]\n질문?")
    assert llm.requests[0]["timeout"] == llm.cfg.deadlines_s.generator
    assert llm.requests[0]["role"] == "generator"


async def test_generate_without_extras_uses_base_prompt_only():
    llm = FakeLLM(script=[Ok("x")])
    tutor = MockTutor(llm)
    await tutor.generate("q", [], [])
    assert llm.requests[0]["messages"][0]["content"] == tutor.system_prompt == BASE_INSTRUCTION


async def test_generate_error_raises_tutor_error_with_code_only():
    llm = FakeLLM(script=[Timeout(), Timeout(), Timeout()])
    with pytest.raises(TutorError) as err:
        await MockTutor(llm).generate("q", [], [])
    assert err.value.code == "timeout" and str(err.value) == "timeout"


def test_base_instruction_has_no_safety_wording():
    # 무방비 베이스라인은 가드 없이 생성 모델이 버티는 정도를 잰다(안전 문구 없음)
    for word in ("거절", "위험", "안전", "유해"):
        assert word not in BASE_INSTRUCTION


# --- 파이프라인 끝까지 (네트워크 없음) ---


async def test_unguarded_guard_with_mock_tutor_end_to_end():
    llm = FakeLLM(responder=lambda req: Ok("승화는 고체가 바로 기체가 되는 것이다. [S1]"))
    guard = Guard(llm.cfg, llm=llm)
    tutor = MockTutor(llm)
    v = await guard.respond("승화가 뭐야?", [], tutor)
    assert v.response == "승화는 고체가 바로 기체가 되는 것이다. [S1]"
    by = {s.stage: s for s in v.stages}
    assert by["retrieve"].status == by["generate"].status == "ok"
    assert by["generate"].tokens_in == 0 and by["generate"].detail == {"chars": len(v.response)}
    assert llm.requests[0]["messages"][0]["content"] == tutor.system_prompt  # system_extra == ""


async def test_fake_ok_generator_cites_first_chunk():
    llm = FakeLLM(fake_config(**{"faults.generator": "fake_ok"}), max_calls=0)
    tutor = MockTutor(llm)
    v = await Guard(llm.cfg, llm=llm).respond("물은 몇 도에서 끓어?", [], tutor)
    assert v.response.endswith(" [S1]") and v.response != NOT_FOUND
    assert llm.requests == []
