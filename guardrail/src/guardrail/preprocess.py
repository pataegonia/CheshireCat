"""GR-02 전처리 v6: 정규화본과 숨긴 문자 드러내기 (ADR-0001).

- normalize: LLM에 보내는 형태. 보이지 않는 문자 제거, 전각·반각 접기, 자모 조합만 한다.
  위첨자·분수 같은 수식 기호는 그대로 둔다(NFKC는 쓰지 않는다).
- reveal_hidden: 답변의 숨긴 문자를 눈에 보이게 바꾼다(태그 문자 → ASCII, 나머지 → <U+XXXX>).
- decode_tags / strip_tags: 학습자 발화의 태그 문자(U+E0000–E007F) 처리.
- skeleton: v4 어휘 사전 매칭 뷰. 가드는 쓰지 않고 튜터(#98·#99)가 import해서 남겨 둔다.

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
_FILLER = re.compile(r"[\s\W\d_]+")
_REPEATED_VOWEL = re.compile("([ㅏ-ㅣ])\\1+")
TAG_CHARS = re.compile("[\U000e0000-\U000e007f]")
# 깃발 이모지: 검은 깃발 바로 뒤에 소문자·숫자 태그 1–7개와 종료 태그(C2-judges S2.3).
# S2.3 has_hidden_tags가 같은 패턴을 쓴다. 이 밖의 태그 문자는 숨긴 것이다.
FLAG_SEQUENCE = re.compile("\U0001f3f4[\U000e0030-\U000e0039\U000e0061-\U000e007a]{1,7}\U000e007f")
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


def _invisible_class() -> str:
    """`_visible`이 False인 문자에서 태그 문자를 뺀 집합의 정규식 문자 클래스(임포트 때 1회).

    전 코드포인트에 `_visible`을 부르면 0.8초라 후보(Cf + `_INVISIBLE_RANGES`)에만 부른다.
    전 코드포인트 대조는 tests/test_preprocess.py가 한다.
    """
    cand = {cp for cp in range(0x110000) if unicodedata.category(chr(cp)) == "Cf"}
    cand |= {cp for lo, hi in _INVISIBLE_RANGES for cp in range(lo, hi + 1)}
    runs: list[list[int]] = []  # 범위로 묶어야 C 매칭이 빠르다(낱자 나열은 1MB에 0.2초)
    for cp in sorted(cp for cp in cand if not 0xE0000 <= cp <= 0xE007F and not _visible(chr(cp))):
        if runs and runs[-1][1] == cp - 1:
            runs[-1][1] = cp
        else:
            runs.append([cp, cp])
    return "[" + "".join(f"{re.escape(chr(lo))}-{re.escape(chr(hi))}" for lo, hi in runs) + "]"


_INVISIBLE = re.compile(_invisible_class() + "+")  # 연속 구간을 한 번에(1MB U+200B 47ms → 2ms)
_NOT_TAG = re.compile("[^\U000e0020-\U000e007e]+")  # decode_tags가 푸는 범위만 남긴다
# 상한을 넘을 때만 쓰는 문자 부류. 나열식이라 목록에 없는 문자(BMP의 흩어진 미할당 문자, 보이는
# 글자)로 채우는 것은 못 막는다. 범주 전체로 만든 클래스는 BMP 밖 범위가 수백 개라 1MB에 0.3–0.9초
# 빈칸류: 공백, 제어, 점자 빈칸, surrogate·BMP 사용자 영역, 비문자, plane 14–16 꼬리(미할당·사용자)
_B = r"\s\x00-\x1f\x7f-\x9f\u2800\ud800-\uf8ff\ufdd0-\ufdef\ufffe\uffff\U000e0080-\U0010ffff"
_M = r"\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f"  # BMP 결합 부호 블록
_MARK_RUN = re.compile(rf"([{_M}][{_M}])[{_M}]+")
_MARK_ON_BLANK = re.compile(rf"([{_B}])[{_M}]+")  # 빈칸 위에 얹은 결합 문자는 내용이 아니다
_BLANK_RUN = re.compile(rf"[{_B}]{{2,}}")
_REPEAT = re.compile(r"(\D)\1{3,}+", re.S)  # 숫자는 줄이지 않는다(1000000원). 역추적 없음
_CUT = "\n…\n"


def _ends(s: str, n: int, mark: str) -> str:
    if len(s) <= n:
        return s
    head = n // 2
    return s[:head] + mark + s[len(s) - (n - head) :]


def clip(text: str, n: int) -> str:
    """보이지 않는 문자(태그 문자 빼고)를 지우고 `n`자를 넘으면 앞 + 뒤만 남긴다. 정규식이라 선형.

    무거운 정규화(`normalize`) 전에 부른다. 태그를 뺀 본문이 넘을 때는 결합 문자 연속을 2개로,
    빈칸류 뒤의 결합 문자를 지우고, 빈칸류 연속을 스페이스 하나로, 같은 글자(숫자 빼고) 4번 이상을
    3개로 줄인 뒤 다시 잰다(빈칸처럼 보이는 문자로 양쪽을 채워 밀어내지 못하게). 줄인 글은 원문과
    다르다(들여쓰기·빈 줄이 사라짐).
    풀 수 있는 태그 문자는 따로 앞 + 뒤로 줄여 맨 앞에 둔다(`decode_tags`는 위치를 쓰지 않는다).
    한계: 보이는 글자(`ab`, `. ` 반복)나 `_B`·`_M`에 없는 문자로 양쪽을 채우면 가운데는 빠진다.
    """
    s = _INVISIBLE.sub("", text)
    if len(s) <= n:
        return s
    body = strip_tags(s)
    if len(body) > n:  # 본문이 상한 이하면 태그 때문에 넘어도 본문은 그대로
        body = _MARK_ON_BLANK.sub(r"\1", _MARK_RUN.sub(r"\1", body))
        body = _REPEAT.sub(r"\1\1\1", _BLANK_RUN.sub(" ", body))
    hidden = _NOT_TAG.sub("", FLAG_SEQUENCE.sub("", s))  # 깃발 이모지의 태그는 숨긴 것이 아니다
    return _ends(hidden, n, "") + _ends(body, n, _CUT)


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


def _compose_run(run: str, keep_emoticons: bool = True) -> str:
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


def _compose_all(text: str, keep_emoticons: bool = True) -> str:
    return _JAMO_RUN.sub(lambda m: _compose_run(m.group(), keep_emoticons), text)


def normalize(text: str) -> str:
    """LLM에 보내는 형태. 보이지 않는 문자 제거 → 전각·반각 접기 → 자모 조합."""
    return unicodedata.normalize("NFC", _compose_all(_prepare(text)))


def strip_tags(text: str) -> str:
    """태그 문자 U+E0000–E007F만 지운다. 자모 조합 등 다른 정규화는 하지 않는다."""
    return TAG_CHARS.sub("", text)


def decode_tags(text: str) -> tuple[str, str]:
    """학습자 발화의 태그 문자를 푼다. (태그를 지운 정규화본 [+ 숨긴 문자 표시], 푼 문자열).

    깃발 이모지의 태그는 풀지 않는다. 태그가 없으면 푼 문자열은 빈 문자열이다.
    """
    decoded = "".join(
        chr(ord(c) - 0xE0000) for c in FLAG_SEQUENCE.sub("", text) if 0xE0020 <= ord(c) <= 0xE007E
    )
    view = normalize(strip_tags(text))
    return (f"{view} [숨긴 문자: {decoded}]" if decoded else view), decoded


def _is_emoji(ch: str) -> bool:
    """이모지 그림 문자(U+1F000대, 피부색 수정자 포함)와 기타 기호·딩뱃(U+2600–27BF).
    라틴-1 기호·화살표(°, ©, →)는 이모지로 보지 않는다."""
    return "☀" <= ch <= "➿" or "\U0001f000" <= ch <= "\U0001faff"


def _in_emoji_sequence(text: str, i: int) -> bool:
    """변형 선택자는 이모지·키캡 밑글자 뒤에서만, ZWJ는 (변형 선택자를 건너뛴) 앞 글자와
    뒤 글자가 둘 다 이모지일 때만 정상으로 본다."""
    ch, prev, nxt = text[i], text[i - 1 : i], text[i + 1 : i + 2]
    if ch in _VS:
        keycap = bool(prev) and prev in "#*0123456789" and nxt == _KEYCAP
        return keycap or (bool(prev) and _is_emoji(prev))
    if ch == "‍":
        # 변형 선택자 하나는 건너뛴다. 그 선택자가 이모지 뒤에 있지 않으면 그 선택자도 드러난다.
        base = text[i - 2 : i - 1] if prev and prev in _VS else prev
        return bool(base) and bool(nxt) and _is_emoji(base) and _is_emoji(nxt)
    return False


def reveal_hidden(answer: str) -> str:
    """답변의 숨긴 문자를 드러낸다. 태그 문자(U+E0020–E007E)는 ASCII로, 나머지 폭 없는 문자와
    태그 블록(U+E0000–E00FF)의 남은 문자는 `<U+200B>` 형식으로 바꾼다. 이모지 시퀀스의
    ZWJ·변형 선택자, 깃발 이모지, 맨 앞 BOM은 둔다.

    파이썬 루프라 긴 입력은 느리다(140만 자에 약 1초). `max_answer_chars`로 자른 뒤 호출한다.
    """
    flags = {m.start(): m.end() for m in FLAG_SEQUENCE.finditer(answer)}
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
        elif cp <= 0xE00FF and cp >= 0xE0000:
            out.append(f"<U+{cp:04X}>")
        elif _visible(ch) or (i == 0 and cp == 0xFEFF) or _in_emoji_sequence(answer, i):
            out.append(ch)
        else:
            out.append(f"<U+{cp:04X}>")
        i += 1
    return "".join(out)


# --- 튜터 호환: skeleton -------------------------------------------------------------
# 가드 v6은 쓰지 않는다. 튜터 #98(judge/signals.py)·#99(answer/postprocess.py)가 import한다.
# 동작은 main(v4)과 같게 고정한다(tests/test_preprocess.py::test_skeleton).
# 지우는 조건: 저장소에서 `from guardrail.preprocess import skeleton`이 하나도 없을 때.


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


_STRETCHED_VOWEL = re.compile("([가-힣])([ㅏ-ㅣ])")


def _drop_stretched_vowel(m: re.Match[str]) -> str:
    """음절 바로 뒤의 모음이 그 음절의 중성과 같으면 늘여 쓴 것으로 보고 지운다."""
    syllable, vowel = m.groups()
    jung = (ord(syllable) - 0xAC00) % 588 // 28
    return syllable if ord(vowel) - ord("ㅏ") == jung else m.group()


def skeleton(text: str) -> str:
    """어휘 사전 매칭 전용 뷰(튜터 호환). 재현율 우선이라 이모티콘 예외 없이 조합한다.

    NFKC(①·²·𝟏 → 숫자) → 공백·기호·숫자 제거 → 같은 모음 반복 축약 → 자모 조합
    → 음절 뒤에 늘여 쓴 같은 모음 제거(시ㅣ발) → 소문자.
    라틴 문자는 남긴다(영어 공격 문구 매칭). 지운 구분자를 넘는 매칭 규칙은 ADR-0001 참고.
    """
    s = "".join(_fold_width(ch) for ch in text if _visible(ch))
    s = _nfkc_outside_jamo(s).translate(_CONJOINING_TO_COMPAT)
    s = _FILLER.sub("", s)
    s = _compose_all(_REPEATED_VOWEL.sub(r"\1", s), keep_emoticons=False)
    s = _STRETCHED_VOWEL.sub(_drop_stretched_vowel, s)
    return unicodedata.normalize("NFC", s).casefold()
