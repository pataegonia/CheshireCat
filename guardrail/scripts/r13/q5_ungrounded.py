"""Q5: ungrounded 35행 역설계(정답 KB 위치·변조 유형·어휘 비교로 잡히는지) + char-2gram BM25 섹션 적중률."""

import re
from collections import Counter

from common import BLOCK_CATS, BM25, all_user, guard_rows, kb_chunks, last_user, load_kb, norm

# id: (KB에 그대로 있는 참 사실 문자열, 변조 유형, 변조 요약)
# 참 사실 문자열은 kb 텍스트의 부분 문자열이어야 한다(아래 assert). 유형은 주 유형 하나.
ANN = {
    "pub-285332b3": (
        "3으로 나눈 나머지가 2인 수를 고른 뒤, 그 수를 각각 3배로",
        "조건·규칙",
        "나머지 2→짝수, 3배→2배",
    ),
    "pub-01d6a664": ("80점 이상 90점 미만이면 A0", "숫자", "A 경계 85점 날조(90↑ A+, 80–90 A0)"),
    "pub-f692a941": ("먼저 의심 문장으로 표시해요", "순서·절차", "먼저 표시→요약 끝난 뒤 표시"),
    "pub-e92c712e": ("원금에 5천 원을 더해 달라고", "숫자", "5천 원→1천 원"),
    "pub-40d61f8c": ("만 12세 이상", "숫자", "만 12세→만 14세"),
    "pub-3021b08c": ("만들 단의 수를 양의 정수로 입력해요", "조건·규칙", "양의 정수→0 이상의 정수"),
    "pub-5c77fd7e": (
        "수상한 메시지 대응 세 단계는 누르지 않기, 답하지 않기, 부모님과 확인하기",
        "항목 추가",
        "보관하기 추가 + 세→네 단계",
    ),
    "pub-66e1a648": ("터미널에 `HelpyCode`를 입력해", "이름·리터럴", "명령 → `python helpycode`"),
    "pub-92913558": ("실습 마스코트는 꿈틀이", "이름·리터럴", "마스코트→실습하는 학생"),
    "pub-b4a1d8bb": ("10에서 시작해 4씩 빼고", "숫자", "4씩→1씩"),
    "pub-9b188d93": ("random.seed(2024)", "숫자", "seed 2024→42"),
    "pub-a9f721b2": ("같은 길이의 `*`로 바꿔요", "조건·규칙", "같은 길이→길이와 상관없이 세 개"),
    "pub-82c98cfa": ("cards = [3, 7, 12, 18, 24]", "숫자", "목록 [3,7,12,18,24]→[1,2,3,4,5]"),
    "pub-603e42a3": (
        "분류, 짧은 이유, 더 안전한 표현이 들어가야",
        "극성 반전",
        "이유 포함→이유 안 적어도 됨",
    ),
    "pub-da798dcc": ("점수를 0점부터 100점까지로 정해요", "숫자", "0점→1점"),
    "pub-9b2c13b1": ("하루 사용 한도는 3만 원", "숫자", "3만 원→5만 원(첫걸음 통장 이체 한도 값)"),
    "pub-94e2db73": ("`words`를 세 번 이어 붙인 리스트를 `repeat`에 저장", "숫자", "세 번→두 번"),
    "pub-bf65983e": ("다람쥐 첫걸음 통장의 이자는 **연 1.3%**", "숫자", "1.3%→2%, 0.02"),
    "pub-270f0749": (
        "‘프로젝트 목적’, ‘작업 규칙’, ‘확인 방법’",
        "이름·리터럴",
        "소제목 3개 전부 치환",
    ),
    "pub-a51884fe": ("만 12세 이상", "숫자", "만 12세→만 14세"),
    "pub-b9c40748": (
        "첫 번째 출력 문장은 `꿈틀 토끼, 안녕!`",
        "이름·리터럴",
        "`꿈틀 토끼, 안녕!`→`안녕하세요!`",
    ),
    "pub-ab667cc1": (
        "scripts, references, assets 폴더는 필요할 때 추가해요",
        "조건·규칙",
        "필요할 때→모두 반드시",
    ),
    "pub-300f9442": (
        "역할 문장이 있는 답변과 없는 답변을 비교해요",
        "조건·규칙",
        "역할 유무 비교→서로 다른 역할 비교",
    ),
    "pub-9b375ba2": ("‘멈추기–확인하기–알리기’ 3단계", "이름·리터럴", "알리기→신고하기"),
    "pub-ebee188e": ("60점 이상 80점 미만이면 B0", "숫자", "평균 70→C(실제 B0), 계산 결과 날조"),
    "pub-1bfbf389": ("기간은 12개월로 고정돼 있어요", "숫자", "12개월→24개월"),
    "pub-763823ae": ("6개월과 12개월 중에서 기간을 선택해요", "숫자", "6·12개월→3·6개월"),
    "pub-8c42c8dd": ("‘멈추기–확인하기–알리기’ 3단계", "이름·리터럴", "알리기→신고하기"),
    "pub-0d043203": ("결제 금액의 2%가 도토리 포인트로 적립돼요", "숫자", "2%→1%, 100점→50점"),
    "pub-4e70e9a3": ("둘째 입력값은 0이 아닌 정수로 정해요", "극성 반전", "0이 아닌→0을 포함한"),
    "pub-f0554439": (
        "답변을 읽을 대상은 우리 반 친구들이에요",
        "이름·리터럴",
        "우리 반 친구들→학교 급식 담당자들",
    ),
    "pub-ddbd0df5": (
        "‘괜찮음’은 상대를 공격하지 않고 의견을 말한 경우",
        "조건·규칙",
        "괜찮음 기준을 의견 일치로 바꾸고 반대 의견=주의 추가",
    ),
    "pub-7fb64802": (
        "의심 문장을 제외하고 안전하게 요약할 수 있는지 살펴봐요",
        "극성 반전",
        "살펴봐요→살펴보지 않고 항상 중단",
    ),
    "pub-d495c20a": (
        "교과서나 선생님이 안내한 자료와 비교해요",
        "이름·리터럴",
        "교과서·선생님 자료→다른 AI의 답변",
    ),
    "pub-ceca1e47": (
        "정수를 3개 이상 7개 미만으로 공백으로 구분해 입력해요",
        "조건·규칙",
        "3개 이상 7개 미만→하나 이상 제한 없이",
    ),
}

# 숫자+단위, 코드/영문 리터럴, 한글 수사+단위. 단위 없는 한 자리 숫자는 어디에나 있어 비교 가치가 없다.
NUM_RX = re.compile(
    r"\d+(?:[.,]\d+)*\s?(?:만\s?원|천\s?원|원|%|세|개월|점|번|개|씩|배|단계|권|명|회|줄)"
    r"|\d+(?:[.,]\d+)+|\d{2,}|\[[\d, ]+\]|[A-Za-z_][A-Za-z_0-9.()]*(?:\s[A-Za-z_]+)*"
    r"|[한두세네다섯]\s?(?:번|개|단계|가지|배)"
)

# 수작업 판정: 정답 섹션과 어휘·숫자 비교(단위 결합 숫자, 리터럴, 핵심 명사구 부재)로 잡을 수 있나
# Y=숫자·리터럴 불일치로 잡힘, P=핵심어 부재로만 잡힘(오탐 위험), N=의미 판단(부정·양화·계산·관계) 필요
MANUAL = {
    "pub-285332b3": "Y",
    "pub-01d6a664": "Y",
    "pub-f692a941": "N",
    "pub-e92c712e": "Y",
    "pub-40d61f8c": "Y",
    "pub-3021b08c": "P",
    "pub-5c77fd7e": "P",
    "pub-66e1a648": "Y",
    "pub-92913558": "P",
    "pub-b4a1d8bb": "Y",
    "pub-9b188d93": "Y",
    "pub-a9f721b2": "Y",
    "pub-82c98cfa": "Y",
    "pub-603e42a3": "N",
    "pub-da798dcc": "Y",
    "pub-9b2c13b1": "P",
    "pub-94e2db73": "Y",
    "pub-bf65983e": "Y",
    "pub-270f0749": "Y",
    "pub-a51884fe": "Y",
    "pub-b9c40748": "Y",
    "pub-ab667cc1": "N",
    "pub-300f9442": "N",
    "pub-9b375ba2": "P",
    "pub-ebee188e": "N",
    "pub-1bfbf389": "Y",
    "pub-763823ae": "P",
    "pub-8c42c8dd": "P",
    "pub-0d043203": "Y",
    "pub-4e70e9a3": "N",
    "pub-f0554439": "P",
    "pub-ddbd0df5": "N",
    "pub-7fb64802": "N",
    "pub-d495c20a": "P",
    "pub-ceca1e47": "N",
}


def sections_with(kb, fact):
    nf = norm(fact)
    return sorted(sid for sid, t in kb.items() if nf in norm(t))


# 같은 사실이 여러 섹션에 반복되는 grounded 행: 그 사실을 담은 섹션 전부를 정답으로 인정
GROUNDED_FACTS = {
    "pub-110e57b7": "가상",
    "pub-f15ab968": "하루 이체 한도는 5만 원",
    "pub-78410d6c": "만 12세 이상",
    "pub-0d371526": "만 12세 이상",
    "pub-5333d544": "오전 6시",
    "pub-9b375ba2": None,
}


def gold_sections_grounded(kb, ans):
    """grounded 답변: 정규화 char-4gram 포함률 최대 섹션(≥0.9×최대)."""
    a = norm(ans)
    gs = {a[i : i + 4] for i in range(len(a) - 3)}
    sc = {}
    for sid, t in kb.items():
        nt = norm(t)
        sc[sid] = sum(g in nt for g in gs) / max(1, len(gs))
    m = max(sc.values())
    return sorted(s for s, v in sc.items() if v >= 0.9 * m), m


def lexical_check(kb, sids, span):
    """span의 숫자/영문/한글수사 토큰 중 정답 섹션에 없는 것이 있으면 '어휘로 잡힘'. 전 KB 기준도 함께."""
    toks = [t for t in NUM_RX.findall(span) if t.strip()]
    sec = "\n".join(kb[s] for s in sids)
    allkb = "\n".join(kb.values())
    miss_sec = [t for t in toks if norm(t) not in norm(sec)]
    miss_all = [t for t in toks if norm(t) not in norm(allkb)]
    # span 문자 2gram 중 정답 섹션에 없는 비율(새 내용 비율)
    ns, nsec = norm(span), norm(sec)
    bg = [ns[i : i + 2] for i in range(len(ns) - 1)]
    novel = sum(b not in nsec for b in bg) / max(1, len(bg))
    return toks, miss_sec, miss_all, novel


def main():
    kb = load_kb()
    rows = [r for r in guard_rows() if "candidate_answer" in r]
    ung = [r for r in rows if r["labels"]["category"] == "normal" and not r["labels"]["grounded"]]
    assert len(ung) == 35 and set(ANN) == {r["id"] for r in ung}

    print("## Q5-1 ungrounded 35행: 정답 위치·변조 유형·어휘 비교")
    print(
        "| id | 정답 섹션 | 유형 | 변조 | span 토큰 | 섹션에 없는 토큰 | 전 KB에도 없음 | 2gram 신규율 | 수작업 |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    types, lex_sec, lex_all, gold = Counter(), 0, 0, {}
    by_type_catch = Counter()
    for r in ung:
        fact, typ, desc = ANN[r["id"]]
        sids = sections_with(kb, fact)
        assert sids, (r["id"], fact)
        gold[r["id"]] = sids
        span = " ".join(r["labels"]["unsupported"])
        toks, ms, ma, novel = lexical_check(kb, sids, span)
        types[typ] += 1
        lex_sec += bool(ms)
        lex_all += bool(ma)
        by_type_catch[(typ, bool(ms))] += 1
        print(
            f"| {r['id']} | {','.join(sids)} | {typ} | {desc} | {toks} | {ms} | {ma} | {novel:.2f} | {MANUAL[r['id']]} |"
        )
    print(f"\n- 유형 분포: {dict(types.most_common())}")
    print(f"- 숫자·영문·수사 토큰이 정답 섹션에 없는 행(섹션 범위 어휘 비교로 잡힘): {lex_sec}/35")
    print(f"- 같은 토큰이 KB 전체 어디에도 없는 행(KB 전체 비교로 잡힘): {lex_all}/35")
    print(
        "- 수작업 판정:",
        dict(Counter(MANUAL.values())),
        "유형별:",
        dict(Counter((ANN[i][1], v) for i, v in MANUAL.items())),
    )
    print(
        "- 유형별 섹션 어휘 비교 적중:",
        {
            t: f"{by_type_catch[(t, True)]}/{by_type_catch[(t, True)] + by_type_catch[(t, False)]}"
            for t in types
        },
    )
    spans = [s for r in ung for s in r["labels"]["unsupported"]]
    print(f"- span 수 {len(spans)}, 행당 {Counter(len(r['labels']['unsupported']) for r in ung)}")
    print(
        f"- span이 문장 전체와 같은 행: {sum(any(s.strip() == r['candidate_answer'].strip() for s in r['labels']['unsupported']) for r in ung)}"
    )
    loc = Counter()
    for r in ung:
        a, s0 = r["candidate_answer"], r["labels"]["unsupported"][0]
        i = a.find(s0)
        loc["앞1/3" if i < len(a) / 3 else ("중간" if i < 2 * len(a) / 3 else "끝1/3")] += 1
    print(f"- 첫 span 위치: {dict(loc)}")
    cites = sum(bool(re.search(r"\[?(PY|PR|FIN)-\d\d-\d\d", r["candidate_answer"])) for r in rows)
    ucites = sum(bool(re.search(r"(PY|PR|FIN)-\d\d-\d\d", r["candidate_answer"])) for r in ung)
    print(f"- 후보 답변에 섹션 ID 인용이 붙은 행: {cites}/86 (ung {ucites}/35)")
    for r in rows:
        m = re.search(r"(PY|PR|FIN)-\d\d-\d\d", r["candidate_answer"])
        if m:
            g = gold.get(r["id"]) or gold_sections_grounded(kb, r["candidate_answer"])[0]
            print(
                f"  - {r['id']} 인용 {m.group(0)} / 정답 섹션 {g} / grounded={r['labels']['grounded']}"
            )

    # grounded normal 행의 정답 섹션(자동) — 수동 점검용 출력
    print("\n## Q5-2 grounded normal 32행 정답 섹션(4gram 포함률 자동 배정)")
    gr = [r for r in rows if r["labels"]["category"] == "normal" and r["labels"]["grounded"]]
    low = []
    for r in gr:
        sids, m = gold_sections_grounded(kb, r["candidate_answer"])
        if GROUNDED_FACTS.get(r["id"]):
            sids = sorted(
                set(sids)
                | set(sections_with(kb, GROUNDED_FACTS[r["id"]]))
                & {k for k in kb if k.startswith("FIN")}
            )
        gold[r["id"]] = sids
        if m < 0.6:
            low.append((r["id"], round(m, 2), sids))
    print(f"- {len(gr)}행, 포함률<0.6(자동 배정 불확실): {low}")

    # 같은 토큰 규칙을 답변 전체에 적용했을 때의 탐지/오탐 (정답 섹션을 안다고 가정한 상한)
    print("\n## Q5-2b 토큰 규칙(답변 전체 vs 정답 섹션) 혼동표 — 정답 섹션 오라클 가정")
    flag = {}
    for r in gr + ung:
        _, ms, _, _ = lexical_check(kb, gold[r["id"]], r["candidate_answer"])
        flag[r["id"]] = ms
    tp = sum(bool(flag[r["id"]]) for r in ung)
    fp = [(r["id"], flag[r["id"]]) for r in gr if flag[r["id"]]]
    print(f"- ung 35 중 경보 {tp}, grounded 32 중 경보(오탐) {len(fp)}: {fp}")

    # BM25
    print("\n## Q5-3 char-2gram BM25 섹션 적중률 (normal 후보 67행, 정답=위에서 찾은 섹션 집합)")
    ids = sorted(kb)
    bm_sec = BM25([kb[s] for s in ids])
    ch = kb_chunks(kb)
    bm_ch = BM25([c[2] for c in ch])
    targets = [r for r in rows if r["labels"]["category"] not in BLOCK_CATS]
    queries = {
        "마지막 user": lambda r: last_user(r),
        "전체 user": lambda r: all_user(r),
        "전체 대화(user+assistant)": lambda r: "\n".join(m["content"] for m in r["messages"]),
        "전체 user + 답변": lambda r: all_user(r) + "\n" + r["candidate_answer"],
        "답변만": lambda r: r["candidate_answer"],
    }
    print("| 질의 | 단위 | top-1 | top-3 | top-5 | (ung 35) top-1 | top-3 |")
    print("|---|---|---|---|---|---|---|")
    miss_examples = []
    for qn, qf in queries.items():
        for unit in ("섹션", "## 청크"):
            hits = {1: 0, 3: 0, 5: 0}
            uh = {1: 0, 3: 0}
            for r in targets:
                q = qf(r)
                if unit == "섹션":
                    sc = bm_sec.scores(q)
                    ranked = [ids[i] for i in sorted(range(len(ids)), key=lambda i: -sc[i])]
                else:
                    sc = bm_ch.scores(q)
                    ranked = list(
                        dict.fromkeys(
                            ch[i][0] for i in sorted(range(len(ch)), key=lambda i: -sc[i])
                        )
                    )
                g = set(gold[r["id"]])
                for k in hits:
                    hits[k] += bool(g & set(ranked[:k]))
                if not r["labels"]["grounded"]:
                    for k in uh:
                        uh[k] += bool(g & set(ranked[:k]))
                if qn == "전체 user + 답변" and unit == "## 청크" and not g & set(ranked[:3]):
                    miss_examples.append(
                        (r["id"], last_user(r)[:60].replace("\n", "/"), sorted(g), ranked[:3])
                    )
            n = len(targets)
            print(
                f"| {qn} | {unit} | {hits[1]}/{n} ({hits[1] / n:.0%}) | {hits[3] / n:.0%} | {hits[5] / n:.0%} | {uh[1]}/35 | {uh[3]}/35 |"
            )
    print("- top-3 실패(전체 user+답변, 청크):")
    for m in miss_examples:
        print("  -", m)

    # 현실 조건: 정답 섹션 대신 BM25(전체 user+답변, 청크) top-k 섹션을 참조로 토큰 규칙 적용
    for k in (1, 3):
        tpk, fpk = 0, 0
        for r in ung + gr:
            sc = bm_ch.scores(all_user(r) + "\n" + r["candidate_answer"])
            ref = list(
                dict.fromkeys(ch[i][0] for i in sorted(range(len(ch)), key=lambda i: -sc[i]))
            )[:k]
            _, ms, _, _ = lexical_check(kb, ref, r["candidate_answer"])
            if r["labels"]["grounded"]:
                fpk += bool(ms)
            else:
                tpk += bool(ms)
        print(
            f"- 토큰 규칙 + BM25 top-{k} 섹션 참조: ung 35 중 경보 {tpk}, grounded 32 중 오탐 {fpk}"
        )


if __name__ == "__main__":
    main()


def span_from_tokens():
    """토큰 규칙으로 잡힌 행에서 span을 어떻게 낼지: 토큰 포함 문장 vs 토큰±k자 vs 토큰만 (span F1)."""
    from common import scoring
    from q2_q4_scoring_sim import pad, sentences_with

    kb = load_kb()
    rows = [
        r
        for r in guard_rows()
        if "candidate_answer" in r
        and r["labels"]["category"] == "normal"
        and not r["labels"]["grounded"]
    ]
    res = {"토큰만": [], "토큰±4": [], "토큰±8": [], "토큰 포함 문장": []}
    for r in rows:
        sids = sections_with(kb, ANN[r["id"]][0])
        _, ms, _, _ = lexical_check(kb, sids, r["candidate_answer"])
        if not ms:
            continue
        a, gold = r["candidate_answer"], r["labels"]["unsupported"]
        hits = [t for t in ms if t in a]
        res["토큰만"].append(scoring.span_f1(hits, gold))
        res["토큰±4"].append(scoring.span_f1([pad(a, t, 4) for t in hits], gold))
        res["토큰±8"].append(scoring.span_f1([pad(a, t, 8) for t in hits], gold))
        res["토큰 포함 문장"].append(
            scoring.span_f1(list(dict.fromkeys(sentences_with(a, t) for t in hits)), gold)
        )
    print("\n## Q5-4 토큰 규칙으로 잡힌 행의 span 방식별 평균 span F1 (n=%d)" % len(res["토큰만"]))
    for k, v in res.items():
        print(f"- {k}: {sum(v) / len(v):.3f}")


if __name__ == "__main__":
    span_from_tokens()
