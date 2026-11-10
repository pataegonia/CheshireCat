"""Q2·Q3·Q4: 채점 코드(scoring.py)로 span/safe 전략과 가드 점수 상·하한·민감도를 시뮬레이션."""

import re

from common import BLOCK_CATS, guard_rows, out_part, output_label, scoring

SENT_RX = re.compile(r"(?<=[.?!])\s+|\n+")


def cand_rows():
    rows = [r for r in guard_rows() if "candidate_answer" in r]
    for r in rows:
        r["_label"] = output_label(r["labels"])
        r["_gold"] = r["labels"]["unsupported"]
        r["_safe"] = r["labels"]["safe"]
    return rows


def sentences_with(ans, span):
    """gold span을 포함하는 문장(문장 경계 . ? ! 줄바꿈). span이 경계를 넘으면 answer 전체."""
    i = ans.find(span)
    if i < 0:
        return ans
    starts = [0] + [m.end() for m in SENT_RX.finditer(ans)]
    ends = [m.start() for m in SENT_RX.finditer(ans)] + [len(ans)]
    for s, e in zip(starts, ends):
        if s <= i and i + len(span) <= e:
            return ans[s:e]
    return ans


def pad(ans, span, k):
    i = ans.find(span)
    return ans[max(0, i - k) : i + len(span) + k]


def wrong_phrase(ans, gold):
    """gold와 겹치지 않는 같은 길이의 다른 구간(가능하면 다른 문장)."""
    L = max(len("".join(gold)), 4)
    masked = ans
    for g in gold:
        masked = masked.replace(g, "\0" * len(g))
    for i in range(len(masked) - L + 1):
        seg = masked[i : i + L]
        if "\0" not in seg and seg.strip():
            return ans[i : i + L]
    return ans[:L]


def strategies(r):
    a, gold = r["candidate_answer"], r["_gold"]
    ung = r["_label"] == "ungrounded"
    G = not ung  # oracle grounded
    S = r["_safe"]
    out = {
        "a 상수(grounded,[],safe)": (True, [], True),
        "a' 상수 + safe=not block범주(오라클)": (True, [], S),
        "o 오라클 grounded + []": (G, [], S),
        "b 오라클 + gold 포함 문장 전체": (G, [sentences_with(a, g) for g in gold], S),
        "b2 오라클 + 답변 전체": (G, [a] if ung else [], S),
    }
    for k in (2, 5, 10, 20):
        out[f"c 오라클 + gold±{k}자"] = (G, [pad(a, g, k) for g in gold], S)
    out["d 오라클 + gold + 오답 구간 1개(같은 길이)"] = (
        G,
        gold + [wrong_phrase(a, gold)] if ung else [],
        S,
    )
    out["d2 오라클 + 오답 구간만"] = (G, [wrong_phrase(a, gold)] if ung else [], S)
    out["e 오라클 + gold 앞 50%"] = (G, [g[: max(1, len(g) // 2)] for g in gold], S)
    out["e2 오라클 + 다중 span 중 첫 번째만"] = (G, gold[:1], S)
    out["f 오라클 grounded=false지만 [] (span 포기)"] = (G, [], S)
    out["g 상수 ungrounded+[] (safe=true, a와 비교용)"] = (False, [], True)
    out["p 완벽"] = (G, gold, S)
    return out


def q2():
    rows = cand_rows()
    ung = [r for r in rows if r["_label"] == "ungrounded"]
    names = list(strategies(rows[0]))
    print("## Q2 span 전략 (output 부분 평균, 86행; ungrounded 35행의 span F1 평균)")
    print("| 전략 | output 평균(86) | span F1 평균(ung 35) | 비고 |")
    print("|---|---|---|---|")
    for n in names:
        tot, sf = 0.0, []
        for r in rows:
            g, sp, s = strategies(r)[n]
            tot += out_part(g, sp, s, r["_label"], r["_gold"], r["_safe"])
            if r in ung:
                sf.append(scoring.span_f1(sp, r["_gold"]))
        print(f"| {n} | {tot / len(rows):.3f} | {sum(sf) / len(sf):.3f} | |")
    # 길이 통계: gold span vs 문장
    gl = [len(scoring.normalize("".join(r["_gold"]))) for r in ung]
    sl = [
        len(
            scoring.normalize("".join(sentences_with(r["candidate_answer"], g) for g in r["_gold"]))
        )
        for r in ung
    ]
    print(
        f"- ung 35행 gold 정규화 길이 중앙값 {sorted(gl)[17]}, gold 포함 문장 길이 중앙값 {sorted(sl)[17]}"
    )
    print(f"- gold/문장 길이비 중앙값 {sorted(g / s for g, s in zip(gl, sl))[17]:.2f}")


def breakeven():
    print(
        "\n## Q2 손익분기 p* (항목당 output 부분 ×3 = grounded 정답 + span F1 + safe; safe는 같다고 두고 제외)"
    )
    print("- 선택지 A grounded/[]: 실제 ung이면 0+0, 실제 grounded면 1+1 → EV_A = 2(1-p)")
    print("- 선택지 B ungrounded/[]: 실제 ung이면 1+0, grounded면 0+1 → EV_B = 1 (p와 무관)")
    print("- 선택지 C ungrounded/spans(품질 s): 실제 ung이면 1+s, grounded면 0+0 → EV_C = p(1+s)")
    print("- A↔B: p=0.5, B↔C: p=1/(1+s), A↔C: p=2/(3+s)")
    print("| s | A↔C p*=2/(3+s) | B↔C p=1/(1+s) | 최적 규칙 |")
    print("|---|---|---|---|")
    for s in (0.3, 0.5, 0.7, 0.9, 1.0):
        print(
            f"| {s} | {2 / (3 + s):.3f} | {1 / (1 + s):.3f} | p<0.5→A, 0.5≤p<{1 / (1 + s):.3f}→B, 이상→C |"
        )
    print("\n- EV 표(항목당 3점 만점 중 grounded+span 2점 기준):")
    print("| p | A | B | C(s=0.5) | C(s=0.7) | C(s=0.9) |")
    print("|---|---|---|---|---|---|")
    for p in (0.2, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        print(f"| {p} | {2 * (1 - p):.2f} | 1.00 | {p * 1.5:.2f} | {p * 1.7:.2f} | {p * 1.9:.2f} |")
    # 검증: 실제 채점 함수로 동일
    assert scoring.span_f1([], ["x"]) == 0.0 and scoring.span_f1(["x"], []) == 0.0


def q3():
    rows = cand_rows()
    nb = sum(r["labels"]["category"] in BLOCK_CATS for r in rows)
    print(
        f"\n## Q3 safe 전략 (공개: 후보 86 중 block 범주 {nb}, safe=false {sum(not r['_safe'] for r in rows)})"
    )
    print(
        "- 가정: 비공개 후보 문항 중 block 범주 비율 = 공개와 같음(22%), block 범주 변형 중 거절형 비율 r(거절형은 safe=true)"
    )
    print(
        "- 규칙 R1 safe = not block범주 (범주 예측 정확도 c), 규칙 R2 답변 순응 판정(정확도 q, normal 오탐 f)"
    )
    fb = nb / len(rows)
    print(
        "| r | R1(c=1.0) | R1(c=0.95) | R2(q=0.95,f=0.01) | R2(q=0.9,f=0.02) | R1+거절문구 감지(k=0.9) |"
    )
    print("|---|---|---|---|---|---|")
    for r in (0.0, 0.2, 0.5):

        def r1(c):
            return fb * ((1 - r) * c + r * (1 - c)) + (1 - fb) * c

        def r2(q, f):
            return fb * q + (1 - fb) * (1 - f)

        k = 0.9  # 거절문구 감지 재현율(거절형을 safe=true로 되돌림), 순응형 오탐 0 가정
        hy = fb * ((1 - r) + r * k) + (1 - fb)
        print(
            f"| {r} | {r1(1.0):.3f} | {r1(0.95):.3f} | {r2(0.95, 0.01):.3f} | {r2(0.9, 0.02):.3f} | {hy:.3f} |"
        )
    print("- 표 값 = safe 정답률(후보 문항 평균). safe 1문항 = output 부분 1/3.")


def make_r(gold_block, cat_scored, input_ok, cat_ok, out_val=None):
    return {
        "valid": True,
        "gold_block": gold_block,
        "input_correct": input_ok,
        "category_scored": cat_scored,
        "category_correct": cat_ok,
        "has_output": out_val is not None,
        "output_correct": None if out_val is None else out_val[0],
        "span_f1": None if out_val is None else out_val[1],
        "safe_correct": None if out_val is None else out_val[2],
    }


def guard_with(pred_block, pred_cat, pred_out):
    rows = guard_rows()
    res = []
    for r in rows:
        lab = r["labels"]
        gb = lab["decision"] == "block"
        cat = lab["category"]
        pb = pred_block(r)
        pc = pred_cat(r)
        ov = None
        if "candidate_answer" in r:
            g, sp, s = pred_out(r)
            ol = output_label(lab)
            ov = (
                g == (ol != "ungrounded"),
                scoring.span_f1(sp, lab["unsupported"]),
                s == lab["safe"],
            )
        res.append(make_r(gb, cat != "normal", pb == gb, pc == cat, ov))
    return scoring.guard_score(res), res


def q4():
    gold_block = lambda r: r["labels"]["decision"] == "block"  # noqa: E731
    gold_cat = lambda r: r["labels"]["category"]  # noqa: E731
    const_out = lambda r: (True, [], True)  # noqa: E731
    safe_rule = lambda r: (True, [], r["labels"]["category"] not in BLOCK_CATS)  # noqa: E731
    perfect_out = lambda r: (
        r["labels"]["grounded"],
        r["labels"]["unsupported"],
        r["labels"]["safe"],
    )  # noqa: E731
    sc = [
        ("전부 allow/normal + 상수 output", lambda r: False, lambda r: "normal", const_out),
        (
            "전부 block/harmful_content + 상수 output",
            lambda r: True,
            lambda r: "harmful_content",
            const_out,
        ),
        (
            "전부 block/harmful + 완벽 output",
            lambda r: True,
            lambda r: "harmful_content",
            perfect_out,
        ),
        ("전부 allow/normal + 완벽 output", lambda r: False, lambda r: "normal", perfect_out),
        ("완벽 decision·category + 상수 output", gold_block, gold_cat, const_out),
        ("완벽 decision·category + safe=not block, grounded/[]", gold_block, gold_cat, safe_rule),
        (
            "완벽 decision·category + 오라클 grounded·safe, span []",
            gold_block,
            gold_cat,
            lambda r: (r["labels"]["grounded"], [], r["labels"]["safe"]),
        ),
        (
            "완벽 decision, category 전부 normal + safe 규칙",
            gold_block,
            lambda r: "normal",
            safe_rule,
        ),
        ("완벽 전부", gold_block, gold_cat, perfect_out),
    ]
    print("\n## Q4 상·하한 (공개 200, scoring.guard_score)")
    print("| 시나리오 | 가드 점수 | 총점 기여(×0.5) |")
    print("|---|---|---|")
    for name, pb, pc, po in sc:
        s, _ = guard_with(pb, pc, po)
        print(f"| {name} | {s:.2f} | {s / 2:.2f} |")

    print("\n## Q4 민감도 (비공개 ~300 가정, 공개 비율 유지: block 84, 비normal 129, 후보 129)")
    N, B, NN, NO = 300, 84, 129, 129
    for f1_tp, f1_fp, f1_fn in ((B, 0, 0), (B - 8, 8, 8), (B - 15, 15, 15)):
        base = 2 * f1_tp / (2 * f1_tp + f1_fp + f1_fn)
        fp1 = 2 * f1_tp / (2 * f1_tp + f1_fp + 1 + f1_fn)
        fn1 = 2 * (f1_tp - 1) / (2 * (f1_tp - 1) + f1_fp + f1_fn + 1)
        print(
            f"- F1 {base:.3f}(TP{f1_tp},FP{f1_fp},FN{f1_fn})에서 FP 1개 −{45 * (base - fp1):.3f}점, FN 1개 −{45 * (base - fn1):.3f}점 (가드 100점 척도)"
        )
    print(f"- 범주 오류 1개: −{10 / NN:.3f}점")
    print(
        f"- output 1문항 전체(3요소) −{45 / NO:.3f}점, 1요소 −{45 / NO / 3:.3f}점, span F1 0.1 −{45 / NO / 3 * 0.1:.4f}점"
    )
    print(
        f"- block↔allow 를 넘는 범주 오류 1개(예: harmful→normal): F1(FN) + 범주 = −{45 * (1 - 2 * (B - 1) / (2 * (B - 1) + 1)) + 10 / NN:.3f}점"
    )
    print("- 총점은 위의 ×0.5")
    # 헤드룸
    s_const, _ = guard_with(gold_block, gold_cat, const_out)
    s_safe, _ = guard_with(gold_block, gold_cat, safe_rule)
    print(
        f"- 헤드룸: output 상수→완벽 {100 - s_const:.2f}점, safe 규칙→완벽 {100 - s_safe:.2f}점(나머지는 grounded·span)"
    )


if __name__ == "__main__":
    q2()
    breakeven()
    q3()
    q4()
