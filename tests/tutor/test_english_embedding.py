"""영어 발화는 임베딩 순위만 검색(`toggles.english_embedding`). 임베딩·LLM은 가짜다(API 호출 없음).

입력 문장은 지어낸 것이다(공개 세트 · KB 문구를 옮기지 않는다).
"""

import dataclasses

import numpy as np

from tests.tutor.test_embedding import FakeEmbed, run, small_index
from tests.tutor.test_flow import FakeLLM, ask, count_retrieves, out, state_with
from tests.tutor.test_oom_gate import ABSTAIN, RoleLLM, _ids
from tests.tutor.test_oom_gate import run as run_flow
from tutor.judge import oom_gate
from tutor.retrieval import default_index, retrieve
from tutor.retrieval import embedding as emb

ON = {"toggles.english_embedding": True, "toggles.oom_gate": "named_term"}
EN_Q = "how many innings does the scoreboard example record"  # 영어 발화, KB에 없는 이름은 없다


VECTORS = {"A-01": [0, 0, 1.0], "A-02": [0, 1.0, 0], "B-01": [1.0, 0, 0]}


def tiny_search(fail=None):
    """질의 벡터는 B-01 > A-02 > A-01 순으로 가깝다. BM25는 "apple basket"에 A-01을 1위로 준다."""
    index = small_index()
    ids = tuple(sid for sid, _text in index.chunks)

    async def embed(texts):
        if fail is not None:
            raise fail
        return [[1.0, 0.5, 0.1] for _ in texts]

    matrix = np.array([VECTORS[sid] for sid in ids])
    return index, emb.EmbeddingSearch(embed, ids, matrix, 1.0)


def test_english_query_uses_only_the_embedding_ranking_when_asked():
    index, search = tiny_search()
    bm25 = run(retrieve("apple basket", index=index, k=3))
    assert bm25[0].id == "A-01"  # BM25 1위는 임베딩 1위와 다르다
    mixed = run(retrieve("apple basket", index=index, k=3, embedding=search))
    assert [s.id for s in mixed] == emb.rrf(["A-01"], ["B-01", "A-02", "A-01"])  # 지금 방식(RRF)
    only = run(
        retrieve("apple basket", index=index, k=3, embedding=search, english_only_embedding=True)
    )
    assert [s.id for s in only] == ["B-01", "A-02", "A-01"]


def test_embedding_only_keeps_the_subject_filter():
    index, search = tiny_search()
    found = run(
        retrieve(
            "apple basket",
            index=index,
            subject="a",
            embedding=search,
            english_only_embedding=True,
        )
    )
    assert [s.id for s in found] == ["A-02", "A-01"]


def test_embedding_only_falls_back_to_bm25_when_the_embedding_fails():
    index, search = tiny_search(fail=RuntimeError("본문"))
    found = run(
        retrieve("apple basket", index=index, k=3, embedding=search, english_only_embedding=True)
    )
    plain = run(retrieve("apple basket", index=index, k=3))
    assert [s.id for s in found] == [s.id for s in plain]  # 임베딩을 못 써서 BM25 순위 그대로


def test_korean_query_ignores_the_flag_and_never_calls_the_embedding():
    fake = FakeEmbed()
    built = run(emb.EmbeddingSearch.build(default_index(), FakeEmbed(), timeout_s=1.0))
    search = dataclasses.replace(built, embed=fake)
    query = "체크카드 사용 규칙이 뭐예요"
    plain = run(retrieve(query, k=5))
    flagged = run(retrieve(query, k=5, embedding=search, english_only_embedding=True))
    assert [s.id for s in flagged] == [s.id for s in plain] and fake.calls == []


# --- 흐름: 근거 묶음·E·규칙 A ---


def fake_search(top_of, calls=None):
    """섹션마다 한 방향(단위 행렬). 질의 임베딩은 top_of(글)이 가리키는 섹션 쪽이다."""
    ids = tuple(default_index().sections)
    index = {sid: i for i, sid in enumerate(ids)}

    async def embed(texts):
        if calls is not None:
            calls.append(list(texts))
        vectors = []
        for text in texts:
            vec = np.full(len(ids), 0.01)
            vec[index[top_of(text)]] = 1.0
            vectors.append(vec.tolist())
        return vectors

    return emb.EmbeddingSearch(embed, ids, np.eye(len(ids)), 1.0)


def with_embedding(state, search):
    state.tutor_emb = search
    return state


def test_english_utterance_calls_the_gate_with_the_embedding_bundle_and_named_term_mode():
    calls = []
    llm = RoleLLM(out(), found=False)
    search = fake_search(lambda text: "PY-01-01", calls)
    state = with_embedding(state_with(llm, **ON), search)
    assert run_flow(state, EN_Q) == ABSTAIN  # E가 found=false면 자료 밖 고정 문장
    assert llm.roles[0] == oom_gate.ROLE
    assert state.oom_gate_counts == {"english_called": 1, "absent": 1}
    gate_user = llm.kwargs[oom_gate.ROLE]["messages"][1]["content"]
    main_user = llm.kwargs["main"]["messages"][-1]["content"]
    assert _ids(gate_user)[0] == "PY-01-01"  # BM25(빈 결과)가 아니라 임베딩 순위
    assert _ids(main_user)[0] == "PY-01-01" and _ids(main_user) == _ids(gate_user)
    # 질의 임베딩은 검색마다 새로 부른다(캐시 금지): E 묶음 + 주 호출 + 규칙 A 재검색
    assert len(calls) == 3


def test_english_utterance_keeps_the_answer_when_the_gate_finds_it():
    llm = RoleLLM(out(), found=True)
    state = with_embedding(state_with(llm, **ON), fake_search(lambda text: "PY-01-01"))
    resp = run_flow(state, EN_Q)
    assert resp["action"] == "answer" and resp["citations"][0] == "PY-01-01"
    assert state.oom_gate_counts == {"english_called": 1, "found": 1}


def test_english_utterance_without_a_built_embedding_skips_the_gate():
    llm = RoleLLM(out(), found=False)
    state = state_with(llm, **ON)  # tutor_emb 없음(기동 실패·미생성)
    assert run_flow(state, EN_Q)["action"] == "answer"
    assert oom_gate.ROLE not in llm.roles and state.oom_gate_counts == {"english": 1}


def test_english_utterance_skips_the_gate_when_the_toggle_is_off():
    llm = RoleLLM(out(), found=False)
    state = with_embedding(
        state_with(llm, **{"toggles.oom_gate": "named_term"}), fake_search(lambda text: "PY-01-01")
    )
    assert run_flow(state, EN_Q)["action"] == "answer"
    assert oom_gate.ROLE not in llm.roles and state.oom_gate_counts == {"english": 1}


def test_korean_utterance_never_calls_the_embedding():
    calls = []
    llm = RoleLLM(out(), found=False)
    state = with_embedding(state_with(llm, **ON), fake_search(lambda text: "PY-01-01", calls))
    korean = "꿈틀 중학교 동아리 야구 점수판 예제는 몇 회까지 점수를 기록해?"
    assert run_flow(state, korean)["action"] == "answer"
    assert calls == [] and oom_gate.ROLE not in llm.roles  # named_term: 이름 붙은 말이 없다
    assert state.oom_gate_counts == {"no_term": 1}


DIALOG = [
    "how do loops work in this course",
    "a zeta loop repeats the same steps again and again",  # 튜터 발화에만 표지 낱말이 있다
    "what is that exactly",
]
ROLES = ["user", "assistant", "user"]


def test_rule_a_search_uses_the_embedding_for_english(monkeypatch):
    calls = count_retrieves(monkeypatch)
    search = fake_search(lambda text: "PY-03-04" if "zeta" in text else "PY-01-01")
    state = with_embedding(
        state_with(
            FakeLLM(out()),
            **{
                "toggles.english_embedding": True,
                "toggles.cite_top1": True,
                "toggles.oom_gate": "named_term",
            },
            **{"toggles.top1_dialog_query": True},
        ),
        search,
    )
    resp = ask(state, *DIALOG, roles=ROLES)
    # 주 호출 묶음의 1위는 PY-01-01, 규칙 A(튜터 발화 포함)의 임베딩 1위는 PY-03-04
    assert resp["citations"] == ["PY-01-01", "PY-03-04"]
    _gate, main, extra = calls  # E 근거 묶음, 주 호출 묶음, 규칙 A 재검색
    assert main[2]["embedding"] is search and main[2]["english_only_embedding"] is True
    assert extra[2]["embedding"] is search and extra[2]["english_only_embedding"] is True


def test_toggle_off_passes_no_english_only_flag(monkeypatch):
    calls = count_retrieves(monkeypatch)
    state = with_embedding(
        state_with(FakeLLM(out()), **{"toggles.embedding": True}),
        fake_search(lambda text: "PY-01-01"),
    )
    ask(state, *DIALOG, roles=ROLES)
    assert all("english_only_embedding" not in kw for _q, _h, kw in calls)
    assert calls[0][2]["embedding"] is state.tutor_emb  # 기존 RRF 경로는 그대로
