import re, unicodedata as u
INVIS = re.compile('[­͏ᅟᅠ᠎​-‏‪-‮⁠-⁤⁦-⁯ㅤ﻿︀-️ﾠ\U000e0000-\U000e007f\U000e0100-\U000e01ef]')
# 종성 인덱스: 초성(U+1100..) -> 종성(U+11A8..) 가능한 것만
CHO2JONG = {0x1100:0x11A8,0x1101:0x11A9,0x1102:0x11AB,0x1103:0x11AE,0x1105:0x11AF,0x1106:0x11B7,0x1107:0x11B8,0x1109:0x11BA,0x110A:0x11BB,0x110B:0x11BC,0x110C:0x11BD,0x110E:0x11BE,0x110F:0x11BF,0x1110:0x11C0,0x1111:0x11C1,0x1112:0x11C2}
COMPAT_CHO = 'ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ'
def normalize(s):
    s = INVIS.sub('', s)
    s = u.normalize('NFKC', s)
    out = []
    for i, ch in enumerate(s):
        c = ord(ch); nxt = ord(s[i+1]) if i+1 < len(s) else 0
        if 0x1100 <= c <= 0x1112 and not (0x1161 <= nxt <= 0x1175):
            prev = out[-1] if out else ''
            if prev and 0xAC00 <= ord(prev) <= 0xD7A3 and (ord(prev)-0xAC00) % 28 == 0 and c in CHO2JONG:
                out[-1] = chr(ord(prev) + CHO2JONG[c] - 0x11A7)   # 받침 붙이기
            else:
                out.append(COMPAT_CHO[c-0x1100])                   # 초성-only는 호환 자모로 복원
            continue
        out.append(ch)
    return u.normalize('NFC', ''.join(out))
def skeleton(s):  # 매칭 전용 뷰: 공백/구두점/숫자/라틴 끼움 제거
    return normalize(re.sub(r'[\s\W\d_]+', '', INVIS.sub('', s)))
if __name__ == '__main__':
    assert normalize('ㅂㅏㅂㅗ') == '바보'
    assert normalize('ㄱㅏㄱ') == '각'
    assert normalize('ㅅㅂ') == 'ㅅㅂ'
    assert normalize('바​보️') == '바보'
    assert normalize('ｓｉｂａｌ') == 'sibal'
    assert skeleton('시1발') == '시발' and skeleton('바 . 보') == '바보'
    assert normalize('학교') == '학교'
    assert skeleton('ㅂ ㅏ ㅂ ㅗ') == '바보'
    print('ok')
