"""GR-01 전처리: 정규화본과 매칭 전용 골격 뷰 (ADR-0001)."""

import re
import unicodedata

INVISIBLE = re.compile(
    r"[\u00ad\u034f\u115f\u1160\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064"
    r"\u2066-\u206f\u3164\ufeff\ufe00-\ufe0f\uffa0\U000e0000-\U000e007f\U000e0100-\U000e01ef]"
)
# 초성 자모(U+1100..) -> 받침 자모(U+11A8..), 받침이 될 수 있는 것만
CHO_TO_JONG = {
    0x1100: 0x11A8,
    0x1101: 0x11A9,
    0x1102: 0x11AB,
    0x1103: 0x11AE,
    0x1105: 0x11AF,
    0x1106: 0x11B7,
    0x1107: 0x11B8,
    0x1109: 0x11BA,
    0x110A: 0x11BB,
    0x110B: 0x11BC,
    0x110C: 0x11BD,
    0x110E: 0x11BE,
    0x110F: 0x11BF,
    0x1110: 0x11C0,
    0x1111: 0x11C1,
    0x1112: 0x11C2,
}
COMPAT_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
FILLER = re.compile(r"[\s\W\d_]+")


def normalize(text: str) -> str:
    """보이지 않는 문자 제거 → NFKC → 자모 재조합(받침 포함). LLM에 보내는 형태."""
    s = unicodedata.normalize("NFKC", INVISIBLE.sub("", text))
    out: list[str] = []
    for i, ch in enumerate(s):
        c = ord(ch)
        nxt = ord(s[i + 1]) if i + 1 < len(s) else 0
        if 0x1100 <= c <= 0x1112 and not 0x1161 <= nxt <= 0x1175:
            prev = out[-1] if out else ""
            open_syllable = (
                prev and 0xAC00 <= ord(prev) <= 0xD7A3 and (ord(prev) - 0xAC00) % 28 == 0
            )
            if open_syllable and c in CHO_TO_JONG:
                out[-1] = chr(ord(prev) + CHO_TO_JONG[c] - 0x11A7)
            else:
                out.append(COMPAT_CHO[c - 0x1100])
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def skeleton(text: str) -> str:
    """어휘 사전 매칭 전용 뷰. 공백·기호·숫자 끼움을 지우고 소문자로 바꾼다.

    라틴 문자는 남긴다(영어 공격 문구 매칭).
    """
    return normalize(FILLER.sub("", INVISIBLE.sub("", text))).casefold()
