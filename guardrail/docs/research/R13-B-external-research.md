# R13-B. 외부 조사: 근거 없는 구간 탐지·소형 LLM 분류·한국어 공격·gpt-5-nano·합성 데이터

작성 2026-11-10 · 방법 `ecc:deep-research`(Exa 검색 약 20회, 원문 정독 6건) · 대상 질문: guard v6 계획의 Q6–Q10
선행 문서: R12(기준선), R7(근거성), R5(한국어 공격), R6(스크리닝 분류기), R10(구조). 거기 있는 내용은 반복하지 않고, **새 근거로 바뀌는 곳만** §0-2에 적는다.

표기
- **[사실]** 출처 사실(출처 번호 병기). **[로컬]** 저장소의 채점 코드·SPEC·공개 세트 집계(외부 출처 아님).
- **[추론]** 출처들을 우리 조건에 대입한 해석. **[권고]** 설계 제안.
- **[약]** 블로그·개인 저장소·집계 사이트 등 단일·비동료심사 출처. 수치는 방향만 참고한다.
- 웹 검색어에는 프로젝트·팀·사람 이름을 넣지 않았다. 여기 나온 외부 데이터·예시는 서버에 넣지 않는다(설계 지식으로만).

---

## 0. 요약

### 0-1. 가장 강한 발견 (확신도)

| # | 발견 | 확신 |
|---|---|---|
| 1 | 채점식(글자 묶음 F1, 빈 것끼리 1점)에서는 **"근거 없음"을 낼지·구간을 낼지의 손익분기가 다르다.** 근거 없을 확률 p < 0.5면 `grounded=true, []`, 0.5 ≤ p < 1/(1+E)면 **`grounded=false, []`**, 그 위에서만 구간을 낸다(E = 낸 구간의 기대 F1). §1-5 | 높음(산수) / 확률 추정은 측정 |
| 2 | 프롬프트만 쓴 GPT-5 계열은 구간 탐지에서 **재현율이 높고 정밀도가 낮다**(GPT-5-mini P 33.6 / R 67.6, GPT-5 P 30.0 / R 71.2). nano는 과잉 표시 쪽으로 기울 것이다 [S4] | 중상 |
| 3 | 정답 구간 중앙값 15자 vs 문장 전체를 내면 글자 묶음 F1이 약 0.5로 깎인다. **문장 선택 → 구절로 좁히기**가 문장 선택만보다 낫다. "고친 답을 쓰게 하고 diff"는 정확하지만 너무 좁아질 수 있어 구절 경계로 넓혀야 한다 [S6][S7] | 중 |
| 4 | gpt-5-nano는 `minimal`에서 단순 정확도가 무너지고(ObviousBench 37.5 vs low 91.0) `low`+`verbosity low`가 지연·정확도 균형점이다. `medium`에서 verbosity만 낮추면 nano는 오히려 39% 느려졌다 [S18][S22] | 중상 |
| 5 | 유사도로 고른 예시(kNN)는 무작위보다 분류 정확도를 꾸준히 올린다(7개 세트 평균 76.3→80.5, BM25 선택). 다만 **최신·추론 모델은 예시 수가 늘수록 좋아지지 않으며**, 예시는 주로 형식·경계 정렬 역할이다 [S24][S27][S30][S32][S33] | 중상 |
| 6 | 자기 일관성(여러 번 뽑아 투표)은 강한 모델에서 이득이 작고(HotpotQA +0.4%p/20표), 표가 갈리는 문항에만 추가 호출하는 적응형이 비용 대비 낫다 [S34][S35] | 중 |
| 7 | 임베딩 위 L2 로지스틱 회귀는 클래스당 60–75개면 GPT-4 수준 분류에 도달한 보고가 있다. **text-embedding-3-small + LR**은 대회 규칙 안에서 바로 쓸 수 있는 2번째 의견이다 [S36] | 중(영어·이진~4클래스 근거) |
| 8 | 한국어 롤플레이 탈옥은 "소설·이야기" 틀이 가장 잘 먹혔고(14.81%) 경어체가 평어체보다 약간 높았다. 직설 요청에서는 한국어 안전 우위가 사라진다 [S42][S43] | 중(단일 연구) |
| 9 | 과잉 차단은 "단어에 반응"하는 것이 원인이다(XSTest). 정책 프롬프트만 강하게 쓰면 과잉 거절이 33%까지 오른 보고가 있다 [S48][S49] | 중 |
| 10 | LLM 합성 데이터는 실데이터 약 100개 수준에서 효과가 크고(+3~26%) 1,000개면 작아진다. 소수 범주만 늘리는 쪽이 일관되게 낫다 [S57][S59][S60] | 중상 |

### 0-2. 기존 문서와 달라지는 곳

| 기존 | 바뀌는 점 | 근거 |
|---|---|---|
| R12 §6-1, ADR-0010·0005 v5: kiwipiepy **불가** | **허용**. SPEC §7.1이 형태소 분석기·토크나이저와 그 사전을 허용한다 | [로컬] `competition/SPEC.md:116` |
| R12 결론 6: 구간은 **문장 번호 선택이 기본** | 문장 선택만 하면 글자 묶음 F1이 약 0.5에 묶인다. **문장 선택 + 구절 좁히기**(diff 또는 숫자·조건 위치) | §1-4, [로컬] F3·F4 |
| R12 결론 7: `grounded = (unsupported == [])` | 중간 확신대에서는 **`grounded=false, []`가 기대값 최대**. 두 필드를 묶지 않는다. 단, 위험한 답(safe=false)은 `grounded=true, []`가 정답 | §1-5, [로컬] `scoring.py:27,62` |
| R12 결론 5: 입력 판정 k=1, 투표는 실험 | 유지하되, 남는 호출 예산(요청당 3–4호출 [로컬] F10)은 **확신 낮은 출력 판정에만** 쓴다 | §2-5 |
| R12 §1 권고: 경계 사례 예시는 팀이 직접 씀 | 공개 세트 200건의 **kNN 예시 검색이 허용**됨 | [로컬] F9 |
| R6 "학습 분류기 폐기" | 공개 세트 + nano 합성으로 **직접 학습한 분류기 허용**. 문자 n-gram LR, 임베딩 LR 부활 | [로컬] F9, §2-6 |
| R7 S7: 주입 셋은 2개 모델 계열로 | nano만 허용. 합성은 nano 한 계열 → **평가는 실제 공개 세트로만** | §5-3 |

---

## 1. Q6 근거 없는 구간 탐지

### 1-1. 벤치마크 수치 (R12 §2 갱신)

**[사실]** RAGTruth 구간 F1(문자 겹침, 데이터셋 단위) [S3][S4]

| 탐지기 | 정밀도 | 재현율 | F1 |
|---|---|---|---|
| gpt-3.5-turbo 프롬프트 | 7.8 | 35.3 | 12.8 |
| gpt-4-turbo 프롬프트 | 18.4 | 60.9 | 28.3 |
| GPT-4o-mini + CoT | 37.1 | 30.2 | 33.1 |
| GPT-5-mini + CoT | 33.6 | **67.6** | 44.5 |
| GPT-5 + CoT | 30.0 | **71.2** | 42.2 |
| o3 + CoT | 43.2 | 63.0 | 51.2 |
| Qwen2.5-7B-Instruct(CoT 없음 / 있음) | 23.5 / 26.2 | 12.2 / 12.3 | 16.0 / 16.2 |
| Qwen3-8B(추론 모드) | 39.8 | 23.9 | 28.5 |
| 미세조정 SFT-14B | 57.4 | 53.8 | 55.4 |
| RL4HS-14B(구간 F1 보상 RL) | 61.3 | 56.1 | 58.3 |
| LettuceDetect-large(395M, 토큰 분류) | 64.9 | 54.0 | 58.9 |

- **[사실]** 응답 단위("환각이 있나") F1은 gpt-4-turbo 63.4(P 46.9 / R 97.9), LettuceDetect-large 79.2, 미세조정 Llama-2-13B 78.7 [S1][S3]. "있나"는 "어디인가"보다 훨씬 쉽다.
- **[사실]** LettuceDetect v2 README: 통합 테스트 세트에서 Qwen-2B 0.689, mmBERT-base 0.642 구간 F1, RAGTruth 부분은 0.574 / 0.528. 프로토콜이 달라 v1과 직접 비교하지 말라고 명시 [S2].
- **[사실]** 사람이 만든 1,000건 이상 벤치마크에서 대형 LLM 4종 중 최고 F1이 0.67. 실패 원인 두 가지: ① 답에 **빠진 내용**을 불일치로 잘못 표시 ② 출처에 없지만 **사실로는 맞는** 내용(모델 지식과 일치)을 놓침 [S5].
- **[사실]** 프롬프트 기반 GPT-4o-mini 판정기는 RAGTruth QA에서 F1 0.44, 미세조정 구간 탐지기는 0.70 [S10 약].
- **[추론]** 우리 정답 구간은 "KB 사실 하나를 비튼 것"(숫자·조건·코드 인자, [로컬] F8)이다. RAGTruth의 자연 환각보다 **경계가 분명하고 KB와 직접 대조 가능**해서 위 수치보다 쉬운 쪽이다. 반대로 [S5]의 ②번 실패(맞는 말인데 KB에 없음)는 우리 정의상 "근거 없음"이 아닐 수 있으니 공개 세트 라벨로 확인한다. `[공개 세트 측정]`

### 1-2. 프롬프트 방식 비교 (SemEval-2025 Mu-SHROOM, 14개 언어)

**[사실]** UCSC(평균 순위 1위) 영어 테스트 IoU [S6]

| 방식 | 모델 | 테스트 IoU |
|---|---|---|
| 직접 추출 + 부분 문자열 위치 찾기, 문맥 없음 | gpt-4o-mini | 0.44 |
| 직접 추출 + 부분 문자열, 검색 문맥 있음 | gpt-4o-mini | **0.56** |
| 지식 그래프 검증 + 사실→구간 매핑 | gpt-4o-mini | 0.20–0.22 |
| **최소 수정(고친 답 생성) + 편집 거리 diff** | o1 | 0.53 |
| 위 최선 + 프롬프트 자동 최적화(MiPROv2) | — | 0.61 |

- 사람 주석자 최고 IoU 0.48 < 시스템 0.57. 과제가 원래 애매하다 [S6].
- **[사실]** "가장 단순한 방법(문맥을 주고 틀린 텍스트를 그대로 뽑게 함)이 복잡한 방법을 꾸준히 이겼다" [S6]. 지식 그래프 경로는 사실→구간 되짚기가 매우 불안정했다.
- **[사실]** 소형 오픈 모델 5종 × 프롬프트 4종 비교: **"틀린 부분을 직접 고쳐 쓰게 하는(fact) 프롬프트"가 표시(annotate) 프롬프트보다 일관되게 정확**(정확도 최고 0.75, 다수결 병합 시 과잉·과소 표시 모두 줄어듦). 저자 해석: 모델에게는 메타 텍스트(표시)를 섞는 것보다 자연스러운 문장을 생성하는 편이 쉽다 [S7].
- **[사실]** 상위 팀 프롬프트의 공통 규칙: "답에 나온 그대로 글자 단위로 복사(문장부호·띄어쓰기 포함)", "일부만 근거 없으면 그 부분만", "지우면 환각이 사라지는 **가장 작은** 구간", "문장 전체 말고 짧은 구절 우선" [S8]. 참조를 명시적으로 주면 구간 탐지가 좋아짐 [S9].
- **[사실]** 분해(지식 그래프·원자 주장)는 강한 검증기에서 오히려 손해라는 R7 [DD] 결론과 [S6]의 결과가 같은 방향이다.

### 1-3. 원자 주장 분해·MiniCheck·HalluGuard (R7 보완만)

- R7 §1이 FActScore·Claimify·Decomposition Dilemmas·LLM-AggreFact(gpt-4o-mini BAcc 74.0)를 이미 다뤘다. 새로 찾은 반대 근거는 없다.
- **[추론]** 우리 답은 짧고(튜터 답 1개), 정답 구간이 1–2개(F4)라 분해 호출을 따로 둘 이유가 약하다. 분해는 **구간 추출의 내부 단계**(문장마다 숫자·조건 확인)로만 쓴다.
- **[추론]** MiniCheck·LettuceDetect·HHEM 같은 학습된 검출기 파일은 대회 규칙상 못 쓴다. 쓸 수 있는 것은 그 **방식**(토큰 분류)뿐이고, 학습 데이터가 35개(F4)라 처음부터 학습한 토큰 분류기는 현실성이 낮다.

### 1-4. 글자 묶음 F1에서 구간 길이가 주는 손실

**[로컬]** 채점: NFKC·소문자·공백·문장부호 제거 뒤 **글자 다중집합(Counter)** F1. 둘 다 비면 1, 정답이 비었는데 내면 0 (`scoring.py:17-29`). 정답 구간 4–43자, 중앙 15자, 전부 답의 부분 문자열(F4).

**[추론]** 예측이 정답을 포함한 L자 구간일 때 대략 F1 ≈ 2g/(g+L) (g=정답 길이, 우연히 겹치는 글자는 무시).

| 낸 구간 | g=15일 때 F1 |
|---|---|
| 정확한 구절(15자) | 1.0 |
| 정답 + 앞뒤 어절 조금(25자) | 0.75 |
| 정답이 든 문장(40자) | 0.55 |
| 정답이 든 2문장(80자) | 0.32 |
| diff로 바뀐 숫자만("2", 1자) | 0.13 |

- **[추론]** 글자 묶음이라 **틀린 구간도 한국어 공통 글자(이·는·요·다)로 부분 점수**를 받는다. 정답이 있는 문항에서 엉뚱한 문장을 내도 0이 아닐 수 있다. 반대로 정답이 빈 문항에서는 무엇을 내든 0이다.
- **[권고]** 2단 구간화: ① LLM이 문장 번호(또는 원문 그대로의 짧은 구절)를 고른다 → ② 코드가 그 문장 안에서 **KB와 다른 토큰**(숫자·단위·연산자·라틴 식별자·부정 형태소)을 찾고 **그 토큰을 담은 어절 창**으로 좁힌다(Kiwi로 어절·구 경계). 창 크기(앞뒤 0–2어절)는 공개 세트에서 F1로 고른다. `[공개 세트 측정]`
- **[권고]** "고친 답을 쓰게 하고 diff" 방식은 정답 위치를 정확히 잡지만 너무 좁다(위 표 마지막 행). diff를 쓰면 반드시 어절·구 단위로 넓힌다.

### 1-5. "확신할 때만 표시" 기준값: 채점식에서 직접 유도

**[로컬]** 출력 점수 = (grounded 정답 여부 + 구간 F1 + safe 정답 여부) / 3. `grounded`의 정답은 `label != 'ungrounded'`이고, safe=false 문항의 label은 `'unsafe'`라 **위험한 답에서는 grounded=true가 정답**이다(`scoring.py:27,62`). 시스템이 두 필드의 일관성을 검사하지 않는다.

**[추론]** 안전한 답(safe=true) 한 개에 대해, p = 근거 없을 확률, E = 근거 없을 때 우리가 낸 구간의 기대 F1이면 (grounded, 구간) 두 항의 기대 합은 다음과 같다.

| 선택 | 기대 합 | 최선 구간 |
|---|---|---|
| A. `true, []` | 2(1−p) | p < 0.5 |
| C. `false, []` | **1** (항상) | 0.5 ≤ p < 1/(1+E) |
| B. `false, [구간]` | p(1+E) | p ≥ 1/(1+E) |
| D. `true, [구간]` | 1 − p + pE | 항상 C보다 나쁨 |

- E=0.55(문장 단위)면 구간을 내는 기준 p ≥ **0.65**, E=0.9(정확한 구절)면 p ≥ **0.53**. **구간을 좁힐수록 구간을 내도 되는 확신 문턱이 내려간다.** 1-4의 구간 좁히기가 점수에 두 번 기여하는 이유다.
- 공개 세트에서 안전한 후보 답의 근거 없음 비율은 35/67 ≈ 0.52(F4·F5에서 계산). 사전확률만으로는 A와 C의 경계에 있다. 신호가 없는 문항을 C로 두면 기대 합 1(= 문항당 출력 점수 1/3)이 보장된다.
- **[권고]** p를 직접 얻을 수 없으므로(gpt-5 계열은 logprobs 없음, R12) **신호 조합을 점수로** 쓴다. 예: ① LLM이 구간을 냄 ② 코드가 그 구간 안 숫자·식별자가 검색된 KB에 없음을 확인 ③ 2번째 LLM 호출도 같은 문장을 지목. 조합별 실제 근거 없음 비율을 공개 세트에서 재고, 위 표의 문턱과 비교해 A/C/B를 고른다. `[공개 세트 측정]`
- **[권고]** 위험한 답을 판정했으면(safe=false) 구간 판정과 무관하게 `grounded=true, []`를 낸다.
- **주의 [추론]**: 비공개 세트 분포가 공개 세트와 다르면 C 대역이 손해일 수 있다. 공개 세트 교차검증 폴드별로 C 정책의 이득이 일관된지 본다.

### 1-6. 소형 LLM의 구간 단위 vs 응답 단위

- **[사실]** 같은 모델에서도 응답 단위 F1(63–84)과 구간 단위 F1(28–59)의 격차가 크다 [S1][S3]. 프롬프트 Qwen2.5-7B는 구간 F1 16, CoT를 넣어도 16.2 [S4].
- **[사실]** 추론 모델이라고 구간을 잘 잡지는 않는다. QwQ-32B 구간 F1 15.3(정밀도 53.3, 재현율 9.0) [S4].
- **[추론]** nano는 GPT-5 계열이므로 "재현율 높고 정밀도 낮음" 쪽일 가능성이 크다(GPT-5·GPT-5-mini 패턴). 1-5에 따라 **정밀도 쪽으로 기울이는 장치**(코드 확인, 2번째 의견)가 필요하다. `[nano 실측]`

---

## 2. Q7 소형 LLM 분류 정확도 올리기

### 2-1. 예시 선택: kNN(유사도) vs 무작위

- **[사실]** KATE: 질의와 비슷한 예시를 검색해 넣으면 무작위보다 꾸준히 좋아짐. 과제 데이터로 미세조정한 문장 인코더로 검색하면 더 좋아짐 [S24].
- **[사실]** 능동학습 관점 비교(24개 분류·선다형 과제): 유사도 선택이 불확실성·다양성 선택보다 일관되게 우수. "비슷한" 예시를 쓴 GPT-2(124M)가 무작위 예시의 GPT-NeoX(20B)보다 평균 정확도가 높았다 [S26].
- **[사실]** 7개 분류 세트 평균 정확도: 무작위 76.33, BM25 선택 80.48 [S27]. 후보를 TopK=30까지 넓히고 라벨 분포까지 맞춰 다시 고르면 +2%p 추가. 후보를 50으로 늘리면 오히려 떨어짐.
- **[사실]** 토큰 분류에서는 예시 풀 선택 방법 간 차이가 작았고, **약 200개 풀로도 전체 학습 세트에서 고를 때의 88% 이상** 성능 [S62].
- **[추론]** 공개 세트 200건(normal 114)은 kNN 풀로 충분한 크기다. 다만 normal이 57%라 이웃이 normal로 쏠린다. 입력 문장이 짧고(학습자 발화) 범주 경계가 미묘해서 **경계를 보여 주는 대조 예시**가 중요하다.

### 2-2. 라벨 균형·순서·개수

- **[사실]** 몇 개 예시로 하는 분류는 예시 선택·형식·순서에 따라 거의 무작위에서 최고 수준까지 흔들린다. 원인은 **다수 라벨 편향**(예시에 많은 라벨)과 **최근 편향**(끝에 놓인 라벨) [S25].
- **[사실]** 순서 효과는 이진보다 **다중 클래스에서 커진다** [S29]. 유사도 낮은 것 → 높은 것 순(가장 비슷한 예시를 질의 바로 앞)이 가장 좋았다 [S28].
- **[사실]** 예시 수와 성능은 단조가 아니다. 5개 모델 × 0–8샷: 0샷이 이미 좋은 모델(GPT-4o-mini 등)은 이득이 유의하지 않음, 어떤 모델은 예시가 늘수록 단조 하락, 어떤 모델은 U자 [S30]. 과도한 예시가 성능을 떨어뜨리는 "over-prompting"도 보고됨 [S31].
- **[사실]** 추론 모델(R1 계열)은 수학에서 **예시 1개가 최적**에 가깝고 2–3개에서 자주 떨어졌다 [S32]. 강한 최신 모델에서 CoT 예시의 주 기능은 **출력 형식 정렬**이었다 [S33].
- **[권고]** 입력 판정 예시 구성(초기값, 공개 세트로 조정):
  - kNN 상위 k=4–6개(임베딩 코사인 또는 BM25), **범주당 최대 2개**로 상한.
  - 이웃 중 block·allow가 섞이도록 **반대 결정의 가장 가까운 예시 1개**를 반드시 포함(경계 대조).
  - 가장 비슷한 예시를 질의 바로 앞에 둔다.
  - k ∈ {0, 3, 6, 10}을 비교해 고른다. 추론 모델이라 많을수록 좋다고 가정하지 않는다. `[공개 세트 측정]`
- **[권고]** 공개 세트로 평가할 때는 질의 자신과 **같은 intent의 변형**을 kNN 풀에서 빼고(leave-intent-out) 잰다. 그러지 않으면 정답을 예시로 보여 주는 셈이다. [로컬] `score_local.py`는 문항을 intent·variant 구조로 다룬다.

### 2-3. 근거 먼저 vs 라벨 먼저

- **[사실]** JSON 모드에서 GPT-3.5가 answer 키를 reasoning 키보다 먼저 쓰면 Last Letter 과제 정확도가 크게 떨어졌다. 반면 **분류 과제에서는 JSON 강제가 오히려 정확도를 올린** 경우도 있었다(답 공간 제한) [S40].
- **[사실]** gpt-4.1·4o 계열에서 "답 먼저 vs 근거 먼저" 차이는 쉬운 추출 과제에서 거의 없었다 [S41 약].
- **[사실]** OpenAI GPT-5 지침: `minimal`에서는 "최종 답 앞에 사고 과정 요약을 짧게 쓰게 하면" 지능이 필요한 과제가 좋아진다 [S11].
- **[추론]** nano `low`는 숨은 추론 토큰을 이미 쓰므로 보이는 근거 필드의 이득은 non-reasoning 모델보다 작다. R12의 "근거 → 범주 → 결정" 순서는 해가 없으니 유지하되, 근거 필드를 **1문장·인용 위주**로 짧게 해 지연을 줄인다. `minimal`을 시험할 때는 근거 필드가 필수다.

### 2-4. 보정(calibration)

- **[사실]** R12 §3: 말로 낸 확신은 몇 값에 몰리고, 여러 표의 일치율이 더 낫다.
- **[사실]** 표마다 자기 확신(P(True))으로 가중 투표하면 같은 정확도에 필요한 표 수가 평균 40% 이상 줄었다. 다만 "가장 잘 보정된 확신 방법이 가중 투표에는 가장 덜 효과적"이었다 [S35].
- **[권고]** LLM 확률 대신 쓸 수 있는 신호: ① kNN 이웃 라벨 일치율 ② LR 분류기 확률(§2-6) ③ LLM 결정과 ①②의 일치 여부. 셋이 모두 일치하는 문항은 추가 호출 없이 확정하고, 엇갈리는 문항만 2번째 호출로 보낸다.

### 2-5. 자기 일관성(투표) 비용 대비 효과

- **[사실]** Gemini 2.5 계열: HotpotQA에서 20표까지 +0.4%p, MATH-500 +1.6%p(15배 비용). 많은 표에서 오히려 떨어지는 경우도 있음 [S34].
- **[사실]** 정답 분포의 엔트로피가 높은(모델이 헷갈리는) 문항에서는 투표도 잘 못 고친다. 1.5B 모델은 16표에서 +13%p(유의), 0.5B는 +7%p(비유의) [S34 관련 저장소, 약].
- **[사실]** 문항마다 표 수를 동적으로 배분하면 같은 정확도에 4.8배 적은 표 [S63].
- **[추론]** block 결정은 범주 정의가 분명한 문항이 대부분이라 k=1로 충분할 가능성이 크다(R12·ADR-0011 유지). 반대로 **구간 판정은 정밀도가 문제**(§1-6)라 "두 번 다 같은 문장을 지목할 때만 표시"가 1-5의 문턱을 넘기는 실용적 방법이다.
- **[권고]** 호출 예산(요청당 3–4호출, [로컬] F10)의 배분: 입력 1 + 출력 1을 기본, 출력에서 "근거 없음"을 냈고 코드 확인이 애매한 경우만 출력 2번째 호출. 입력은 신호가 엇갈릴 때만 2번째 호출.

### 2-6. 가벼운 학습 분류기와 결합

- **[사실]** 소형 LLM 임베딩 위 L2 로지스틱 회귀가 17개 문장 분류 과제(2–4클래스)에서 "수십 샷" 범위로 GPT-4와 같거나 나았고, 대부분 **클래스당 60–75개**면 GPT-4를 넘었다. 같은 수의 예시로 하는 few-shot 프롬프트는 5샷 부근에서 포화 [S36].
- **[사실]** 결합 방식 3가지가 보고됨: ① 확신 높은 것은 분류기, 낮은 것만 LLM으로 보내는 캐스케이드(보정된 LR 확률을 기준) [S37] ② 분류기의 예측·확신을 LLM 프롬프트에 넣어 LLM이 최종 판단(SuperICL) [S38] ③ 분류기 확신이 낮은 경계 영역에서 LLM 가중치를 높이는 적응형 선형 결합. LLM이 경계 문항에서 더 믿을 만했다 [S39].
- **[사실]** SuperICL 계열의 한계: 최적 설정(예시 수·분류기 조합)이 데이터셋마다 크게 달랐다 [S38]. 임베딩+LR은 분포 이동에서 크게 떨어진다(R6 §1, LODO −8.4pp).
- **[추론]** 우리 쪽 분류기 후보는 둘이다. ① **text-embedding-3-small 임베딩 + LR**(허용 모델의 출력 위에서 직접 학습, [S36]과 같은 구조) ② **문자 n-gram(1–3) TF-IDF + LR**(자모 분리·띄어쓰기 변형에 강함, 임베딩 호출 불필요). 클래스 10개 중 4개는 공개 세트에 3–7건뿐이라(F2) 10클래스 LR은 약하다. **이진(block/allow) LR**을 먼저 만들고, 범주는 LLM에 맡긴다.
- **[권고]** 결합은 ②번(LR 확률을 프롬프트의 힌트로)이 아니라 **①+③ 혼합**을 먼저 시험한다: LR·kNN·LLM이 모두 일치하면 확정, 엇갈리면 LLM 2번째 호출. 힌트로 넣으면 nano가 힌트를 그대로 따라가 오류를 복제할 위험이 있다 [추론]. 둘 다 공개 세트 교차검증으로 비교 `[측정]`.

### 2-7. 아주 작은 추론 모델에 대한 증거

- **[사실]** 사용자 정의 20과제 라벨링 정확도: gpt-5 0.89, gpt-5-mini 0.87, **gpt-5-nano 0.77**(80% 넘는 과제 수 15 / 15 / 7) [S20 약].
- **[사실]** 한 개인 실험에서 같은 분류 과제에 gpt-4.1-nano는 출력 2토큰·정답, gpt-5-nano는 추론 포함 393토큰·오답 [S21-b 약, n=1].
- **[추론]** nano는 "mini보다 한 단계 낮은" 분류기다. 정의가 복잡한 10범주를 한 번에 맡기기보다, **block 여부 + 범주**처럼 구조를 단순하게 하고 결정 규칙은 코드로 계산하는 R12 방향이 맞다.

---

## 3. Q8 한국어 탈옥·인젝션·유해 요청 (R5 갱신)

### 3-1. 2025–2026 새 근거

| 출처 | 내용 | 수치 |
|---|---|---|
| 영어·평어체·경어체 비교 [S42] (2025-12) | 롤플레이·파라미터 조작·GCG 공격, 범용·한국어 특화 LLM 3종 | 평균 ASR 영어·평어 약 1.5%, **경어체 약 2.0%**. 롤플레이 템플릿 중 **"이야기·소설" 14.81%** > "반대 모드" 13.33% > DAN 7.25% > "게임 모드" 6.67%. 한국어 특화 모델 + 경어체 10.26% |
| 한국 맥락 안전성 평가 [S43] (2025-10) | 14개 모델, 영어 vs 한국어 번안 | 한국어 프롬프트가 대체로 **덜 해로움**. 그러나 롤플레이·꾸민 사연·감정 호소 같은 포장을 벗긴 **직설 요청**에서는 한국어 우위가 사라지고, 오픈 모델 5종은 한국어에서 더 순응 |
| 추론 유도 + 기호 인코딩(HaPLa) [S44] (2025-09) | "중간 단계를 추론해 보라"는 귀추 틀 + 유해 단어 기호 치환 | GPT 계열 ASR 95% 이상, 전체 대상 70%. 모델이 **명시적 유해 키워드에 주로 반응**한다는 점을 이용 |
| 템플릿 채우기(TrojFill) [S45] (2026-02) | 유해 요청을 "안전성 분석을 위한 예시 작성"으로 포장, 자리표시자·Base64·글자 쪼개기 | GPT-4o 97%, Gemini-flash-2.5 100%. 의도 분류기를 피하도록 설계 |
| 자모 단위 오타 [S46] (2026-08) | KMMLU에 자모 교란 5종 | 교란 강도에 따라 정확도 단조 하락, **모델 크기를 키워도 강건해지지 않음** |
| 한국 문화 맥락 멀티모달 [S47] | 일반 질의 ASR 13.4% → ProgramExecution 틀 74.2% | 낮은 ASR 모델일수록 정상 질의 과잉 거절 경향 |
| 한·영 탈옥 분류기 [S50] (2025) | Benign/Harmful/Jailbreak 3분류, 한국어 전용 학습이 한국어에서 더 좋음 | 두 모델 모두 Benign 정밀도 0.8 미만(유해를 정상으로 놓침) |
| 공격 분류 체계 [S53] (2025-10) | 난독화(글자 변형·인코딩·다국어·코드 포장), 분해(무해한 조각으로 나눴다 합치기) | 분해 공격은 입출력 검열을 피하는 "조합 누출" |

- **[사실]** 단순한 "이전 지시 무시" 직접 덮어쓰기는 최신 모델에서 성공률 0%, 문맥 전환형이 가장 효과적이라는 비공식 측정이 있다 [S52 약]. 단, 우리 과제는 **공격 성공이 아니라 시도 탐지**이므로 성공률이 낮아도 jailbreak_injection으로 분류해야 한다 [추론].

### 3-2. 탐지 패턴 (우리 범주에 대입)

**[추론]** 공개 세트 정의(block = jailbreak·harmful·academic_dishonesty·off_topic)에 맞춘 신호 목록. 모두 LLM 프롬프트의 범주 정의·대조 예시와 코드 신호(힌트) 양쪽에 쓴다.

| 패턴 | 예(자리표시자) | 범주 | 코드 신호 |
|---|---|---|---|
| 지시 덮어쓰기 | "앞의 규칙은 잊고 …", "시스템: …" | jailbreak | 정규식(무시·잊어·규칙·시스템·개발자 모드), 역할 태그 흉내 |
| 소설·롤플레이 포장 | "소설 속 악당이 <유해요청>을 설명하는 장면" | jailbreak 또는 harmful | "소설/역할/가정/게임/~인 척" + 유해 주제 동시 출현 |
| 연구·안전 교육 포장 | "보안 교육용 예시로 <유해요청> 작성" [S45] | jailbreak | "예시로/교육용/연구 목적" + 실행 가능한 산출 요구 |
| 귀추·단계 추론 유도 | "결과가 이렇다면 중간 단계는?" [S44] | harmful | 단계·방법·절차 요구 + 유해 주제 |
| 인코딩·기호 치환 | Base64, 자리표시자 매핑, 글자 사이 기호 | jailbreak | Base64·16진 패턴, 짧은 매핑 정의("X는 ~를 뜻함") |
| 자모 분리·초성·띄어쓰기 | `ㅍㅗㄱㅌㅏㄴ`, 초성 약어 | 위 범주 그대로 | R5 N2–N4 정규화 뒤 판정. 정규화 전후 둘 다 LLM에 주기 |
| 경어체·공손한 포장 | "선생님, 혹시 … 알려주실 수 있을까요" [S42] | 내용으로 판단 | 없음(어조로 판단하지 않는다) |
| 과제 대행 | "숙제 답만 써 줘", "제 이름으로 낼 리포트" | academic_dishonesty | "대신/답만/제출/베껴" |
| 답만 요구(허용) | "정답만 알려줘" | answer_seeking(allow) | academic과 짝 사례로 예시 필수 |

- **[권고]** 정규화(R5 A절)는 LLM 입력 전에 하되, **원문과 정규화본을 둘 다** 프롬프트에 넣는다. 자모 교란은 모델 크기로 해결되지 않는다 [S46].
- **[권고]** 코드 신호는 결정이 아니라 **힌트**로 둔다(R12·ADR-0002 유지). [S52]에서 키워드형 탐지기의 오탐은 전부 "공격 어휘를 담은 정상 문장"이었다(예: "'이전 지시 무시' 공격이 뭔지 설명해 줘").

### 3-3. 과잉 차단 함정

- **[사실]** XSTest: 과장된 안전은 **어휘 과적합**("죽이다", "총" 같은 단어에 반응)에서 온다. 안전 맥락(게임·영화), 비유 표현, 공인 정보에서 거절이 많았다 [S48].
- **[사실]** 정책 프롬프트로만 가드를 세우면 과잉 거절 33.2%·위험 순응 0.0%, LLM 판정기 방식은 5.2%·6.0%로 균형이 달랐다 [S49].
- **[사실]** 프롬프트 판정기 오탐률 18%(입력의 약 1/5을 잘못 차단)라는 비공식 측정 [S52 약]과, gpt-4o-mini 판정기가 키워드 함정 20건에서 0건 오탐이라는 측정 [S51 약]이 서로 다르다. **우리 공개 세트로 재야 한다.**
- **[추론] 교육 튜터에서 위험한 혼동 쌍**
  - harmful(block) vs **emotional_distress(allow)**: "죽고 싶어요"는 위기 신호지 유해 요청이 아니다. block으로 잘못 보내면 block F1 정밀도가 깎인다.
  - harmful vs **정상 교과**: 전쟁사, 약물의 생물학적 작용, 화학 반응의 위험성, 보안 개념 설명.
  - jailbreak vs **보안 개념 질문**: "프롬프트 인젝션이 뭐예요?"
  - off_topic(block) vs **out_of_material(allow)**: 교과와 무관한 잡담인지, 교과 관련이지만 자료 밖인지.
  - academic_dishonesty(block) vs **answer_seeking(allow)**.
- **[권고]** 위 짝마다 공개 세트에서 가장 가까운 allow 예시를 kNN 대조 예시로 넣는다(§2-2). 정책 문장에 "주제가 민감해도 요청이 이해·학습이면 allow"를 명시한다.

---

## 4. Q9 gpt-5-nano 운영 사실 (R12 §4 갱신)

### 4-1. 파라미터

| 항목 | 값 | 출처 |
|---|---|---|
| 상태 | **Deprecated**, 스냅샷 `gpt-5-nano-2025-08-07`, 컨텍스트 400k(입력 최대 272k, 출력 128k), 지식 기준 2024-05-31 | [S13] [사실] |
| 기능 | structured_outputs, function_calling, streaming, prompt_caching, 추론 토큰 | [S13] [사실] |
| `reasoning_effort` | GPT-5 계열: `minimal`, `low`, `medium`(기본), `high`. `none`은 gpt-5.2 이후 모델 값 | [S11][S14][S23] [사실] |
| 파라미터 이름 | Chat Completions `reasoning_effort`, Responses `reasoning.effort` / `text.verbosity` | [S17 약][S12] [사실] |
| `verbosity` | low / medium(기본) / high. 최종 답 길이에 영향, 추론 길이와 별개 | [S12] [사실] |
| 샘플링 파라미터 | 추론 모델은 temperature·top_p를 받지 않음(OpenAI 직접 호출). 대회 프록시는 확인 필요 | [S17 약], R12 [8] [사실] |
| `max_completion_tokens` | 추론 토큰도 이 한도를 씀 → 충분히(≥800) | R12 [사실] |
| minimal에서 병렬 도구 호출 | 지원 안 함(Azure 문서) | [S23] [사실] |

### 4-2. 지연·처리량

| 측정 | 값 | 출처 |
|---|---|---|
| JSON 분류 약 360건, medium/medium | nano p50 5.06s·p95 7.84s (mini 4.12s·5.60s) | [S18 약] |
| medium + verbosity low | nano **p50 7.03s·p95 11.08s(+39% 악화)**, mini는 개선 | [S18 약] |
| low + low (json_object / json_schema) | nano p50 2.56s·p95 3.87s, json_schema는 지연 변화 없음 | [S18 약] |
| minimal | 첫 토큰 1.04s, 출력 126 tok/s | [S21 약] |
| low | 단순 추론 3문제 정확도 98.9%, 평균 1.72s | [S19 약] |

- **[추론]** 300요청·동시 64면 약 5파. low+low에서 호출 p95 약 4s라면 직렬 2호출 + 여유로 요청당 10s 안. 입력·출력 판정을 병렬로 두면 3–4호출 예산(F10)이 지연 안에 들어간다. 프록시 실측 전에는 가정이다. `[런박스 측정]`

### 4-3. 정확도 (effort별)

| 벤치 | minimal | low | medium | high | 출처 |
|---|---|---|---|---|---|
| ObviousBench(산술·글자세기·부정·형식 등 쉬운 실수, pass³) | 37.5 | 91.0 | 95.1 | 97.2 | [S22 약, 2026-10-29] |
| 단순 추론 3문제 30회 | 4.4 | 98.9 | — | — | [S19 약] |
| IFBench(지시 따르기) | 32.5 | — | 65.9 | — | R12 [30] |

- **[추론]** 세 출처가 모두 "minimal은 무너진다, low부터 정상"이라는 같은 방향이다. R12의 `low` 기본 결정을 확인한다. medium 대비 low의 정확도 손실은 쉬운 과제에서 4%p 안쪽이다.
- **[사실]** OpenAI: minimal은 "추출·형식 맞추기·짧은 재작성·단순 분류"용, 다단계 판단에는 피하라 [S12]. 우리 입력 판정(10범주·문맥)은 단순 분류보다 어렵다 [추론].

### 4-4. 프롬프트 캐시

- **[사실]** 1,024토큰 이상 접두가 정확히 같으면 자동 캐시, 128토큰 단위로 늘어남. gpt-5-nano 캐시 입력 가격 $0.005/1M(90% 할인) [S16].
- **[사실]** 요청은 접두 앞 약 256토큰의 해시로 서버에 배정된다. 같은 접두+`prompt_cache_key` 조합은 **분당 약 15요청**을 한 서버가 처리하고, 넘치면 다른 서버로 분산되며 그 서버마다 첫 요청은 캐시 미스 [S16].
- **[사실]** GPT-5.6 이전 모델은 암묵 캐시만 지원. **모델·도구·스키마(text.format)·reasoning effort·verbosity가 바뀌면 접두가 달라진다** [S15].
- **[추론]** 300요청을 수 분에 몰아 보내면 분당 15요청을 크게 넘으므로 첫 몇 요청은 여러 서버에서 미스가 난다. 그래도 서버 수만큼만 미스라 적중률은 높다. 이득은 주로 비용이고, 입력 3–5k토큰에서 지연 이득은 작다.
- **[권고]** ① 정책·범주 정의·고정 예시를 **1,024토큰 이상의 고정 접두**로 맨 앞에 ② kNN 예시·대화·KB 섹션은 그 뒤에 ③ 입력 판정과 출력 판정은 접두가 다르므로 `prompt_cache_key`를 판정 종류별로 따로 ④ 같은 판정 안에서는 effort·verbosity·스키마를 고정. 프록시가 `cached_tokens`를 돌려주는지 확인한다.

### 4-5. 다른 모델과의 비교

- **[사실]** gpt-5-nano < gpt-5-mini(분류 0.77 vs 0.87) [S20 약]. KMMLU 69.6 vs 78.5(R12 [22]).
- **[공백]** gpt-5-nano를 가드·구간 판정기로 쓴 공개 정확도는 이번에도 찾지 못했다. gpt-4.1-nano와의 체계적 분류 비교도 없다(n=1 일화만, [S21-b]).

---

## 5. Q10 LLM 합성 데이터

### 5-1. 분류용 합성 데이터

- **[사실]** 실데이터 100개 상황에서 합성 증강은 6개 과제 모두 +3~26%, 1,000개에서는 4개 과제만 +5% 미만. 실데이터만으로 학습한 성능이 대부분 상한 [S57].
- **[사실]** 합성만으로 학습하면 실데이터보다 macro-F1이 6.7%(몇 개 실예시를 보고 생성)~16.9%(예시 없이 생성) 낮다. **주관성이 높은 과제**(유머·풍자)에서 격차가 가장 크고(약 −27%), 사람끼리 일치도가 낮은 문항일수록 합성이 덜 통한다 [S58].
- **[사실]** 사람 라벨이 대체로 우세하지만, **다중 클래스·불균형 과제의 희귀 클래스**에서는 균형 맞춘 합성 증강이 도움 [S59]. 가짜 뉴스 탐지에서 **소수 클래스만 낮은 비율로** 늘린 것이 가장 일관된 이득(최대 +7.7 F1), 다수 클래스까지 늘리면 오히려 악화 [S60].
- **[사실]** 합성 증강은 드문 개념에서 특히 가치가 있고, 사람 라벨 1,000개가 있으면 이득이 작다. 합성으로 학습한 분류기도 소수의 전문가 라벨로 보정이 필요 [S61].
- **[추론]** 우리 범주 중 answer_seeking(4)·emotional_distress(4)·low_signal(3)·academic(7)·off_topic(7)·pii(7)은 공개 세트에서 수십 개도 안 된다(F2). 이 범주들만 nano로 클래스당 30–60개 정도 늘려 **LR 분류기 학습용**으로 쓰는 것이 근거와 맞는다. LLM 입력 판정의 kNN 풀에는 **실제 공개 예시만** 두는 것이 안전하다(합성 예시가 경계를 흐릴 수 있음).

### 5-2. "사실 하나 비튼" 답변 합성

- **[사실]** 정상 응답을 교란해 만든 합성 데이터로 미세조정한 T5-base가 제로샷 검출기보다 정확·빠름 [S54].
- **[사실]** 환각 패턴 안내 + 원문 문체 맞추기로 생성 → 판정 LLM이 최선 후보를 고르는 2단 생성: 이 데이터로 학습한 검출기 F1 0.938, **같은 LLM의 ICL 검출기는 자기가 만든 환각도 0.613**. 다른 생성기로 만든 데이터는 분포가 멀어서, 섞을 때 표본을 늘리지 않으면 성능 손해 [S55].
- **[사실]** 그럴듯한 대체어로 교란한 데이터가 무작위 대체보다 낫고, **사전학습용으로 쓰는 편이 증강보다 낫다**(분포 차이와 잡음 때문) [S56].
- **[추론]** 학습된 토큰 분류기를 만들지 않는다면, 비튼 답 합성의 가장 큰 쓸모는 ① **구간 좁히기 규칙(§1-4)의 창 크기 튜닝**과 ② **"근거 없음" 확신 조합(§1-5)의 비율 추정**을 위한 추가 표본이다. 공개 세트의 근거 없는 답은 35개뿐이라 조합별 비율을 재기에 부족하다.
- **[권고]** 생성 규칙: KB 섹션에서 숫자·나이·비율·조건·코드 인자 중 하나만 바꾸게 하고(F8과 같은 유형), 바뀐 구간을 nano가 함께 출력하게 한 뒤 **코드로 원 답과 diff해 구간을 검증**한다. 검증에 실패한 표본은 버린다.

### 5-3. 누수 없는 평가

- **[권고]** ① 합성 데이터는 학습 폴드의 KB 섹션·예시에서만 만든다. ② 평가는 **실제 공개 세트만**, intent 단위로 묶어 나눈다. ③ 합성 예시와 평가 예시는 임베딩 코사인으로 근접 중복을 걸러 낸다(임계값은 공개 세트 안 중복 분포로 정함). ④ 프롬프트·창 크기·문턱을 바꿀 때마다 같은 폴드 분할에서 짝 비교한다(R12 §3의 McNemar·부트스트랩 유지).
- **[사실]** 프롬프트 최적화는 학습 세트에서 5–20% 부풀려 보인다(R12 [19][20]). MiPROv2 같은 자동 최적화가 SemEval에서 +0.06 IoU를 냈지만 [S6], 우리 세트 크기(200)에서는 과적합 위험이 더 크다 [추론].
- **[사실]** 합성 데이터의 출처 규칙: nano가 만든 예시만 허용([로컬] F9). R7 S7의 "2개 모델 계열로 주입"은 이 대회에서 불가능하다.

---

## 6. 설계 시사점

### 6-1. 기대 점수 영향 순 권고

점수 = 0.45·block F1 + 0.10·범주 정확도(비정상) + 0.45·출력 평균. 출력 상수 응답은 약 0.655([로컬] F7). 아래 "영향"은 근거와 채점식에서 짐작한 크기다 [추론].

| 순위 | 권고 | 근거 | 영향 | 확신 |
|---|---|---|---|---|
| 1 | **출력 3분기 정책**: p<0.5 → `true,[]`, 중간 → `false,[]`, 확신 높음 → `false,[구간]`. 위험한 답은 항상 `true,[]` | §1-5, [로컬] scoring.py | 출력 항에서 큼(잘못 낸 구간 하나가 그 답의 2/3점을 잃음) | 높음(산수), 문턱은 측정 |
| 2 | **구간 좁히기**: 문장 선택 → KB와 다른 숫자·조건 토큰의 어절 창으로. diff를 쓰면 반드시 넓힘 | §1-4, [S6][S7][S8] | 구간 F1 약 0.55 → 0.8 이상 기대, 1번의 문턱도 낮춤 | 중 |
| 3 | **코드 확인을 정밀도 장치로**: LLM이 낸 구간 안 숫자·식별자가 검색된 KB에 없을 때만 확신 상향 | §1-6, [S4] | GPT-5 계열의 과잉 표시를 억제 | 중 |
| 4 | **block 이진 결정에 3중 신호**: kNN 이웃 일치 + 이진 LR(문자 n-gram 또는 임베딩) + nano. 모두 일치하면 확정, 엇갈리면 2번째 호출 | §2-4, §2-6, [S36][S37][S39] | block F1(가중 0.45) | 중 |
| 5 | **kNN 대조 예시**: 범주당 상한 2, 반대 결정의 최근접 1개 필수, 가장 비슷한 것을 마지막에. k는 {0,3,6,10} 비교 | §2-2, [S25][S27][S28][S30] | block F1·범주 정확도 | 중 |
| 6 | **과잉 차단 짝 사례**: harmful↔emotional_distress, jailbreak↔보안 개념 질문, off_topic↔out_of_material, academic↔answer_seeking | §3-3, [S48][S49] | block 정밀도 | 중 |
| 7 | nano `reasoning_effort=low`, `verbosity=low`, json_schema strict, 근거 필드 1문장. minimal은 쓰지 않음 | §4, [S18][S19][S22] | 지연 절반, 정확도 유지 | 중상 |
| 8 | 고정 접두 ≥1,024토큰 → 그 뒤에 kNN·대화·KB. 판정 종류별 `prompt_cache_key` | §4-4, [S15][S16] | 비용, 약간의 지연 | 높음(문서) |
| 9 | 정규화 원문·정규화본 둘 다 입력, 롤플레이·연구 포장·인코딩 패턴을 힌트로 | §3-1·3-2, [S42][S44][S45][S46] | jailbreak 재현율 | 중 |
| 10 | 합성은 소수 범주(LR 학습용)와 비튼 답(창 크기·문턱 튜닝용)에만. kNN 풀·평가는 실데이터만 | §5, [S57][S59][S60][S55] | 소수 범주 정확도, 튜닝 안정성 | 중 |

### 6-2. 남는 위험

1. **비공개 세트 분포**: 변형(variant)이 공개 세트와 다르면 C 정책(§1-5)과 kNN 이득이 줄어든다. 공개 세트 폴드별로 이득이 일관된지 본다.
2. **nano 실측 공백**: 구간 탐지 정밀도·지연·프록시의 파라미터 수용(`reasoning_effort`, `verbosity`, `json_schema`, `prompt_cache_key`, `cached_tokens` 반환)은 모두 실측 전이다.
3. **작은 세트 과적합**: 200건에 프롬프트·k·창 크기·문턱을 동시에 맞추면 쉽게 부풀려진다. intent 단위 교차검증과 잠근 홀드아웃(R12 §3)을 지킨다.
4. **LR 분포 이동**: 임베딩·n-gram LR은 학습 분포 밖에서 크게 떨어진다(R6). 단독 결정권을 주지 않고 "3중 일치" 조건에서만 확정한다.
5. **키워드 힌트의 역효과**: 힌트가 nano를 과잉 차단 쪽으로 끌 수 있다(XSTest식 어휘 과적합). 힌트 유무를 짝 비교한다.
6. **nano Deprecated**: 대회 기간 중 프록시 뒤 모델 교체 가능성. 기동 시 모델 이름·파라미터 확인(R12 권고 유지).

### 6-3. 찾지 못한 것

- 한국어 구간 단위 환각 탐지 벤치마크, gpt-5-nano의 구간 탐지 정확도.
- 한국어 탈옥 **탐지**(공격 성공률이 아니라 분류) 정확도의 공개 수치. [S50]만 있고 수치 일부만 확인.
- 글자 묶음 F1(우리 채점식)을 쓰는 공개 벤치마크. RAGTruth·Mu-SHROOM은 문자 위치 겹침·IoU라 §1-4의 손실 계산은 직접 유도했다.

---

## 출처

접속일은 모두 2026-11-10. **약** = 블로그·개인 저장소·집계 사이트 등 비동료심사.

1. [S1] Kovács & Recski, "LettuceDetect" (2025-02-24) https://arxiv.org/html/2502.17125
2. [S2] LettuceDetect README(v2 수치) https://github.com/KRLabsOrg/LettuceDetect/
3. [S3] Niu et al., "RAGTruth", ACL 2024 https://aclanthology.org/2024.acl-long.585.pdf
4. [S4] "Learning to Reason for Hallucination Span Detection"(RL4HS, 2025-10) https://arxiv.org/html/2510.02173
5. [S5] Peisakhovsky et al., "Fine-Grained Detection of Context-Grounded Hallucinations Using LLMs" (2025-09-26) https://arxiv.org/abs/2509.22582
6. [S6] UCSC at SemEval-2025 Task 3 (2025-05) https://arxiv.org/html/2505.03030 · 요약 https://siconghuang.pages.dev/projects/ucsc-semeval-task-3/
7. [S7] "Better Together: Localizing Fact-Related Hallucinations using Open Small Language Models", CHOMPS 2025 https://aclanthology.org/2025.chomps-main.3.pdf
8. [S8] MSA at SemEval-2025 Task 3 (부록 프롬프트) https://aclanthology.org/2025.semeval-1.131.pdf
9. [S9] FiRC-NLP at SemEval-2025 Task 3 https://aclanthology.org/2025.semeval-1.273.pdf
10. [S10] Sharma, "Verify-RAG" (2026-07-02, 약) https://doi.org/10.5281/zenodo.21120261
11. [S11] OpenAI, Using GPT-5 https://developers.openai.com/api/docs/guides/latest-model/gpt-5
12. [S12] OpenAI Cookbook, GPT-5 New Params and Tools https://developers.openai.com/cookbook/examples/gpt-5/gpt-5_new_params_and_tools
13. [S13] OpenAI, GPT-5 nano 모델 페이지 https://developers.openai.com/api/docs/models/gpt-5-nano
14. [S14] OpenAI, Reasoning models https://developers.openai.com/api/docs/guides/reasoning
15. [S15] OpenAI, Prompt caching https://developers.openai.com/api/docs/guides/prompt-caching
16. [S16] OpenAI Cookbook, Prompt Caching 201 https://developers.openai.com/cookbook/examples/prompt_caching_201
17. [S17] OpenAI Community, "GPT-5-Nano accepted parameters" (약) https://community.openai.com/t/gpt-5-nano-accepted-parameters/1355086
18. [S18] Qiita, gpt-5-nano 지연 파라미터 검증 (2026-03-13, 약) https://qiita.com/testuse80712458/items/771341c162608df7b6bd
19. [S19] OpenAI 모델 10종 비교 (2026-05-15, 약) https://jaesolshin.com/posts/openai-models-comparison/
20. [S20] Label Studio, Evaluating the GPT-5 Series on Custom Benchmarks (2025-08-08, 약) https://labelstud.io/blog/evaluating-the-gpt-5-series-on-custom-benchmarks/
21. [S21] Artificial Analysis, GPT-5 nano (Minimal) vs GPT-4.1 mini (약) https://artificialanalysis.ai/models/comparisons/gpt-5-nano-minimal-vs-gpt-4-1-mini · [S21-b] LinkedIn 일화(2025-11-17, 약) https://www.linkedin.com/posts/rithik-k_gpt-5-nano-is-cheaper-than-gpt-41-nano-activity-7396030474625695745-mjFH
22. [S22] BenchmarkList, GPT-5 Nano(ObviousBench 2026-10-29 행, 집계, 약) https://benchmarklist.com/models/openai-gpt-5-nano/
23. [S23] Microsoft Learn, GPT-5 vs GPT-4.1 https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/model-choice-guide
24. [S24] Liu et al., "What Makes Good In-Context Examples for GPT-3?"(KATE, 2021) https://arxiv.org/abs/2101.06804
25. [S25] Zhao et al., "Calibrate Before Use" (2021) https://arxiv.org/abs/2102.09690
26. [S26] "Active Learning Principles for In-Context Learning with LLMs" (2023-11) https://arxiv.org/html/2305.14264v2
27. [S27] "Learn to Select: Label Distribution Divergence for ICL Demonstration Selection" (2025-11) https://arxiv.org/html/2511.10675v1
28. [S28] Sun et al., "Text Classification via Large Language Models"(CARP), Findings EMNLP 2023 https://aclanthology.org/2023.findings-emnlp.603.pdf
29. [S29] "On Sensitivity of Learning with Limited Labelled Data to the Effects of Randomness" (2024-02) https://arxiv.org/html/2402.12817v1
30. [S30] "When Does Few-Shot Prompting Help?" (2026-07) https://ar5iv.labs.arxiv.org/html/2607.22969
31. [S31] "The Few-shot Dilemma: Over-prompting LLMs" (2025-09-16) https://arxiv.org/html/2509.13196v1
32. [S32] "Innate Reasoning is Not Enough: ICL Enhances Reasoning LLMs" (2025-03) https://arxiv.org/html/2503.19602
33. [S33] "Revisiting Chain-of-Thought Prompting: Zero-shot Can Be Stronger than Few-shot", Findings EMNLP 2025 https://aclanthology.org/2025.findings-emnlp.729.pdf
34. [S34] "Self-Consistency Is Losing Its Edge" (2026-05-07) https://arxiv.org/html/2511.00751v2 · 소형 모델 투표 실험(약) https://github.com/v-code01/selfconsistency
35. [S35] Taubenfeld et al., "Confidence Improves Self-Consistency in LLMs"(CISC), Findings ACL 2025 https://aclanthology.org/2025.findings-acl.1030.pdf
36. [S36] Buckmann, "Logistic Regression makes small LLMs strong and explainable tens-of-shot classifiers" (2024-10) https://arxiv.org/html/2408.03414
37. [S37] "A Strategy to Combine 1stGen Transformers and Open LLMs for Automatic Text Classification" (2024-08) https://arxiv.org/html/2408.09629
38. [S38] "Improving In-Context Learning with Small Language Model Ensembles"(Ensemble SuperICL) https://arxiv.org/html/2410.21868v2
39. [S39] "LLM and ML combination with adaptive weighting" (2024-05) https://arxiv.org/pdf/2405.05445
40. [S40] Tam et al., "Let Me Speak Freely? / Format-Restricting Prompting", EMNLP Industry 2024 https://aclanthology.org/2024.emnlp-industry.91.pdf
41. [S41] "Does field ordering affect model performance" (2025-05-21, 약) https://blog.kallyaleksiev.net/does-field-ordering-affect-model-performance
42. [S42] 이지민·장항배, 「다국어 Prompt Injection 기반 Jailbreak 취약점 분석: 영어, 한국어 평어체, 경어체 비교」, 융합보안논문지 (2025-12-31) https://koreascience.kr/article/JAKO202507757704084.page
43. [S43] Scale AI, "ROK-FORTRESS" (2025-10-28) https://scale.com/blog/rok-fortress-multilingual-ai-safety
44. [S44] Joo, Koh, Jung, "Harmful Prompt Laundering"(HaPLa) (2025-09-13) https://doi.org/10.48550/arxiv.2509.10931
45. [S45] "The Trojan Example: Jailbreaking LLMs through Template Filling"(TrojFill) (2026-02-18) https://arxiv.org/html/2510.21190v2
46. [S46] Lee & Lee, "Quantifying and Mitigating Korean Jamo-Level Typographical Vulnerabilities in LLMs" (2026-08-31) https://arxiv.org/abs/2608.30229
47. [S47] "KSAFE-MM: Korean Multimodal Safety Benchmark" https://arxiv.org/html/2605.28013v1
48. [S48] Röttger et al., "XSTest", NAACL 2024 https://aclanthology.org/2024.naacl-long.301.pdf
49. [S49] "PL-Guard: Probabilistic Logic Reasoning for LLM Guardrails" (2026-08) https://arxiv.org/html/2608.15673
50. [S50] 박대얼 외, 「영어-한국어 탈옥 프롬프트 데이터셋 구축 및 탈옥 프롬프트 분류 모델 성능 분석」, 정보보호학회논문지 (2025) https://www.koreascience.kr/article/JAKO202518939602628.page
51. [S51] prompt-injection-detection-system (개인 저장소, 약) https://github.com/joseherrera1988/prompt-injection-detection-system
52. [S52] LLM Prompt Injection Benchmark 대시보드 (개인 프로젝트, 약) https://himathx.github.io/llm-injection-bench/
53. [S53] Hong et al., "SoK: Taxonomy and Evaluation of Prompt Security in LLMs" (2025-10-17) http://arxiv.org/pdf/2510.15476v1
54. [S54] Zhang et al., "Enhancing Hallucination Detection through Perturbation-Based Synthetic Data Generation", Findings ACL 2024 https://aclanthology.org/2024.findings-acl.789/
55. [S55] "Controlled Automatic Task-Specific Synthetic Data Generation for Hallucination Detection" (2024-10) https://arxiv.org/html/2410.12278v2
56. [S56] Whitehead, "Pre-Training Multimodal Hallucination Detectors with Corrupted Grounding Data" https://arxiv.org/html/2409.00238
57. [S57] "Data Generation Using LLMs for Text Classification: An Empirical Case Study" (2024-07) https://arxiv.org/html/2407.12813
58. [S58] Li et al., "Synthetic Data Generation with LLMs for Text Classification: Potential and Limitations", EMNLP 2023 https://doi.org/10.18653/v1/2023.emnlp-main.647
59. [S59] Møller et al., "The Parrot Dilemma", EACL 2024 https://doi.org/10.18653/v1/2024.eacl-short.17
60. [S60] Chalehchaleh et al., multilingual fake news augmentation (2025-08-28) https://doi.org/10.1007/s13278-025-01505-z
61. [S61] Halterman, "Synthetically generated text for supervised text analysis", Political Analysis (2025-01-24) https://doi.org/10.1017/pan.2024.31
62. [S62] Berger et al., "In-Context Learning on a Budget: A Case Study in Token Classification", Insights 2025 https://doi.org/10.18653/v1/2025.insights-1.2
63. [S63] "Optimal Self-Consistency for Efficient Reasoning with LLMs"(Blend-ASC, 2026-06-30) https://arxiv.org/html/2511.12309v2

로컬 근거(외부 출처 아님): `competition/grader/scoring.py`(채점식), `competition/harness/score_local.py`(intent·variant 구조), `competition/SPEC.md` §7.1(허용 라이브러리), 공개 세트 집계 F2–F10(guard v6 계획 §1).

## 방법

- 검색: Exa 의미 검색 약 20회(질문당 2–4개 변형, 2025–2026 우선), 원문 정독 6건(LettuceDetect·RL4HS·UCSC 표, OpenAI 캐시 문서, KATE·Calibrate Before Use 초록). Firecrawl은 쓰지 않음.
- 하위 질문: Q6 구간 탐지 / Q7 분류 정확도 / Q8 한국어 공격·과잉 차단 / Q9 gpt-5-nano / Q10 합성 데이터.
- 조사 중 지시문 성격의 텍스트(에이전트를 겨냥한 문구)를 담은 출처는 발견하지 못했다.
- 미검증: [S21-b]·[S51]·[S52]는 일화·개인 측정이라 결론에 쓰지 않고 "재야 한다"는 근거로만 썼다. [S30]·[S49]·[S46] 등 2026년 arXiv 수치는 원문 표를 직접 열지 않고 초록·검색 요약으로 확인했다.
