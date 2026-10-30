import pytest

from guardrail.preprocess import normalize, skeleton


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ㅂㅏㅂㅗ", "바보"),  # 자모 분리
        ("ㄱㅏㄱ", "각"),  # 받침 결합
        ("ㅅㅂ", "ㅅㅂ"),  # 초성만은 그대로
        ("바​보️", "바보"),  # 제로폭·변형 선택자
        ("ｓｉｂａｌ", "sibal"),  # 전각
        ("학교", "학교"),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("시1발", "시발"),
        ("바 . 보", "바보"),
        ("ㅂ ㅏ ㅂ ㅗ", "바보"),
        ("IGNORE  previous", "ignoreprevious"),
    ],
)
def test_skeleton(raw, expected):
    assert skeleton(raw) == expected


def test_normalize_keeps_math_and_code():
    assert normalize("x = 3.14 * r**2  # 원의 넓이") == "x = 3.14 * r**2  # 원의 넓이"
