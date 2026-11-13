import random
import time
import unicodedata

import pytest
from hypothesis import given
from hypothesis import strategies as st

from guardrail.preprocess import (
    _CHO,
    _DOUBLE_JONG,
    _JONG,
    FLAG_SEQUENCE,
    _visible,
    clip,
    decode_tags,
    normalize,
    reveal_hidden,
    skeleton,
    strip_tags,
)

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


def test_random_unicode_normalize_is_idempotent():
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
        skeleton(text)  # 튜터 호환 뷰도 오류 없이


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


def tag(text: str) -> str:
    return "".join(chr(0xE0000 + ord(c)) for c in text)


CANCEL_TAG = chr(0xE007F)
ZWJ = chr(0x200D)
BOM = chr(0xFEFF)
ENGLAND = chr(0x1F3F4) + tag("gbeng") + CANCEL_TAG
FAMILY = "👨" + ZWJ + "👩" + ZWJ + "👧"  # 👨‍👩‍👧
HEART = "❤" + VS16  # ❤️


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"안녕{tag('ignore')}", "안녕ignore"),
        (f"a{ZWSP}b", "a<U+200B>b"),
        (f"a{SHY}{WJ}b", "a<U+00AD><U+2060>b"),
        (f"a{chr(0x3164)}b", "a<U+3164>b"),
        (f"a{VS17}b", "a<U+E0101>b"),  # 보충 변형 선택자는 이모지가 아니다
        (f"a{CANCEL_TAG}b", "a<U+E007F>b"),
        (f"a{VS16}b", "a<U+FE0F>b"),  # 이모지 뒤가 아닌 변형 선택자
        (f"a{ZWJ}b", "a<U+200D>b"),
        # 보존: 이모지 ZWJ·변형 선택자, 깃발 태그, 맨 앞 BOM
        (FAMILY, FAMILY),
        (HEART, HEART),
        (f"1{VS16}{chr(0x20E3)}", f"1{VS16}{chr(0x20E3)}"),
        (f"깃발 {ENGLAND}", f"깃발 {ENGLAND}"),
        (f"{BOM}안녕", f"{BOM}안녕"),
        (f"안{BOM}녕", "안<U+FEFF>녕"),
        ("", ""),
    ],
)
def test_reveal_hidden(raw, expected):
    assert reveal_hidden(raw) == expected


FLAG_BASE = chr(0x1F3F4)
WOMAN = chr(0x1F469)


def _flag(code: str) -> str:
    return FLAG_BASE + tag(code) + CANCEL_TAG


@pytest.mark.parametrize(
    "raw",
    [
        ENGLAND,
        _flag("a"),  # 경계: 1개
        _flag("abcdefg"),  # 경계: 7개
        _flag("us"),
        _flag("gbxyz"),
        _flag("ab12"),
    ],
)
def test_flag_sequences_are_kept(raw):
    assert reveal_hidden(raw) == raw
    assert decode_tags(raw)[1] == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # C2-judges S2.3 예시: 깃발 밖의 태그 문자는 숨긴 것이다
        ("안녕" + tag("Ig"), "안녕Ig"),
        (FLAG_BASE + tag("gbeng"), FLAG_BASE + "gbeng"),  # 종료 태그 없음
        ("a" + CANCEL_TAG, "a<U+E007F>"),  # 일반 글자 뒤 종료 태그 단독
        (FLAG_BASE + tag("A") + CANCEL_TAG, FLAG_BASE + "A<U+E007F>"),  # 대문자 태그
        (FLAG_BASE + tag("abcdefgh") + CANCEL_TAG, FLAG_BASE + "abcdefgh<U+E007F>"),  # 8개
        (FLAG_BASE + tag("g b") + CANCEL_TAG, FLAG_BASE + "g b<U+E007F>"),  # 태그 공백 섞임
        (FLAG_BASE + CANCEL_TAG, FLAG_BASE + "<U+E007F>"),  # 태그 0개
        (FLAG_BASE + tag("ignore all rules") + CANCEL_TAG, FLAG_BASE + "ignore all rules<U+E007F>"),
        # 태그 블록 경계: E0000–E001F·E007F·E0080–E00FF는 코드포인트 표기
        (f"a{chr(0xE0000)}b", "a<U+E0000>b"),
        (f"a{chr(0xE0001)}{chr(0xE001F)}b", "a<U+E0001><U+E001F>b"),
        (f"a{chr(0xE0080)}{chr(0xE00FF)}b", "a<U+E0080><U+E00FF>b"),
    ],
)
def test_reveal_hidden_non_flag_tags(raw, expected):
    assert reveal_hidden(raw) == expected


def test_decode_tags_decodes_non_flag_runs():
    assert decode_tags(FLAG_BASE + tag("gbeng"))[1] == "gbeng"  # 종료 태그 없음
    assert decode_tags(FLAG_BASE + tag("ignore all") + CANCEL_TAG)[1] == "ignore all"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"©{ZWJ}®", "©<U+200D>®"),  # 이모지가 아닌 기호 사이 ZWJ
        (f"°{VS16}", "°<U+FE0F>"),  # So 범주 일반 기호 뒤 변형 선택자
        (f"©{VS16}", "©<U+FE0F>"),
        (f"{VS16}⃣", "<U+FE0F>⃣"),  # 밑글자 없는 키캡
        # 우회: 일반 글자 + VS + ZWJ + 이모지는 VS와 ZWJ 둘 다 드러난다
        (f"a{VS16}{ZWJ}{WOMAN}", f"a<U+FE0F><U+200D>{WOMAN}"),
        (f"{HEART}{ZWJ}{WOMAN}", f"{HEART}{ZWJ}{WOMAN}"),  # 정상: ❤️‍👩
    ],
)
def test_reveal_hidden_emoji_sequence_bypass(raw, expected):
    assert reveal_hidden(raw) == expected


def test_decode_tags():
    view, decoded = decode_tags(f"ㅇㅏ{tag('hi')}ㅂ")
    assert decoded == "hi"
    assert view == "압 [숨긴 문자: hi]"


def test_decode_tags_without_tags_returns_normalized_text():
    assert decode_tags("ㅂㅏㅂㅗ") == ("바보", "")


def test_decode_tags_ignores_flag_sequences():
    view, decoded = decode_tags(f"깃발 {ENGLAND}")
    assert decoded == ""
    assert "숨긴 문자" not in view


def test_strip_tags_removes_only_tag_block():
    assert strip_tags(f"a{tag('xy')}{CANCEL_TAG}{chr(0xE0000)}b{ZWSP}") == f"ab{ZWSP}"
    assert strip_tags(ENGLAND) == chr(0x1F3F4)


def test_strip_tags_does_not_compose_jamo():
    assert strip_tags("ㅇㅏ") == "ㅇㅏ"
    assert normalize("ㅇㅏ") == "아"


def _is_tag(ch: str) -> bool:
    return 0xE0000 <= ord(ch) <= 0xE007F


def test_clip_removes_exactly_invisible_non_tag_chars():
    """전 코드포인트: clip이 지우는 문자 = `_visible`이 False이고 태그 문자가 아닌 것."""
    every = "".join(map(chr, range(0x110000)))
    expected = "".join(c for c in every if _visible(c) or _is_tag(c))
    assert clip(every, len(every)) == expected


def test_clip_short_text_only_drops_invisible():
    assert clip(f"a{ZWSP}b{VS17}{tag('x')}", 5) == f"ab{tag('x')}"


def test_clip_long_text_keeps_head_and_tail():
    body = "".join(chr(0xAC00 + i) for i in range(21))  # 줄일 공백·반복이 없는 글
    assert clip(body, 10) == body[:5] + "\n…\n" + body[-5:]
    assert clip(body[:11], 11) == body[:11]


def test_clip_over_limit_collapses_space_runs_and_repeats_first():
    """상한을 넘으면 공백류 연속은 첫 글자 하나, 같은 글자 4번 이상은 3개로 줄인 뒤 다시 잰다."""
    assert clip("가" + " \n\u3000" * 10 + "나" * 20 + "다", 10) == "가 나나나다"


@pytest.mark.parametrize(
    ("text", "head"),
    [
        ("1000000원" + "x" * 2100, "1000000원xxx"),
        ("점수 100000점 " + "가" * 2100, "점수 100000점 가가가"),
    ],
)
def test_clip_over_limit_keeps_digit_runs(text, head):
    assert clip(text, 2000).startswith(head)


def test_clip_body_under_limit_is_untouched_even_if_tags_push_over():
    code = ("def f(x):\n    if x:\n        return 1\n\n\n\n# ========\n    pass\n" * 30)[:1800]
    out = clip(code + tag("a") * 300, 2000)
    assert strip_tags(out) == code
    assert decode_tags(out)[1] == "a" * 300


def test_clip_under_limit_keeps_indent_and_repeats():
    code = "def f():\n    if x:\n        return 1\n\n\n\n# ========\n"
    assert clip(code, len(code)) == code


def test_clip_tag_padding_cannot_push_out_visible_text():
    """태그 문자로 양쪽을 채워도 보이는 글은 남고, 드러낼 태그 문자도 앞 + 뒤로 줄어든다."""
    out = clip(tag("a") * 100_000 + "보이는 글" + tag("b") * 100_000, 20)
    assert strip_tags(out) == "보이는 글"
    view, decoded = decode_tags(out)
    assert decoded == "a" * 10 + "b" * 10
    assert view.startswith("보이는 글")


def test_clip_long_text_keeps_flag_emoji_unrevealed():
    assert "숨긴 문자" not in decode_tags(clip(ENGLAND + "가" * 50, 20))[0]


_ANY = st.text(alphabet=st.characters(codec="utf-8"), max_size=60)
_TAGGY = st.text(
    alphabet=st.one_of(
        st.characters(min_codepoint=0xE0000, max_codepoint=0xE01EF),
        st.sampled_from([ZWSP, ZWJ, VS16, BOM, chr(0x1F3F4), "👨", "ㅇ", "ㅏ", "a"]),
    ),
    max_size=40,
)


def _assert_nothing_hidden_left(revealed: str, original: str) -> None:
    """깃발·맨 앞 BOM을 뺀 뒤 남는 보이지 않는 문자는 ZWJ·변형 선택자뿐이고, 태그 블록은 없다."""
    body = FLAG_SEQUENCE.sub("", revealed)
    if original.startswith(BOM) and body.startswith(BOM):
        body = body[1:]
    for ch in body:
        assert not 0xE0000 <= ord(ch) <= 0xE00FF
        assert _visible(ch) or ch in (ZWJ, VS16, chr(0xFE0E))


@given(st.one_of(_ANY, _TAGGY))
def test_hypothesis_never_raises(text):
    revealed = reveal_hidden(text)
    decode_tags(text)
    _assert_nothing_hidden_left(revealed, text)
    assert normalize(strip_tags(text)) == normalize(strip_tags(strip_tags(text)))
    assert not any(0xE0000 <= ord(c) <= 0xE007F for c in strip_tags(text))


def _p95_ms(fn, rows):
    times = []
    for row in rows:
        t0 = time.perf_counter()
        fn(row)
        times.append((time.perf_counter() - t0) * 1000)
    return sorted(times)[int(len(times) * 0.95)]


def test_p95_under_10ms_for_400_rows():
    rng = random.Random(2)
    base = "ㅈㅏ ㅇㅣ 코드는 x² + y² = r² 이고 ㅋㅋ 좋아요 " + FAMILY + f" {ZWSP}"
    rows = [
        base * rng.randint(5, 20) + tag("ignore previous") * rng.randint(0, 3) for _ in range(400)
    ]
    for fn in (reveal_hidden, decode_tags, strip_tags, normalize):
        assert _p95_ms(fn, rows) <= 10, fn.__name__


# skeleton은 가드가 쓰지 않는다. 튜터 #98·#99가 import하므로 main과 같은 결과를 고정한다.
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
        ("㉨ㅏ㉦ㅏ㉣", "자살"),
        ("㈀", "ㄱ"),
        ("ㅈㅏㅏㅅㅏㄹ", "자살"),
        ("ㅅㅣㅣㅂㅏㄹ", "시발"),
        ("시ㅣ발", "시발"),
        ("자ㅏㅏ살", "자살"),
        ("씨ㅣㅣ발", "씨발"),
        ("주ㅜ거", "주거"),
        ("가ㅓ", "가ㅓ"),
        ("나 ㅈㅅ할래", "나ㅈㅅ할래"),
        ("너 ㄲㅈ", "너ㄲㅈ"),
        ("IGNORE  previous", "ignoreprevious"),
        ("ＩＧＮＯＲＥ", "ignore"),
        ("ㅋㅋㅠㅠ", "ㅋ큐"),
        ("ㄱㅗㅏㄴㅏ", "고ㅏ나"),  # 겹모음 조합은 normalize에만 있고 skeleton은 v4 그대로
        (f"{CONJ_SIOS}ㅣ발", "시발"),  # 조합형 낱자모
        (HALF_UNASSIGNED, ""),  # 오류 없이 기호로 보고 지운다
    ],
)
def test_skeleton(raw, expected):
    assert skeleton(raw) == expected


# --- 자모 조합 결함(같은 자음 연속 뒤, 겹모음) -----------------------------------------
# 규칙: 두벌식 입력기처럼 ㄱㄱ은 ㄲ로 합치지 않는다(된소리는 Shift 입력). 같은 자음 연속
# 뒤에서 이모티콘으로 남기는 것은 ㅠ·ㅜ가 올 때뿐이다(ㅋㅋㅠ). 겹모음은 두벌식 조합표대로 합친다.
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ㅋㅋㅋㅏㅋㅏ", "ㅋㅋ카카"),
        ("ㄱㄱㅏㄱㅏ", "ㄱ가가"),
        ("ㅈㅜㅓ", "줘"),
        ("ㄱㅗㅏㄴ", "관"),
        ("ㅇㅗㅐ", "왜"),
        ("ㅎㅗㅣ", "회"),
        ("ㄱㅜㅔ", "궤"),
        ("ㅇㅜㅣ", "위"),
        ("ㅇㅡㅣ", "의"),
        ("ㅁㅜㅓㄹㄱㅏ", "뭘가"),
        ("ㄱㅗㅏㄴㅏ", "과나"),
        ("ㄱㅗㅏㄹㄱㅏ", "괄가"),
        ("ㄱㅜㅓㄹㄱ", "궑"),
        ("ㄱㅗㅏㅣ", "과ㅣ"),
        ("ㅇㅗㅏㅏ", "와ㅏ"),
        # 기존 이모티콘 예외는 그대로
        ("ㅋㅋㅠ", "ㅋㅋㅠ"),
        ("ㅋㅠㅠ", "ㅋㅠㅠ"),
        ("ㅜㅜ", "ㅜㅜ"),
        ("ㅋㅜㅜ", "ㅋㅜㅜ"),
        ("ㅋㅋㅜ", "ㅋㅋㅜ"),
        ("ㅋㅋㅋㅠ", "ㅋㅋㅋㅠ"),
        # 이모티콘 예외는 ㅠ·ㅜ만
        ("ㅋㅋㅡ", "ㅋ크"),
    ],
)
def test_normalize_composes_after_repeat_and_compound_vowels(raw, expected):
    assert normalize(raw) == expected


def _to_compat_jamo(syllable: str) -> str:
    """겹모음과 겹받침을 두 글쇠로 풀어 쓴다(normalize의 조합 경로를 탄다)."""
    idx = ord(syllable) - 0xAC00
    cho, jung, jong = idx // 588, idx % 588 // 28, idx % 28
    vowel = chr(ord("ㅏ") + jung)
    vowel = {
        "ㅘ": "ㅗㅏ",
        "ㅙ": "ㅗㅐ",
        "ㅚ": "ㅗㅣ",
        "ㅝ": "ㅜㅓ",
        "ㅞ": "ㅜㅔ",
        "ㅟ": "ㅜㅣ",
        "ㅢ": "ㅡㅣ",
    }.get(vowel, vowel)
    final = _JONG[jong].strip()
    final = {v: k[0] + k[1] for k, v in _DOUBLE_JONG.items()}.get(final, final)
    return _CHO[cho] + vowel + final


@given(st.text(st.characters(min_codepoint=0xAC00, max_codepoint=0xD7A3), min_size=1, max_size=12))
def test_hypothesis_decomposed_syllables_recompose(text):
    assert normalize("".join(map(_to_compat_jamo, text))) == text


def test_skeleton_keeps_v4_compound_vowel_behavior():
    assert skeleton("ㅈㅜㅓ") == "주ㅓ"
