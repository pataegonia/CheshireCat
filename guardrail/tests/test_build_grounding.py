"""S1.1 준비: 하위 분할의 결정성, 같은 씨앗은 한 하위 분할, 인용 바꿔치기 규칙, CLI 사전 확인."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from guardrail.eval.build_grounding import assemble, main, subsplit, swap_citation
from guardrail.eval.datasets import Item


def _g(id_, seed, answer, subtype=None, violation=False):
    return Item.model_validate(
        dict(id=id_, seed_id=seed, split="dev", kind="grounding", category="S0", technique="none",
             subtype=subtype, turns=[{"role": "student", "content": f"질문 {seed}"}],
             chunks=[{"sid": "S1", "source": "a", "text": "x"},
                     {"sid": "S2", "source": "b", "text": "y"}],
             answer=answer, expected={"violation": violation}, source="self", license="CC-BY-4.0")
    )  # fmt: skip


def test_subsplit_fixed_values_and_ratio():
    assert subsplit("grd-0001") == subsplit("grd-0001")
    share = sum(subsplit(f"grd-{i:04d}") == "calibration" for i in range(3000)) / 3000
    assert 0.57 < share < 0.63  # R7 §S7.7 보정 60 / 보고 40


@given(st.text(min_size=1, max_size=12))
def test_same_seed_lands_in_one_subsplit(seed):
    items = [_g("n", seed, "정상 [S1]"), _g("t2", seed, "주입 [S1]", "T2", True),
             _g("c", seed, "인용 [S2]", violation=True)]  # fmt: skip
    kept, _ = assemble(items)
    assert len({i.subsplit for i in kept}) == 1 and {i.split for i in kept} == {"dev"}


def test_assemble_keeps_normal_injected_and_cite_of_same_question():
    seed = "grd-0042"
    items = [
        _g("n", seed, "정상 [S1]"),
        _g("t", seed, "주입 [S1]", "T1", True),
        _g("n2", seed, "정상 [S1]"),
    ]
    kept, stats = assemble(items)
    assert [i.id for i in kept] == ["n", "t"] and stats["removed_duplicates"] == 1
    assert stats["by_subtype"] == {"normal": 1, "T1": 1}


@pytest.mark.parametrize("key", ["a", "b", "c", "d", "e"])
def test_swap_uses_only_other_sids_in_item_and_is_deterministic(key):
    answer = "물은 0°C에서 언다 [S1]. 밀도는 질량÷부피다 [S2]."
    first = swap_citation(answer, ["S1", "S2", "S3"], key)
    assert first == swap_citation(answer, ["S1", "S2", "S3"], key)
    new_answer, old, new = first
    assert new != old and new in {"S1", "S2", "S3"}
    assert new_answer.count("[") == 2 and f"[{new}]" in new_answer


def test_swap_missing_sid_and_errors():
    assert swap_citation("답 [S1]", ["S1"], "k", missing=True) == ("답 [S9]", "S1", "S9")
    with pytest.raises(ValueError, match="인용"):
        swap_citation("인용 없음", ["S1", "S2"], "k")
    with pytest.raises(ValueError, match="다른 sid"):
        swap_citation("답 [S1]", ["S1"], "k")


def test_api_subcommands_need_max_calls(capsys):
    assert main(["answers"]) == 2
    assert "--max-calls" in capsys.readouterr().err
    assert main(["inject", "--estimate"]) == 2  # 본체는 GP1
    assert main(["assemble"]) == 2
