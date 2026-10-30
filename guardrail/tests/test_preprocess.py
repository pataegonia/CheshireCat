import random
import unicodedata

import pytest

from guardrail.preprocess import _CHO, _DOUBLE_JONG, _JONG, normalize, skeleton

# 보이지 않는 문자는 소스에서 읽히도록 코드포인트로 만든다
ZWSP, SHY, WJ, VS16, FVS1, VS17 = map(chr, (0x200B, 0x00AD, 0x2060, 0xFE0F, 0x180B, 0xE0101))
HALF_BIEUP, HALF_A, HALF_O, HALF_UNASSIGNED = map(chr, (0xFFB2, 0xFFC2, 0xFFCC, 0xFFBF))
CONJ_SIOS = chr(0x1109)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 자모 분리
        ("ㅂㅏㅂㅗ", "바보"),
        ("ㄱㅏㄱ", "각"),
        ("ㅎㅏㄴㄱㅡㄹ", "한글"),
        ("ㄱㅏㄹㄱㅏ", "갈가"),
        # 받침과 다음 초성이 같은 자음
        ("ㅎㅏㄱㄱㅛ", "학교"),
        ("ㅇㅏㄴㄴㅕㅇ", "안녕"),
        ("ㄱㅏㄱㄱㅏ", "각가"),
        ("ㅈㅜㄱㄱㅗ ㅅㅣㅍㅇㅓ", "죽고 싶어"),
        ("ㅁㅗㄹㄹㅏ", "몰라"),
        # 겹받침을 둘로 나눠 쓴 경우
        ("ㄷㅏㄹㄱ", "닭"),
        ("ㅇㅓㅂㅅㄷㅏ", "없다"),
        ("ㄷㅏㄺ", "닭"),
        # 이미 완성된 음절 뒤의 초성은 받침으로 붙이지 않는다
        ("야ㅅㅂ", "야ㅅㅂ"),
        ("좋아ㅋㅋ", "좋아ㅋㅋ"),
        ("나ㅇㅇ", "나ㅇㅇ"),
        ("감사ㄳ", "감사ㄳ"),
        ("바ㅂㅗ", "바보"),
        # 초성만, 모음만, 겹자모 단독, 이모티콘은 그대로
        ("ㅅㅂ", "ㅅㅂ"),
        ("ㅠㅠ", "ㅠㅠ"),
        ("ㅗ", "ㅗ"),
        ("ㄳ", "ㄳ"),
        ("ㅋㅋㅋㅠㅠ", "ㅋㅋㅋㅠㅠ"),
        ("ㅋㅋㅠ", "ㅋㅋㅠ"),
        ("ㅎㅎㅜ", "ㅎㅎㅜ"),
        ("ㅋㅠㅠ", "ㅋㅠㅠ"),
        # 보이지 않는 문자
        (f"바{ZWSP}보{VS16}", "바보"),
        (f"바{SHY}보{WJ}", "바보"),
        (f"시{FVS1}발{VS17}", "시발"),
        # 전각·반각, 미할당 반각 코드포인트는 오류 없이 유지
        ("ｓｉｂａｌ", "sibal"),
        (HALF_BIEUP + HALF_A + HALF_BIEUP + HALF_O, "바보"),
        (f"가{HALF_UNASSIGNED}나", f"가{HALF_UNASSIGNED}나"),
        # 조합형 낱자모와 호환 모음이 섞인 경우, NFD 입력
        (f"{CONJ_SIOS}ㅣ발", "시발"),
        (unicodedata.normalize("NFD", "학교 닭 갔어"), "학교 닭 갔어"),
        ("", ""),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("시1발", "시발"),
        ("시①발", "시발"),
        ("시⑴발", "시발"),
        ("씨¹발", "씨발"),
        ("시𝟏발", "시발"),
        ("ㅈㅏ²ㅅㅏㄹ", "자살"),
        ("바 . 보", "바보"),
        ("ㅂ ㅏ ㅂ ㅗ", "바보"),
        ("ㅈ ㅏ ㅅ ㅏ ㄹ", "자살"),
        # 원문자 자모, 조합형 낱자모
        ("㉨ㅏ㉦ㅏ㉣", "자살"),
        (f"{CONJ_SIOS}ㅣ발", "시발"),
        ("㈀", "ㄱ"),
        # 같은 모음을 겹쳐 쓴 우회
        ("ㅈㅏㅏㅅㅏㄹ", "자살"),
        ("ㅅㅣㅣㅂㅏㄹ", "시발"),
        # 완성 음절 뒤에 같은 모음을 늘여 쓴 우회(실제 입력기로 나오는 형태)
        ("시ㅣ발", "시발"),
        ("자ㅏㅏ살", "자살"),
        ("씨ㅣㅣ발", "씨발"),
        ("주ㅜ거", "주거"),
        ("가ㅓ", "가ㅓ"),  # 중성이 다르면 그대로
        # 앞 단어와 뒤 초성이 공백 제거 후 합쳐지지 않는다
        ("나 ㅈㅅ할래", "나ㅈㅅ할래"),
        ("너 ㄲㅈ", "너ㄲㅈ"),
        ("IGNORE  previous", "ignoreprevious"),
        ("ＩＧＮＯＲＥ", "ignore"),
        (HALF_UNASSIGNED, ""),  # 오류 없이 기호로 보고 지운다
    ],
)
def test_skeleton(raw, expected):
    assert skeleton(raw) == expected


@pytest.mark.parametrize(
    "text",
    [
        "x = 3.14 * r**2  # 원의 넓이",
        "for i in range(10): print(i)",
        "2³=8 이고 x²+y²=r²",
        "½ + ¼, x₁ + x₂, 넓이는 3㎡",
        "좋아ㅋㅋ 이해했어요ㅠㅠ",
    ],
)
def test_normalize_keeps_normal_text(text):
    assert normalize(text) == text


def test_random_unicode_never_crashes_and_normalize_is_idempotent():
    rng = random.Random(0)
    pools = [
        range(0x20, 0x3000),
        range(0x3131, 0x3164),
        range(0xFFA0, 0xFFDD),
        range(0x1100, 0x1200),
        range(0x3200, 0x3280),  # 원문자 자모
        range(0x1D400, 0x1D800),  # 수학 영숫자
        range(0xE0000, 0xE0200),  # 태그·변형 선택자 보충
    ]
    for _ in range(3000):
        text = "".join(chr(rng.choice(rng.choice(pools))) for _ in range(rng.randint(0, 12)))
        once = normalize(text)
        assert normalize(once) == once
        skeleton(text)


def _to_jamo(syllable: str) -> str:
    """완성 음절을 호환 자모로 풀어 쓴다(겹받침도 두 글자로)."""
    code = ord(syllable) - 0xAC00
    cho, jung, jong = code // 588, code % 588 // 28, code % 28
    double = {v: k[0] + k[1] for k, v in _DOUBLE_JONG.items()}
    final = _JONG[jong].strip()
    return _CHO[cho] + chr(ord("ㅏ") + jung) + double.get(final, final)


def test_split_jamo_round_trip_on_random_syllables():
    rng = random.Random(1)
    for _ in range(3000):
        word = "".join(chr(0xAC00 + rng.randrange(11172)) for _ in range(rng.randint(1, 4)))
        split = "".join(_to_jamo(s) for s in word)
        assert normalize(split) == word
        assert skeleton(split) == skeleton(word)
