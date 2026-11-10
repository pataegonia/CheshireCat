"""Extra: 길이·유니코드·코드블록·영어 통계 + Q3 보조(거절 문구 감지 정규식의 분리력)."""

import re
import statistics as st
import unicodedata
from collections import Counter

from common import BLOCK_CATS, guard_rows, last_user, tutor_rows

REFUSAL_RX = re.compile(
    r"수업과 (무관|관련이 없)|써 줄 수 없|수는 없|말아 주세요|계속 도와|돌아가 볼게요|함께 (공부|살펴)|하지 말|할 수 없|안 돼요|안 돼\b|도와줄 수 없|드릴 수 없|알려 줄 수 없|맡을 수 없|않을게요|하지 않아요|대신 .{0,20}(도와|알려|할 수)|못 해요|공개하지 않"
)


def q(xs):
    xs = sorted(xs)
    return f"min {xs[0]} / 중앙 {st.median(xs):.0f} / p90 {xs[int(0.9 * (len(xs) - 1))]} / max {xs[-1]}"


def main():
    g, t = guard_rows(), tutor_rows()
    print("## 길이 (문자 수)")
    print("- guard 마지막 user:", q([len(last_user(r)) for r in g]))
    print("- guard 대화 전체:", q([sum(len(m["content"]) for m in r["messages"]) for r in g]))
    print(
        "- guard candidate_answer:",
        q([len(r["candidate_answer"]) for r in g if "candidate_answer" in r]),
    )
    print("- tutor 대화 전체:", q([sum(len(m["content"]) for m in r["messages"]) for r in t]))
    print("- tutor reference_answer:", q([len(r["labels"]["reference_answer"]) for r in t]))
    long_paste = [r for r in g if len(last_user(r)) > 300]
    print(
        f"- guard 마지막 user >300자: {len(long_paste)}  범주 {dict(Counter(r['labels']['category'] for r in long_paste))}"
    )

    print("\n## 유니코드")
    for name, rows in (("guard", g), ("tutor", t)):
        texts = [m["content"] for r in rows for m in r["messages"]] + [
            r.get("candidate_answer", "") for r in rows
        ]
        cats = Counter(unicodedata.category(ch) for s in texts for ch in s)
        cf = Counter(
            ch
            for s in texts
            for ch in s
            if unicodedata.category(ch) in ("Cf", "Co", "Cn", "Cc") and ch not in "\n\t"
        )
        nfkc = Counter(ch for s in texts for ch in s if unicodedata.normalize("NFKC", ch) != ch)
        jamo = sum(bool(re.search(r"[ㄱ-ㅎㅏ-ㅣ]", s)) for s in texts)
        print(
            f"- {name}: 보이지 않는/제어 문자 {dict(cf) or '없음'}, NFKC로 바뀌는 문자 {dict(nfkc.most_common(10)) or '없음'}, 자모 포함 발화 {jamo}, 문자 범주 상위 {cats.most_common(6)}"
        )

    print("\n## 코드 블록·영어·특수 형식 (guard, 마지막 user 기준)")
    feats = {
        "``` 코드블록": lambda s: "```" in s,
        "인라인 `코드`": lambda s: bool(re.search(r"`[^`]+`", s)),
        "import/print( 등 코드 줄": lambda s: bool(
            re.search(r"^\s*(import |print\(|for |if |def |[a-z_]+ = )", s, re.M)
        ),
        "영문만(한글 0)": lambda s: not re.search(r"[가-힣]", s),
        "영문 단어 ≥3": lambda s: len(re.findall(r"[A-Za-z]{2,}", s)) >= 3,
        "①②/1. 번호 목록": lambda s: bool(re.search(r"[①②③]|^\s*\d\.\s", s, re.M)),
        "줄바꿈 포함": lambda s: "\n" in s,
        "음절 사이 공백(광 고/시 드 류)": lambda s: bool(
            re.search(r"(광 고|시 드|평 균|야 구|그 림|예 시|발 급|수 수|가 입|잇어|됄지)", s)
        ),
        "자모 축약(ㄱㄱ/ㅇㅋ/ㄹㅇ/ㄷㅂ)": lambda s: bool(re.search(r"ㄱㄱ|ㅇㅋ|ㄹㅇ|ㄷㅂ|ㄱㅊ", s)),
    }
    cats = [c for c, _ in Counter(r["labels"]["category"] for r in g).most_common()]
    print("| 특징 | 합계 | " + " | ".join(c[:10] for c in cats) + " |")
    print("|---|---|" + "---|" * len(cats))
    for name, f in feats.items():
        cells = [sum(f(last_user(r)) for r in g if r["labels"]["category"] == c) for c in cats]
        print(f"| {name} | {sum(cells)} | " + " | ".join(map(str, cells)) + " |")
    ca = [r for r in g if "candidate_answer" in r]
    n_cite = sum(bool(re.search(r"(PY|PR|FIN)-\d\d-\d\d", r["candidate_answer"])) for r in ca)
    print(
        f"- candidate_answer 코드블록 {sum('```' in r['candidate_answer'] for r in ca)}/86, 인라인 코드 {sum('`' in r['candidate_answer'] for r in ca)}/86, 섹션ID 인용 {n_cite}/86, 영문만 {sum(not re.search(r'[가-힣]', r['candidate_answer']) for r in ca)}/86"
    )

    print("\n## Q3 보조: 거절 문구 정규식 분리력")
    pos = [
        r["labels"]["reference_answer"]
        for r in t
        if r["labels"]["action"] in ("refuse", "redirect")
    ]
    neg_block = [r["candidate_answer"] for r in ca if r["labels"]["category"] in BLOCK_CATS]
    neg_norm = [r["candidate_answer"] for r in ca if r["labels"]["category"] not in BLOCK_CATS]
    print(
        f"- tutor refuse/redirect 참조답변(거절형 대용) {sum(bool(REFUSAL_RX.search(s)) for s in pos)}/{len(pos)} 감지"
    )
    print(
        f"- guard block범주 순응형 후보답변 {sum(bool(REFUSAL_RX.search(s)) for s in neg_block)}/{len(neg_block)} 오감지"
    )
    print(
        f"- guard normal 후보답변 {sum(bool(REFUSAL_RX.search(s)) for s in neg_norm)}/{len(neg_norm)} 감지(거절형 아님)"
    )
    miss = [s[:60] for s in pos if not REFUSAL_RX.search(s)]
    print("- 거절형 미감지 예:", miss[:6])
    hit_norm = [s[:60] for s in neg_norm if REFUSAL_RX.search(s)]
    print("- normal 감지 예:", hit_norm[:4])


if __name__ == "__main__":
    main()
