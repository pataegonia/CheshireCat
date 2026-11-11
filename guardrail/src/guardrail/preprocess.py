"""GR-02 전처리 v6: 정규화본과 숨긴 문자 드러내기 (ADR-0001).

- normalize: LLM에 보내는 형태. 보이지 않는 문자 제거, 전각·반각 접기, 자모 조합만 한다.
  위첨자·분수 같은 수식 기호는 그대로 둔다(NFKC는 쓰지 않는다).
- reveal_hidden: 답변의 숨긴 문자를 눈에 보이게 바꾼다(태그 문자 → ASCII, 나머지 → <U+XXXX>).
- decode_tags / strip_tags: 학습자 발화의 태그 문자(U+E0000–E007F) 처리.

호환 자모(ㄱ-ㅣ) 연속 구간은 직접 조합한다. NFKC는 앞 음절과 뒤따르는 자음을 구분하지 못해
"야ㅅㅂ" 같은 초성 약어까지 받침으로 붙이기 때문이다.
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
_TAG_CHARS = re.compile("[\U000e0000-\U000e007f]")
# 깃발 이모지: 검은 깃발 + 영국 세 지역 코드 태그 + 취소 태그. 다른 태그열은 숨긴 문장.
_FLAG = re.compile(
    "\U0001f3f4(?:"
    + "|".join("".join(chr(0xE0000 + ord(c)) for c in code) for code in ("gbeng", "gbsct", "gbwls"))
    + ")\U000e007f"
)
_VS = "\ufe0e\ufe0f"
_KEYCAP = "\u20e3"

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


def _prepare(text: str) -> str:
    s = "".join(_fold_width(ch) for ch in text if _visible(ch))
    # NFC는 완전한 조합형 음절(NFD 입력)만 합치고, 낱자모는 남겨 아래에서 호환 자모로 바꾼다
    return unicodedata.normalize("NFC", s).translate(_CONJOINING_TO_COMPAT)


def _compose_run(run: str) -> str:
    """호환 자모 연속 구간을 두벌식 입력기처럼 음절로 조합한다. 모음이 없는 구간은 그대로 둔다."""
    out: list[str] = []
    i, n = 0, len(run)
    while i < n:
        c = run[i]
        v = run[i + 1] if i + 1 < n else ""
        # 이모티콘: 조합되지 않고 남은 같은 자음 뒤(ㅋㅋㅠ), ㅠ·ㅜ 반복(ㅋㅠㅠ)은 조합하지 않는다.
        # 앞 자음이 받침으로 들어갔으면(ㅎㅏㄱㄱㅛ) out[-1]은 음절이라 해당하지 않는다.
        emoticon = (out and out[-1] == c) or (
            v in _EMOTICON_VOWELS and i + 2 < n and run[i + 2] == v
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


def _compose_all(text: str) -> str:
    return _JAMO_RUN.sub(lambda m: _compose_run(m.group()), text)


def normalize(text: str) -> str:
    """LLM에 보내는 형태. 보이지 않는 문자 제거 → 전각·반각 접기 → 자모 조합."""
    return unicodedata.normalize("NFC", _compose_all(_prepare(text)))


def strip_tags(text: str) -> str:
    """태그 문자 U+E0000–E007F만 지운다. 자모 조합 등 다른 정규화는 하지 않는다."""
    return _TAG_CHARS.sub("", text)


def decode_tags(text: str) -> tuple[str, str]:
    """학습자 발화의 태그 문자를 푼다. (태그를 지운 정규화본 [+ 숨긴 문자 표시], 푼 문자열).

    깃발 이모지의 태그는 풀지 않는다. 태그가 없으면 푼 문자열은 빈 문자열이다.
    """
    decoded = "".join(
        chr(ord(c) - 0xE0000) for c in _FLAG.sub("", text) if 0xE0020 <= ord(c) <= 0xE007E
    )
    view = normalize(strip_tags(text))
    return (f"{view} [숨긴 문자: {decoded}]" if decoded else view), decoded


def _is_emoji(ch: str) -> bool:
    # 라틴-1 기호·화살표(©, →)는 제외: 이모지 그림 문자(1F000대)와 기타 기호·딩뱃(2600–27BF)만
    if "☀" <= ch <= "➿" or "🀀" <= ch <= "🫿":
        return True
    return ch in _VS


def _in_emoji_sequence(text: str, i: int) -> bool:
    """ZWJ는 이모지 사이에서, 변형 선택자는 이모지·키캡 밑글자 뒤에서만 정상으로 본다."""
    prev, nxt = text[i - 1 : i] if i else "", text[i + 1 : i + 2]
    if text[i] == "\u200d":
        return bool(prev and nxt) and _is_emoji(prev) and _is_emoji(nxt)
    if text[i] in _VS:
        return unicodedata.category(prev or " ") == "So" or (
            bool(prev) and prev in "#*0123456789" and nxt == _KEYCAP
        )
    return False


def reveal_hidden(answer: str) -> str:
    """답변의 숨긴 문자를 드러낸다. 태그 문자(U+E0020–E007E)는 ASCII로, 나머지 폭 없는 문자는
    `<U+200B>` 형식으로 바꾼다. 이모지 시퀀스의 ZWJ·변형 선택자, 깃발 태그, 맨 앞 BOM은 둔다."""
    flags = {m.start(): m.end() for m in _FLAG.finditer(answer)}
    out: list[str] = []
    i, n = 0, len(answer)
    while i < n:
        if i in flags:
            out.append(answer[i : flags[i]])
            i = flags[i]
            continue
        ch = answer[i]
        cp = ord(ch)
        if 0xE0020 <= cp <= 0xE007E:
            out.append(chr(cp - 0xE0000))
        elif _visible(ch) or (i == 0 and cp == 0xFEFF) or _in_emoji_sequence(answer, i):
            out.append(ch)
        else:
            out.append(f"<U+{cp:04X}>")
        i += 1
    return "".join(out)
