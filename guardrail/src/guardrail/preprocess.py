"""GR-01 전처리: 정규화본과 매칭 전용 골격 뷰 (ADR-0001).

- normalize: LLM에 보내는 형태. 보이지 않는 문자 제거, 전각·반각 접기, 자모 조합만 한다.
  위첨자·분수 같은 수식 기호는 그대로 둔다.
- skeleton: 어휘 사전 매칭 전용. 호환 자모 구간 밖에 NFKC를 적용하고 공백·기호·숫자를 지운 뒤
  조합한다.

호환 자모(ㄱ-ㅣ) 연속 구간은 NFKC에 맡기지 않고 직접 조합한다. NFKC는 앞 음절과 뒤따르는 자음을
구분하지 못해 "야ㅅㅂ" 같은 초성 약어까지 받침으로 붙이기 때문이다.
"""

import re
import unicodedata

# 서식 문자(Cf) 외에 지울 것: 결합 문자 접착자, 한글 채움 문자, 몽골 FVS, 변형 선택자
_INVISIBLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x180B, 0x180D),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0xE0100, 0xE01EF),
)
_JAMO_RUN = re.compile("[ㄱ-ㅣ]+")
_FILLER = re.compile(r"[\s\W\d_]+")
_REPEATED_VOWEL = re.compile("([ㅏ-ㅣ])\\1+")

_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_JONG = " ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"  # 0번은 받침 없음
_DOUBLE_JONG = {
    ("ㄱ", "ㅅ"): "ㄳ", ("ㄴ", "ㅈ"): "ㄵ", ("ㄴ", "ㅎ"): "ㄶ", ("ㄹ", "ㄱ"): "ㄺ",
    ("ㄹ", "ㅁ"): "ㄻ", ("ㄹ", "ㅂ"): "ㄼ", ("ㄹ", "ㅅ"): "ㄽ", ("ㄹ", "ㅌ"): "ㄾ",
    ("ㄹ", "ㅍ"): "ㄿ", ("ㄹ", "ㅎ"): "ㅀ", ("ㅂ", "ㅅ"): "ㅄ",
}  # fmt: skip
_EMOTICON_VOWELS = "ㅠㅜ"

# 조합형 낱자모(원문자 ㉨ → NFKC → U+110C 등)를 호환 자모로 바꾼다
_CONJOINING_TO_COMPAT = str.maketrans(
    {0x1100 + i: ch for i, ch in enumerate(_CHO)}
    | {0x1161 + i: chr(ord("ㅏ") + i) for i in range(21)}
    | {0x11A8 + i: ch for i, ch in enumerate(_JONG[1:])}
)


def _is_vowel(ch: str) -> bool:
    return "ㅏ" <= ch <= "ㅣ"


def _visible(ch: str) -> bool:
    cp = ord(ch)
    if unicodedata.category(ch) == "Cf":
        return False
    return not any(lo <= cp <= hi for lo, hi in _INVISIBLE_RANGES)


def _fold_width(ch: str) -> str:
    """전각·반각 문자만 기본 문자로 접는다(ｓ→s, 반각 자모 → 호환 자모). 미할당 문자는 그대로."""
    decomposition = unicodedata.decomposition(ch)
    if decomposition.startswith(("<wide>", "<narrow>")):
        return chr(int(decomposition.split()[1], 16))
    return ch


def _nfkc_outside_jamo(text: str) -> str:
    """호환 자모 구간은 보존하고 나머지에만 NFKC를 적용한다."""
    parts: list[str] = []
    pos = 0
    for m in _JAMO_RUN.finditer(text):
        parts.append(unicodedata.normalize("NFKC", text[pos : m.start()]))
        parts.append(m.group())
        pos = m.end()
    parts.append(unicodedata.normalize("NFKC", text[pos:]))
    return "".join(parts)


def _prepare(text: str, full_nfkc: bool) -> str:
    s = "".join(_fold_width(ch) for ch in text if _visible(ch))
    # NFC는 완전한 조합형 음절(NFD 입력)만 합치고, 낱자모는 남겨 아래에서 호환 자모로 바꾼다
    s = _nfkc_outside_jamo(s) if full_nfkc else unicodedata.normalize("NFC", s)
    return s.translate(_CONJOINING_TO_COMPAT)


def _compose_run(run: str, keep_emoticons: bool) -> str:
    """호환 자모 연속 구간을 두벌식 입력기처럼 음절로 조합한다. 모음이 없는 구간은 그대로 둔다."""
    out: list[str] = []
    i, n = 0, len(run)
    while i < n:
        c = run[i]
        v = run[i + 1] if i + 1 < n else ""
        # 이모티콘: 조합되지 않고 남은 같은 자음 뒤(ㅋㅋㅠ), ㅠ·ㅜ 반복(ㅋㅠㅠ)은 조합하지 않는다.
        # 앞 자음이 받침으로 들어갔으면(ㅎㅏㄱㄱㅛ) out[-1]은 음절이라 해당하지 않는다.
        emoticon = keep_emoticons and (
            (out and out[-1] == c) or (v in _EMOTICON_VOWELS and i + 2 < n and run[i + 2] == v)
        )
        if c in _CHO and _is_vowel(v) and not emoticon:
            jong, i = 0, i + 2
            f = run[i] if i < n else ""
            nxt = run[i + 1] if i + 1 < n else ""
            if f and f in _JONG[1:] and not _is_vowel(nxt):
                pair = _DOUBLE_JONG.get((f, nxt))
                after = run[i + 2] if i + 2 < n else ""
                if pair and not _is_vowel(after):
                    jong, i = _JONG.index(pair), i + 2
                else:
                    jong, i = _JONG.index(f), i + 1
            cho, jung = _CHO.index(c), ord(v) - ord("ㅏ")
            out.append(chr(0xAC00 + (cho * 21 + jung) * 28 + jong))
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _compose_all(text: str, keep_emoticons: bool) -> str:
    return _JAMO_RUN.sub(lambda m: _compose_run(m.group(), keep_emoticons), text)


def normalize(text: str) -> str:
    """LLM에 보내는 형태. 보이지 않는 문자 제거 → 전각·반각 접기 → 자모 조합."""
    composed = _compose_all(_prepare(text, full_nfkc=False), keep_emoticons=True)
    return unicodedata.normalize("NFC", composed)


def skeleton(text: str) -> str:
    """어휘 사전 매칭 전용 뷰. 재현율 우선이라 이모티콘 예외 없이 조합한다.

    NFKC(①·²·𝟏 → 숫자) → 공백·기호·숫자 제거 → 같은 모음 반복 축약 → 자모 조합 → 소문자.
    라틴 문자는 남긴다(영어 공격 문구 매칭). 지운 구분자를 넘는 매칭 규칙은 ADR-0001 참고.
    """
    s = _FILLER.sub("", _prepare(text, full_nfkc=True))
    s = _compose_all(_REPEATED_VOWEL.sub(r"\1", s), keep_emoticons=False)
    return unicodedata.normalize("NFC", s).casefold()
