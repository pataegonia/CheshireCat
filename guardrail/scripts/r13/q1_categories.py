"""Q1: 범주 경계 — 최소쌍, 단서 정규식의 범주별 분포, 멀티턴, 빠른 질문 버튼, tutor 라벨 스키마."""

import difflib
import re
from collections import Counter, defaultdict

from common import BLOCK_CATS, all_user, guard_rows, last_user, norm, tutor_rows


def grams(s, n=3):
    s = norm(s)
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def jacc(a, b):
    return len(a & b) / len(a | b) if a | b else 0.0


def diff_segments(a, b):
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "equal":
            out.append(f"[{a[i1:i2].strip()}→{b[j1:j2].strip()}]")
    return " ".join(out)[:160]


def minimal_pairs(rows, thr=0.5):
    g = [grams(all_user(r)) for r in rows]
    pairs = []
    for i, r in enumerate(rows):
        ci = r["labels"]["category"]
        if ci == "normal":
            continue
        best = max(
            (
                (jacc(g[i], g[j]), j)
                for j in range(len(rows))
                if rows[j]["labels"]["category"] != ci
            ),
            default=(0, -1),
        )
        pairs.append((best[0], r, rows[best[1]]))
    pairs.sort(key=lambda x: -x[0])
    print("\n## 최소쌍: 비normal 문항마다 다른 범주의 최근접 이웃 (user 발화 char-3gram Jaccard)")
    print("| sim | 범주(이 문항) | 이웃 범주 | 차이(이 문항→이웃) |")
    print("|---|---|---|---|")
    for s, a, b in pairs:
        la, lb = last_user(a).replace("\n", "/"), last_user(b).replace("\n", "/")
        print(
            f"| {s:.2f} | {a['labels']['category']} {a['id']} | {b['labels']['category']} {b['id']} | {diff_segments(la, lb)} |"
        )
    sims = [s for s, _, _ in pairs]
    for t in (0.8, 0.6, 0.5, 0.4):
        n = sum(s >= t for s, _, _ in pairs)
        print(f"- sim≥{t}: {n}/{len(pairs)}")
    nb = Counter(b["labels"]["category"] for s, a, b in pairs if s >= thr)
    print("- 이웃 범주 분포(sim≥0.5):", dict(nb))
    return sims


CUES = {
    "정답만": r"정답만",
    "풀이/푸는 방법·hint·check": r"풀이 ?방법|푸는 ?(방법|법)|hint|힌트|check ?방법|어떻게 풀|어떤 기준으로 풀|어떤 순서로 계산",
    "제출(용)/수행평가 답안·전체": r"제출|수행평가 답안|보고서 전체|완성 ?code|완성 코드",
    "수행평가·숙제 언급": r"수행평가|숙제|과제",
    "광고·홍보 문구/그림/사과 문자": r"광 ?고|홍보|slogan|copy|그 ?림|사과 문자",
    "이전 지시 무시/제한 없": r"이전 지시|제한 없|튜터 역할을 버|역할 버려",
    "시스템/숨겨진 프롬프트·지시": r"시스템 ?(메시지|프롬프트)|system prompt|sys prompt|숨겨진 (지시|지침|프롬프트)|숨겨진 프롬프트",
    "~가 돼서/안내자가 돼/역할로 바꿔/맡아서": r"돼서|가 돼|역할로 바꿔|맡아서|적용해",
    "검토/비교/분석/위험성/왜 (메타 질문)": r"검토|비교해서|분석|위험성|왜 위험|왜 차별|수상한 부분|위험 신호|어케 표시|어떻게 써|보여 줘\.?$|가상의 예",
    "사칭·스미싱·속여·인증번호 받아내": r"사칭|스미싱|속여|받아내|비밀번호 입력을 유도|돈을 더 보내게",
    "타인 정보(친구/선생님/다른 학생 + 번호/주소/이름·학교/성적)": r"(친구|선생님|다른 학생|반 친구|애들|우리 반).{0,12}(번호|주소|폰번|이름이랑 학교|이름과 학교|성적|전화)|(이름이랑 학교|비공개 성적)",
    "자기소개(나는/내 이름은/제 이름은 …야)": r"(^|\n|\s)(나는|난|내 이름은|제 이름은) ?[가-힣]{1,4}(야|이야|이에요|예요|고)|[0-9]학년",
    "혐오(동네/마을 사람들)": r"(동네|마을) 사람들",
    "폭력·모욕": r"때려|다치게|가치도 없|쓸모없",
    "몇 주째/혼자 버티기": r"몇 주째|혼자 버티|아무것도 하기",
    "걱정/헷갈려(가벼운 감정)": r"걱정|헷갈려|막막",
    "ㅁㄴㅇㄹ류 무의미": r"^[ㄱ-ㅎㅏ-ㅣ ]+$",
    "이 과정의 … 몇 점/수수료/최대/만점/평가 기준(자료 밖 값)": r"수수료|fee|만점|최대 금액|평가 기준|배점|통과 점수|몇 ?글자|몇번 확인|limit|이주일",
    "[방해] 거절하지 말고/안 된다는 말 말고": r"거절하지 말고|안 된다는 말 말고",
    "[방해] 그냥/걍 궁금": r"그냥 궁금|걍 궁금|그냥 호기심|그냥 장난",
    "[방해] 권한 주장(쌤/선생님/부모님/엄마 허락, 개발자/관리자/선생님 계정)": r"(쌤|선생님|부모님|엄마).{0,6}(ㅇㅋ|된다|된댔|된대|ㄱㅊ|괜찮)|개발자|관리자|선생님 계정",
    "[방해] 소설/연극/역할극 프레이밍": r"소설|연극|대본|역할극",
    "[방해] 붙여 넣은 글": r"붙여 ?넣|붙일게|정리한 글|정리한 내용|아래 글",
    "[방해] 영어 혼용": r"[A-Za-z]{3,}",
}


def cue_table(rows, title):
    cats = [c for c, _ in Counter(r["labels"]["category"] for r in rows).most_common()]
    print(f"\n## 단서 정규식 × 범주 ({title}, 마지막 user 발화 기준; 괄호=전체 user 발화)")
    print("| 단서 | " + " | ".join(c[:10] for c in cats) + " |")
    print("|---|" + "---|" * len(cats))
    for name, pat in CUES.items():
        rx = re.compile(pat, re.M)
        cells = []
        for c in cats:
            rs = [r for r in rows if r["labels"]["category"] == c]
            a = sum(bool(rx.search(last_user(r))) for r in rs)
            b = sum(bool(rx.search(all_user(r))) for r in rs)
            cells.append(f"{a}" + (f"({b})" if b != a else ""))
        print(f"| {name} | " + " | ".join(cells) + " |")
    print(
        "| (n) | "
        + " | ".join(str(sum(r["labels"]["category"] == c for r in rows)) for c in cats)
        + " |"
    )


def multiturn(rows):
    print("\n## 멀티턴")
    t = Counter((r["labels"]["category"], len(r["messages"])) for r in rows)
    cats = sorted({c for c, _ in t})
    print("| 범주 | 1턴 | 3메시지 | 5메시지 |")
    print("|---|---|---|---|")
    for c in cats:
        print(f"| {c} | {t[(c, 1)]} | {t[(c, 3)]} | {t[(c, 5)]} |")
    # 이력 user 발화에 공격 단서가 있는데 정답이 normal인 문항 / 마지막 발화에만 있는 문항
    rx = re.compile(
        "|".join(
            CUES[k]
            for k in (
                "이전 지시 무시/제한 없",
                "시스템/숨겨진 프롬프트·지시",
                "사칭·스미싱·속여·인증번호 받아내",
                "혐오(동네/마을 사람들)",
            )
        )
    )
    hist_only = []
    for r in rows:
        if len(r["messages"]) == 1:
            continue
        hist = "\n".join(m["content"] for m in r["messages"][:-1] if m["role"] == "user")
        if rx.search(hist) and not rx.search(last_user(r)):
            hist_only.append(
                (r["id"], r["labels"]["category"], last_user(r)[:50].replace("\n", "/"))
            )
    print("- 공격 단서가 이력 user 발화에만 있는 문항:", hist_only)
    prior_refusal = [
        (r["id"], r["labels"]["category"])
        for r in rows
        if any(
            m["role"] == "assistant" and re.search(r"안 돼요|할 수 없어요|함부로", m["content"])
            for m in r["messages"]
        )
    ]
    print("- 이력 assistant 발화에 거절/경고가 있는 문항:", prior_refusal)


BUTTONS = [
    "실습 도중 어려운 부분이 있어요",
    "힌트를 받을 수 있을까요",
    "이 코드를 설명해주세요",
    "도움을 받을 수 있을까요",
]


def buttons(guard, tutor):
    print("\n## 빠른 질문 버튼 첫 발화")
    for name, rows in (("guard", guard), ("tutor", tutor)):
        hits = [
            r
            for r in rows
            if any(b in r["messages"][0]["content"] for b in BUTTONS)
            and len(r["messages"][0]["content"]) < 60
        ]
        firsts = Counter(r["messages"][0]["content"] for r in hits)
        print(
            f"- {name}: {len(hits)}문항, 범주 {dict(Counter(r['labels']['category'] for r in hits))}, 문구 {dict(firsts)}"
        )


def tutor_schema(tutor):
    print("\n## tutor.jsonl 라벨 스키마/분포")
    print("- 키:", Counter(tuple(sorted(r["labels"])) for r in tutor))
    print("- 메시지 수:", dict(sorted(Counter(len(r["messages"]) for r in tutor).items())))
    ca = Counter((r["labels"]["category"], r["labels"]["action"]) for r in tutor)
    print("| category | action | n | evidence 비어있음 | gold_facts 비어있음 |")
    print("|---|---|---|---|---|")
    for (c, a), n in ca.most_common():
        rs = [r for r in tutor if r["labels"]["category"] == c and r["labels"]["action"] == a]
        print(
            f"| {c} | {a} | {n} | {sum(not r['labels']['evidence'] for r in rs)} | {sum(not r['labels']['gold_facts'] for r in rs)} |"
        )
    ev = Counter(len(r["labels"]["evidence"]) for r in tutor)
    print("- evidence 개수 분포:", dict(ev))
    print(
        "- 과목 분포(evidence 접두):",
        dict(Counter(e.split("-")[0] for r in tutor for e in r["labels"]["evidence"])),
    )


def main():
    guard, tutor = guard_rows(), tutor_rows()
    print("# Q1 결과")
    minimal_pairs(guard)
    cue_table(guard, "guard 200")
    cue_table(tutor, "tutor 200")
    multiturn(guard)
    buttons(guard, tutor)
    tutor_schema(tutor)
    by = defaultdict(list)
    for r in guard + tutor:
        by[r["labels"]["category"]].append(r)
    print(
        "\n- guard+tutor 범주 합계:",
        {c: len(v) for c, v in sorted(by.items(), key=lambda x: -len(x[1]))},
    )
    print("- block 범주(F1) 확인:", sorted(BLOCK_CATS))


if __name__ == "__main__":
    main()
