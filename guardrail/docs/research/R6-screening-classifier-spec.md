# R6. 1단 스크리닝 분류기 구현 사양 (임베딩 + LR, conformal 2-threshold, LLM judge cascade)

- 작성일: 2026-10-29
- 범위: R1(아키텍처)과 R4(라이브러리·모델)에 이미 정리된 내용(CC/CC++ 수치, SafeRoute 5.09%, NVIDIA 임베딩+RF, KURE 검색 벤치, Prompt Guard 2 기본 카드, SEQ/AUD 다턴 논쟁)은 반복하지 않고 참조만 한다.
- 표기: **[사실]** 출처에 적힌 값(데이터셋·설정 병기). **[추론]** 우리 계산이나 해석. **[단일출처]** 독립 재현을 확인하지 못한 값. **[추정]** 실측 없이 계산한 값이며 반드시 측정으로 대체해야 한다.
- 결론 한 줄: 임베딩+LR 1단은 **in-distribution에서는 강하지만 분포 이동에서 크게 무너진다**는 근거가 일관된다. 그러므로 1단은 "확실한 것만 차단, 애매하면 judge"라는 라우터로 쓰고, 차단 threshold는 benign 데이터만으로 conformal/NP 방식으로 정하며, 운영 중 재보정 트리거를 명시해 둔다.

---

## Q1. 소형/임베딩 분류기의 공개 수치

### 1-1. 비교표

| 모델 (크기) | 학습/평가 데이터 | TPR(Recall) | FPR 또는 Precision | 다국어 | 출처 |
|---|---|---|---|---|---|
| OpenAI emb-3-small + RF | HF 6개 셋 467,057개(악성 23.54%), 80/20 층화 분할 | 0.870 | Prec 0.867, AUC 0.764, FPR 미보고 | 영어 | [EBC] Table 4–5 |
| OpenAI emb + **LR** | 동일 | 0.807 | Prec 0.793, AUC 0.637 | 영어 | [EBC] |
| MiniLM-L6 (22M) + LR | 동일 | 0.795 | Prec 0.777 | 영어 | [EBC] |
| ProtectAI deberta-v3-base-v2 (184M) | 같은 테스트셋 | 0.991 | Prec 0.758 | 영어 | [EBC] Table 5 |
| ProtectAI v2 (자체 카드) | 학습 외 20,000개 | 99.74% | Prec 91.59% | **영어 전용**, jailbreak 미탐지 명시 | [PAI] |
| Prompt Guard 2 86M (mDeBERTa) | Meta 자체 평가 | Recall 97.5% @1% FPR(영어) | AUC .998(영) / .995(다국어, 8개 언어, 한국어 없음) | 다국어 | [PG2] |
| Prompt Guard 2 22M | 동일 | 88.7% @1% FPR | AUC .995 / .942(다국어) | 약함 | [PG2] |
| PromptGuard(1) / ProtectAI v2 / InjecGuard | PromptShield 평가 split, 약 24K, 학습에 없던 데이터셋 | **12.78% / 1.97% / 20.37% @1% FPR**, 9.39% / 0.00% / 6.61% @0.1% FPR | — | — | [PS] Table 4 |
| PromptShield DeBERTa-v3-base (184M) | 학습 20K(benign:악성 = 1:1) | 43.22% @1%, 31.45% @0.1% | — | 영어 | [PS] Table 5 |
| PromptShield Llama-3.1-8B | 동일 | 94.80% @1%, 65.33% @0.1% | — | 영어 | [PS] |
| InjecGuard/PIGuard (DeBERTa-v3-base) | 학습 76,755개(benign 61,089 / 인젝션 15,666) + MOF 1,000개 | Malicious acc 77.39% | **NotInject over-defense acc 87.32%**, Benign acc 85.74% | NotInject에 다국어 84개 포함 | [IG] Table 1 |
| 같은 표: PromptGuard / ProtectAI v2 / Lakera / GPT-4o | NotInject 339개 | Malicious 97.10 / 48.60 / 53.19 / 79.10 | **Over-defense 0.88 / 56.64 / 87.61 / 86.73** | — | [IG] Table 1 |
| Sentinel (ModernBERT-large, 395M) | 내부 10% 테스트 / 공개 4개 셋 | Recall 0.991(내부) | F1 공개 평균 0.938 (ProtectAI v2 0.709) | 영어 | [SEN] Table 1–2 `[단일출처]` |
| GenTel-Shield (multilingual-E5 미세조정) | GenTel-Bench 84,812 공격 + 동수 benign | Jailbreak R 97.34, Goal hijack R 94.19 | Prec 98.04 / 99.44 | 평가 5개 언어(기계번역), 한국어 없음 | [GT] Table 1–2 `[단일출처]` |
| Llama-3.1-8B layer-31 activation + **LR** | 18개 데이터셋 | — | AUC: 5-fold CV 0.996 → **LODO 0.912** (-8.4pp). Qwen-3.5-2B 기준 -16.5pp | 영어 | [WBL] |
| 같은 논문, benign FPR(Table 6) | OpenOrca 등 benign | — | PromptGuard 2 0.4%, LlamaGuard 3.0%, LLM-judge 4.4%, ProtectAI v2 4.2%, Deepset v2 82.4% | — | [WBL] |

### 1-2. 수치에서 읽을 수 있는 것
- **[사실] AUC와 기본 threshold 0.5는 배포 성능을 예측하지 못한다.** PromptShield 저자들은 AUC가 low-FPR 영역 성능을 잘 예측하지 못한다고 명시했다. PromptGuard는 threshold 0.5에서 FPR 2.91% / TPR 22.82%였고, FPR을 1%로 낮추자 TPR이 12.8%로 떨어졌다. 자체 보고치는 "1% FPR에서 71%"였다 [PS].
- **[사실] 같은 출처 안에서 나눈 split은 성능을 부풀린다.** leave-one-dataset-out(LODO)으로 평가하면 pooled AUC가 8.0–16.5pp 떨어지고, 데이터셋별 정확도 격차는 최대 25.4pp(jayavibhav 94.5% vs 69.1%)까지 벌어진다. 상위 SAE feature의 28%는 데이터셋 고유의 shortcut이었다 [WBL]. 표준적인 domain-generalization 기법으로도 이 격차를 메우지 못했다 [WBL].
- **[사실] trigger-word 때문에 over-defense가 생긴다.** "ignore", "system" 같은 단어가 들어간 정상 문장(NotInject)에서 PromptGuard는 정확도 0.88%로 사실상 전부를 차단했다. ProtectAI v2는 56.64%였다 [IG]. 반대로 오탐을 줄이면 malicious recall이 내려간다(ProtectAI v2 48.6%). 두 지표를 **동시에** 보고해야 하는 이유다.
- **[사실] 놓친 공격에 대한 확신이 매우 높다.** ProtectAI v2와 PG2 두 체크포인트를 공격 분포 이동 5종에 적용했더니 FNR은 0.01–0.97로 퍼졌지만, 놓친 공격에 대한 확신도(severity)는 0.99–1.00이었다. pooled calibration error가 0.06인 탐지기도 공격 샘플만 떼어 보면 0.91이었다 [CW] `[단일출처, abstract 기반]`. 따라서 "점수가 낮다 = 안전"이라고 가정할 수 없다.
- **[사실] in-domain benign 데이터의 효과.** 대화형 benign 데이터를 학습에 넣으면, 대화 테스트셋에서 0.05% FPR 목표 threshold의 실제 FPR이 0.32%에서 0.06%로 내려갔다 [PS].
- **[추론] 현실적인 기대치.** 한국어 학생 트래픽에서 임베딩+LR이 낼 수 있는 성능은 대략 다음과 같다. (a) 학습 출처와 같은 분포에서는 AUC 0.97–0.99, TPR@1%FPR 60–90%. (b) 새 공격 계열(LODO)에서는 AUC가 0.82–0.92로 떨어지고 TPR@1%FPR은 10–45%까지 내려갈 수 있다. 근거는 [WBL]의 LODO 하락폭과 [PS]의 DeBERTa 43%, 기존 탐지기 2–20%다. **그러므로 1단 단독으로 FPR 1%와 높은 recall을 함께 달성한다는 주장은 방어할 수 없다.** 1단 성능은 escalate율과 함께 보고해야 한다.
- **[사실/공백] 한국어 근거.** 위 표에 한국어 평가는 없다. 한국어 수치는 R4의 Kanana-Prompt(F1 0.844, 자체 보고)가 유일하다.

### 1-3. 알려진 실패 유형 (평가셋에 반드시 포함할 것)
1. trigger-word over-defense: 교과 속 "무시하다/명령/시스템/해킹/폭탄 반응식" [IG]
2. 출처 shortcut: 데이터셋마다 다른 문체나 길이를 학습하는 문제 [WBL]
3. 간접·행동 탈취형 인젝션: 모든 탐지기가 높은 확신으로 통과시켰다 [CW][WBL 7–37% 탐지]
4. black-box 재작성기로 만든 confident miss [CW]
5. adaptive attack 앞에서 탐지기를 붙이면 ASR이 오히려 오른 사례 (R1의 [AMS])

---

## Q2. Calibration과 threshold: 공식과 표본 크기

### 2-1. 설정
1단 점수 s(x) ∈ [0,1]은 LR의 위험 확률이다. benign을 class 0, attack을 class 1로 둔다. 필요한 threshold는 둘이다.
- **τ_b (block):** P(s(X) > τ_b | benign) ≤ α를 만족해야 한다. 이는 benign 한 클래스만 쓰는 class-conditional(Mondrian) conformal이며, Neyman–Pearson type-I 제약과 같다.
- **τ_e (escalate):** P(s(X) < τ_e | attack) ≤ β (attack miss율). attack 클래스만 써서 같은 방식으로 정한다.

### 2-2. Split conformal (marginal 보장) [CP][MCP]
calibration benign 점수 s_1..s_n을 오름차순 정렬해 s_(1) ≤ … ≤ s_(n)으로 둔다.
- k = ⌈(n+1)(1−α)⌉, **τ_b = s_(k)**, 규칙은 s > τ_b일 때 차단.
- **[사실] 보장:** 교환가능성(exchangeability)이 성립하면 α − 1/(n+1) ≤ P(FP) ≤ α이다(하한은 동점이 없을 때). 이 보장은 calibration 셋에 대한 기댓값(marginal)이다 [CP].
- **[사실] calibration 셋에 조건부인 실제 FPR**은 Beta(l, n+1−l), l = n+1−k를 따른다. 한 번 뽑은 셋에서 실제 FPR은 이 분포만큼 흔들린다 [CP §3.2].
- 실행 조건: k ≤ n이어야 하므로 n ≥ (1−α)/α. α=1%이면 n ≥ 99, α=5%이면 n ≥ 19.
- Mondrian 확장: 그룹 g(과목·학년)마다 같은 식을 n_g로 반복한다. 보장은 그룹별 P(FP | g) ≤ α가 되지만, 입력 x 단위 조건부 보장은 불가능하다 [MCP]. 그룹별 n_g가 위 하한을 넘지 못하면 그 그룹은 전체 τ_b를 쓴다.

### 2-3. NP umbrella (고확률 보장) [NP]
"P(실제 FPR > α) ≤ δ"를 원하면 다음과 같이 정한다.
- v(k) = Σ_{j=k}^{n} C(n,j)(1−α)^j α^{n−j}, **k\* = min{k : v(k) ≤ δ}**, τ_b = s_(k\*).
- **[사실] 최소 표본: n ≥ log δ / log(1−α).** α=δ=0.05이면 59, α=0.01·δ=0.05이면 **299**다 [NP].

### 2-4. 계산 예시 (이 문서에서 직접 계산, stdlib 이항/Beta)

| α | n(benign calib) | conformal k | 허용 초과 개수 n−k | 실제 FPR 95백분위 (Beta) | NP(δ=.05) 허용 초과 | NP 기대 FPR |
|---|---|---|---|---|---|---|
| 1% | 100 | 100 | 0 (=최댓값) | 2.95% | 불가능 | — |
| 1% | 300 | 298 | 2 | 2.08% | 0 | 0.33% |
| 1% | 1,000 | 991 | 9 | 1.57% | 4 | 0.50% |
| 1% | 2,000 | 1,981 | 19 | 1.39% | 12 | 0.65% |
| 5% | 100 | 96 | 4 | 8.92% | 1 | 1.98% |
| 5% | 300 | 286 | 14 | 7.20% | 8 | 2.99% |
| 5% | 1,000 | 951 | 49 | 6.18% | 38 | 3.90% |

해석 [추론]
- α=1%에 n=300을 쓰면 "평균 1%"는 지켜지지만, 운이 나쁜 calibration 셋에서는 실제 FPR이 2%를 넘는다. **α=1% 차단에는 benign calibration 1,000개 이상**이 필요하고, 이때 95% 상한이 약 1.6%다. NP 방식(δ=5%)을 쓰면 FPR ≤ 1%가 95% 확률로 보장되는 대신 기대 FPR이 0.5%로 보수적이 되어 차단이 줄어든다(TPR 손실). 차단이 escalate보다 비용이 크므로 **τ_b는 NP로, τ_e는 conformal로 정하는 것**을 권장한다.
- 계산 예시(α=5%, n=200): k = ⌈201×0.95⌉ = ⌈190.95⌉ = 191. 즉 benign 점수 중 191번째로 작은 값이 τ_b이고, 이를 넘는 benign은 9개(4.5%)까지 허용된다.

### 2-5. 교환가능성 위반 대응
- **[사실] Weighted conformal** [WCP]: 공변량 이동(covariate shift)에서 w(x) = dP_test/dP_cal(x)를 각 calibration 점에 가중치로 주고 가중 분위수를 쓰면 보장이 회복된다. w는 "calibration vs 최근 트래픽" 판별 확률분류기로 추정할 수 있다: w = p/(1−p) × n_cal/n_new. [추론] 임베딩이 이미 있으므로 구현 비용이 거의 없다(LR 하나 추가). 다만 attack 분포 이동은 라벨 이동이라 이 방법으로 고쳐지지 않는다.
- **[사실] 비교환 conformal** [NEX]: 고정 가중치(예: 최근 데이터일수록 큰 가중)를 쓰면 coverage 손실이 Σ w̃_i·d_TV(Z, Z^i) 이하로 제한된다. 즉 시간 감쇠 가중이 이론적으로 정당화된다.
- **[사실] Adaptive conformal inference** [ACI]: α_{t+1} = α_t + γ(α − err_t). 장기 평균 오류율이 α로 수렴한다. [추론] benign 라벨을 온라인으로 얻어야 한다는 전제가 있다. 우리 시스템에서는 judge가 safe로 판정한 escalate 표본과 차단 표본 감사 결과를 err_t의 proxy로 쓸 수 있지만, 편향된 부분 피드백이므로 **주기적 재보정이 주 수단이고 ACI는 보조**로 둔다.
- **[사실] Mondrian/conformal만으로는 "놓친 공격의 높은 확신"을 고칠 수 없다** [CW]. threshold 보정은 FPR만 다룬다. recall 하락은 재학습(Q6)과 judge 경로로 대응한다.

---

## Q3. Cascade 수학과 escalation 정책

### 3-1. 3-영역 cascade의 end-to-end 지표 [추론, 표준 확률 계산]
1단 영역은 A(allow: s<τ_e), E(escalate: τ_e≤s≤τ_b), B(block: s>τ_b)이다. 클래스 c ∈ {benign, attack}에 대해 다음을 정의한다.
- b_c = P(B|c), e_c = P(E|c), a_c = 1 − b_c − e_c
- judge의 **E 영역 조건부** 판정률: f_J = P(judge=unsafe | E, benign), t_J = P(judge=unsafe | E, attack)

그러면 다음이 성립한다.
- **FPR_sys = b_ben + e_ben·f_J**
- **TPR_sys = b_att + e_att·t_J** (A 영역으로 빠진 attack은 이후 출력 judge만 막을 수 있음)
- **escalate율 r = π·e_att + (1−π)·e_ben** (π = 트래픽 중 공격 비율)
- **요청당 비용 C = c_1 + r·c_J**, 기대 지연 = L_1 + r·L_J
- **p95 지연**: r > 5%이면 p95에 judge 지연이 들어간다. r ≤ 5%이면 p95 ≈ L_1의 p95. 그래서 **r ≈ 5%가 지연 SLA의 분기점**이다.
- 주의: f_J와 t_J는 judge의 **전체 평균 성능이 아니다.** E 영역은 어려운 표본만 모아 놓은 곳이므로 judge 오류율이 평소보다 높다. 실측할 때는 E 영역 표본으로 따로 측정해야 한다. 소형 분류기가 확신하며 틀린 표본에서는 LLM judge도 같은 오류를 반복하는 경향이 보고되었다 [JEV] `[단일출처]`. 따라서 A 영역의 confident miss는 judge로 회수되지 않는다는 전제로 설계한다.

### 3-2. 예시 계산 (가정값)
π=2%. 1단 benign: b=0.5%, e=6%. attack: b=60%, e=32%(A로 빠짐 8%). judge(E 조건부): f_J=10%, t_J=85%.
- FPR_sys = 0.005 + 0.06×0.10 = **1.1%**
- TPR_sys = 0.60 + 0.32×0.85 = **87.2%**
- r = 0.02×0.32 + 0.98×0.06 = **6.5%** → p95에 judge 지연이 걸리는 경계
- LODO 상황을 가정해 attack 쪽이 b=20%, e=40%(A 40%)로 나빠지면 TPR_sys = 0.20 + 0.40×0.85 = **54%**이고, 출력 단계 exchange judge(R1)가 나머지를 막아야 한다.

### 3-3. τ_e 선택 절차 (비용 최소화)
제약 FPR_sys ≤ α_sys와 r ≤ r_max 아래에서 다음 비용을 최소화한다.
- J(τ_e) = c_FN·π·(1−TPR_sys) + c_FP·(1−π)·FPR_sys + c_J·r

τ_b는 2-3절에서 고정한다. τ_e는 attack calib 점수의 β-분위수에서 시작해 validation 격자(0.01 간격)로 탐색한다. 근거 [사실]: CC++ 1단은 red-team 데이터 대부분을 flag하도록 설정되었고 escalate율은 5.5–10%였다(R1). SafeRoute 오라클은 5.09%였다(R1). OpenAI도 "빠른 고recall 분류기 → 애매한 것만 safeguard"를 권장한다(R1). **목표: attack calib recall(B∪E) ≥ 95%, r ≤ 10%.** 두 목표가 충돌하면 recall을 우선하고 judge 비용 예산을 늘린다.

### 3-4. Deferral 정책
- 1단은 **E 영역 = abstain**으로 처리한다. 학습된 router(SafeRoute)는 hidden state가 필요하므로 쓰지 않는다(R1).
- judge 실패(타임아웃·JSON 파싱 실패 2회)는 **fail-closed가 아니라 "안전 템플릿 응답 + 로그"**로 처리한다. 학생 서비스에서 fail-closed는 over-refusal을 그대로 키운다. 단 B 영역은 judge 없이 차단한다.
- judge 결과 3단계(safe/controversial/unsafe, R1)는 controversial을 교육용 안전 답변 경로로 보낸다.

---

## Q4. 다턴: 알고리즘, 재판정 주기, 비용

### 4-1. 근거 (R1 이후 추가분)
- **[사실] Defensive M2S** [M2S]: 다턴 대화를 단일턴으로 압축(hyphenize/numberize/pythonize)한 뒤 guard를 학습한다. Qwen3Guard+hyphenize가 SafeDialBench에서 **recall 93.8%**를 냈고, 추론 토큰은 대화당 **3,231 → 173(−94.6%)**으로 줄었다. 학습은 779개, 평균 10.6턴. benign 다턴 FPR은 보고되지 않았다. `[단일출처, abstract 기반]`
- **[사실] DeepContext** [DC]: 턴 임베딩 시퀀스를 RNN으로 처리하는 stateful 모니터다. F1 **0.84**(Llama-Prompt-Guard-2 0.67, Granite-Guardian 0.67), T4 GPU에서 **20ms 미만**. recall과 FPR은 abstract에 없다. `[단일출처]`
- **[사실] X-Boundary** [XB] (방어용 미세조정이며 외부 guard가 아님): Llama-3-8B에서 ActorAttack ASR이 58.5%에서 16.5%로 내려갔다. API 모델에는 적용할 수 없다.
- **[사실] X-Teaming** [XT]: 다턴 공격 ASR이 최대 98.1%였고, XGuard-Train(30K 다턴 대화)은 SafeMTData 대비 저항력을 +28.3% 높였다(모델 학습용).
- [공백] CPU 전용 대화 단위 분류기를 MHJ/ActorAttack에 대해 FPR까지 보고한 공개 결과는 찾지 못했다.

### 4-2. 채택 알고리즘 [추론]
1. 턴 점수는 s_t = 1단 점수(사용자 발화)다.
2. 세션 위험은 **R_t = max(R_{t−1}, λ·R_{t−1} + (1−λ)·s_t)**이다. 값이 오르기만 하는 EMA이며 λ=0.7(반감기 약 2턴)이다. **R_t는 차단 근거로 쓰지 않고 escalate 트리거로만** 쓴다(R1 결론 유지).
3. **대화 judge 호출 조건**(하나라도 해당하면 호출):
   (a) R_t ≥ τ_sess (= benign 세션 calib에서 R의 95% 분위수, 세션 단위 conformal)
   (b) 최근 4턴 중 2턴 이상이 E 영역
   (c) 마지막 대화 judge 이후 6턴이 지나고 R_t ≥ τ_e/2
4. **대화 judge 입력**: 최근 사용자 발화 최대 8턴을 M2S hyphenize 형식으로 압축하고, 마지막 assistant 답변은 300자로 자른다. 예상 입력은 300–600 토큰이다 [M2S 173 토큰/대화 참고, 추정].
5. 대화 judge가 unsafe를 내면 그 세션은 이후 모든 턴을 judge로 보낸다(sticky). safe를 내면 R_t를 τ_sess 아래로 리셋하지 않고 동결한다. 동결해야 공격자가 리셋을 반복해 우회하는 것을 막을 수 있다.
6. 비용 [추정]: 세션 30%가 한 번 이상 트리거되고 평균 1.5회 호출이라고 하면 세션당 약 0.45회, 600 토큰 기준 세션당 약 270 입력 토큰이 추가된다. 정상 세션 중 민감 단원(전쟁사, 화학) 세션은 트리거율이 높을 것이므로 이를 따로 측정한다.

---

## Q5. CPU 성능

### 5-1. 공개 수치 (빈약함)
- **[사실]** bge-m3 ONNX fp32는 PyTorch(FlagEmbedding) 대비 857ms vs 1091ms(1.27x)였다. 긴 문서 기준이고 CPU·스레드는 미기재. 파일 크기는 fp32 약 2.2GB, 동적 int8의 dense cosine은 0.985 [BGEONNX].
- **[사실]** bge-m3 OpenVINO int8: 약 570MB(fp32 약 2.2GB), FP32와 Pearson 0.9941 [OVINT8] `[단일출처]`.
- **[사실]** USER-bge-m3 int8(러시아어 파생): 0.3539s vs 0.7504s(약 2x), cosine 약 0.988. 하드웨어와 길이는 미기재 [USERINT8].
- **[사실]** int8 속도 이득은 CPU 명령어에 따라 달라진다. AVX-512 VNNI가 없는 x86에서는 int8이 약 1.8x **느려진** 사례가 있다(ResNet-50) [INT8P] `[다른 모델]`.
- 한국어 소형 대안의 한국어 검색 품질(KURE 카드, Recall@1 평균): KURE 0.526 > bge-m3 0.518 > me5-large 0.501 ≈ KoE5 0.502 > me5-base 0.470 ≫ ko-sroberta 0.338 [KURE]. **주의: 검색 점수이지 분류 점수가 아니다.** me5-small은 이 표에 없다.

### 5-2. FLOP 기반 추정 (50토큰, 배치 1) [추정]
- XLM-R large 계열(KURE/bge-m3, 24층, d=1024, FFN 4096): 층·토큰당 약 24d² ≈ 25M FLOPs → **약 30 GFLOPs/쿼리**. 임베딩 행렬(약 256M 파라미터)은 연산이 아니라 메모리만 차지한다.
- me5-small(12층, d=384): **약 2.1 GFLOPs/쿼리**로 약 14배 적다.
- 4코어 AVX2 fp32에서 배치 1 GEMM 실효 처리량을 120–200 GFLOPS로 잡으면 다음과 같다.

| 구성 | p50 추정 | p95 추정 | 상주 메모리 추정 |
|---|---|---|---|
| KURE/bge-m3 fp32 (ONNX Runtime, 4 스레드) | 150–250ms | 250–400ms | 2.3–2.8GB |
| KURE int8 (VNNI 있음) | 80–130ms | 150–220ms | 0.7–1.0GB |
| me5-small fp32 | 15–30ms | 30–50ms | 0.5–0.6GB |
| me5-small int8 | 8–20ms | 20–35ms | 0.2–0.3GB |
| 토크나이즈 + 정규화 + LR(1024차원 내적) | 1–3ms | 5ms | 무시 가능 |

p95는 동시 요청 때문에 생기는 스레드 경합에 크게 좌우된다. 동시성이 2 이상이면 `intra_op_num_threads=2`, 워커 2개로 나눈다. **이 표의 모든 값은 대상 CPU에서 측정해 교체한다.**

---

## Q6. 학습 데이터 레시피

### 6-1. 근거
- **[사실] 학습량 곡선** [PS Table 6]: 8B 미세조정 기준 TPR@1%FPR이 1K 62.0% → 5K 89.6% → 10K 88.8% → 20K 94.8%였고, @0.1%FPR은 28.1 → 60.1 → 61.9 → 65.3%였다. **1K→5K에서 크게 오르고 그 뒤로는 거의 평탄하며, 단조 증가하지도 않는다.** 모델 계열이 다르므로(LR 헤드 아님) 형태만 참고한다.
- **[사실] 모델 크기 효과** [PS Table 5]: 20K 학습에서 DeBERTa-base(184M) 43%가 FLAN-T5-large(751M) 56%와 Llama-1B 67%보다 낮다. 크기만으로 설명되지 않는다.
- **[사실] 라벨 노이즈**: WildGuardMix train 라벨은 GPT-4가 붙였고 500개 감사에서 prompt-harm 일치율이 92%였다. test는 3인 주석이며 prompt-harm Fleiss κ가 **0.55**(moderate)다 [WGM]. [추론] 이 셋으로 측정한 정확도는 대략 90% 초반이 상한이고, 경계 사례 라벨은 신뢰하기 어렵다.
- **[사실] hard negative**: NotInject는 trigger word 수(1/2/3개) × 4개 범주(일반 질의·기법·가상 창작·다국어)로 각 113개씩 구성된다 [IG Table 8]. MOF는 trigger word 편향을 겨냥한 benign 1,000개를 추가 생성한다 [IG].
- **[사실/약함] 증강**: CC는 학습에 쓰지 않은 변형으로 OOD 평가셋을 만들었다(R1 [CC]). GenTel은 EDA와 LLM 재작성 증강을 썼지만 효과를 분리해 보여 주는 ablation은 확인하지 못했다 [GT]. 패러프레이즈 증강이 OOD 탐지를 개선한다는 직접 근거는 찾지 못했다. LODO 격차는 표준 DG 기법으로도 메워지지 않았다 [WBL].
- [공백] "고정 다국어 임베딩 + LR"의 learning curve를 안전 과제에서 보고한 1차 출처는 찾지 못했다. 아래 수치는 [추론]이다.

### 6-2. 권장 크기 [추론]
- 1024차원 L2-LR은 C를 CV로 고르면 클래스당 수백 개만으로도 학습되지만, **low-FPR 꼬리**를 안정시키려면 benign 쪽이 훨씬 많아야 한다.
- **train**: benign 3,000개(이 중 hard negative 1,000개 이상) + attack 2,000개(공격 계열 최소 8개, 계열당 100개 이상, 한국어 직접 작성·번역·난독화 포함)
- **calib**: benign 1,000개(τ_b용, 학생 실제 질의 분포. 과목별 Mondrian을 하려면 과목당 300개 이상) + attack 500개(τ_e용)
- **test**: benign 1,000개 + attack 500개. **공격 계열 단위 LODO 폴드와 별도의 adaptive red-team 셋**을 둔다.
- 클래스 균형: LR 학습은 `class_weight="balanced"`로 한다. 확률 보정은 하지 않아도 된다. threshold를 분위수로 정하므로 단조 변환과 무관하다.
- 분할 규칙: 같은 원문에서 나온 증강·번역·패러프레이즈는 **같은 split에 묶는다**(group split). 이를 어기면 [WBL]식 shortcut 때문에 성능이 부풀려진다.
- **학습량 판정**: 25/50/75/100% 부분집합으로 곡선을 그린다. 75%→100%에서 TPR@1%FPR(LODO 평균)이 2pp 미만으로 변하면 데이터 수집을 멈추고 계열 다양성 확보로 넘어간다.

---

## 구현 사양

### S1. 모델 선택과 폴백
| 순위 | 인코더 | 조건 |
|---|---|---|
| 1 | `nlpai-lab/KURE-v1` (ONNX int8, CLS pooling, L2 정규화) | VNNI가 있는 CPU에서 p95 ≤ 250ms일 때 |
| 1' | `BAAI/bge-m3` | KURE와 LODO TPR@1%FPR 차이가 2pp 이내면 라이선스와 생태계를 보고 선택 |
| 2 (폴백) | `intfloat/multilingual-e5-small` int8 (`query: ` 접두어, mean pooling) | p95가 초과하거나 RAM이 1GB 미만일 때. 성능 하락은 bake-off로 측정 |
| 보조 특징 (기본 off) | Prompt Guard 2 86M 점수 | 한국어 미평가이고 low-FPR 성능이 약해서(PS 12.78%@1%) bake-off에서 +2pp 이상일 때만 사용 |

선정 지표는 KURE 검색 벤치가 아니라 **우리 한국어 dev셋의 LODO TPR@1%FPR과 NotInject-ko over-defense 정확도**다.

### S2. 특징 파이프라인
1. R4 §9 정규화(NFKC → 제로폭 제거 → 혼동문자 → 자모 재조합). 원문과 정규화본을 둘 다 보존한다.
2. 정규화본을 인코딩한다. 최대 256토큰이며 넘으면 앞 128 + 뒤 128 토큰을 쓴다.
3. 특징 x = [emb(1024), 규칙 hit 수, 정규화 편집거리 비율, log 길이, 라틴 문자 비율]. 스칼라 특징은 표준화한다.
4. LR은 L2, C ∈ {0.1, 0.3, 1, 3}을 group 5-fold로 고르고 `class_weight="balanced"`로 학습한다. 출력 s = P(attack).
5. 임베딩은 RAG 관련도 계산에도 재사용하므로 요청 단위로 캐시한다(R4).

### S3. threshold 절차 (단계별)
1. train으로 LR을 학습한다. calib와 test는 이 단계에서 보지 않는다.
2. calib benign 점수 {s_i}(n_0개)로 NP umbrella(α_b=1%, δ=5%)를 적용한다. v(k) = Σ_{j≥k} C(n_0,j)(0.99)^j(0.01)^{n_0−j}, k* = min{k: v(k) ≤ 0.05}, **τ_b = s_(k\*)**. n_0 < 299이면 NP를 포기하고 split conformal k = ⌈(n_0+1)(0.99)⌉로 정한 뒤 문서에 "marginal 보장"이라고 표기한다.
3. calib attack 점수 {t_j}(n_1개)를 오름차순으로 정렬하고 **τ_e⁰ = t_(m)**, m = ⌊(n_1+1)·β⌋ (β=5%, m=0이면 τ_e⁰ = 최솟값)로 둔다. attack의 95% 이상이 τ_e⁰ 이상이 된다.
4. benign calib에서 r̂ = P(τ_e⁰ ≤ s ≤ τ_b)를 계산한다. r̂ > 10%이면 3-3의 J(τ_e)를 최소화하는 τ_e를 격자 탐색으로 찾되 τ_e ≤ τ_b를 지킨다. 그 결과 낮아진 recall을 보고서에 명시한다.
5. 과목별 Mondrian: 과목 g의 n_g ≥ 299이면 τ_b^g를 따로 계산하고, 아니면 전체 τ_b를 쓴다.
6. test(LODO 폴드 포함)에서 FPR_sys, TPR_sys, r을 3-1의 식과 실측으로 함께 보고한다. calib 셋은 이 평가에 쓰지 않는다.
7. τ_b, τ_e, n_0, n_1, k*, 학습 데이터 해시를 `thresholds.json`에 저장한다. 런타임은 이 파일만 읽는다.

참고 구현: R4 §8의 numpy 한 줄 공식에 NP의 이항 꼬리 합(`math.lgamma` 기반 10줄)을 더한다. scipy는 필요 없다.

### S4. Cascade 수식 (런타임)
- s > τ_b이면 **block**(judge 없이 안전 템플릿)
- τ_e ≤ s ≤ τ_b이거나 세션 트리거(4-2)가 걸리면 **LLM policy judge**(safe/controversial/unsafe)
- s < τ_e이면 **allow**. 이후 출력 exchange judge가 최종 방어선이다(R1).
- 지표: FPR_sys = b_ben + e_ben·f_J, TPR_sys = b_att + e_att·t_J, r = π·e_att + (1−π)·e_ben, C = c_1 + r·c_J

### S5. 기대 지표 범위 [추론, 실측으로 교체]
| 지표 | in-distribution | LODO/신규 공격 계열 |
|---|---|---|
| 1단 AUC | 0.97–0.99 | 0.82–0.92 |
| 1단 benign 차단율(τ_b) | ≤ 1% (NP 기대값 약 0.5%) | 분포 이동 시 1–3%로 오를 수 있음 → 모니터링 |
| attack B∪E recall | ≥ 95% (calib 정의상) | 50–85% |
| escalate율 r | 5–10% | 10–20% |
| 시스템 TPR (입력 단계) | 85–92% | 50–75% (출력 judge가 보완) |
| 시스템 benign FPR (입력 단계) | 1–2% | 2–4% |

### S6. 지연 예산 (4코어 CPU, 요청당)
| 단계 | p50 목표 | p95 상한 |
|---|---|---|
| 정규화 + 규칙 | 1ms | 3ms |
| 인코딩 (KURE int8 / me5-small) | 100ms / 20ms | 250ms / 50ms |
| LR + threshold | <1ms | 1ms |
| **1단 합계** | **≤ 110ms** | **≤ 260ms** |
| LLM judge (API, E 영역만) | 0.8–2s [추정] | 3–4s, 타임아웃 5s |
r ≤ 5%를 유지하면 시스템 p95가 1단 p95 근처에 머문다. r이 5%를 넘으면 p95는 judge 지연이 지배한다.

### S7. 모니터링과 재보정 트리거
| 신호 | 계산 | 트리거 | 조치 |
|---|---|---|---|
| 차단율 | 일별 B 비율 | 7일 이동평균 > 2·α_b·(1−π̂) | 차단 표본 200개 감사 → τ_b 재보정 |
| escalate율 | 일별 r | 목표 대비 0.5배 미만 또는 2배 초과가 3일 연속 | τ_e 재탐색 (3-3) |
| judge 뒤집기율 | E 영역에서 judge=safe인 비율 | > 90% (1단이 과민) 또는 < 30% (judge 비용 낭비) | τ_e 상향/하향 |
| 차단 감사 | B 표본 1%를 비동기로 judge에 보냄 | judge=safe 비율 > 30% | τ_b 재보정 + hard negative 추가 |
| 점수 분포 이동 | benign 추정 트래픽 s의 PSI (calib 대비) | PSI > 0.2 | weighted conformal 적용 또는 재보정 |
| 임베딩 이동 | "calib vs 최근 1주" 판별 LR의 AUC | > 0.70 | 최근 트래픽 benign을 라벨링해 calib 교체 |
| 신규 공격 계열 | 레드팀/사고에서 계열별 B∪E recall | < 80% | 해당 계열 100개 이상 추가 → 재학습 → S3 전체 재실행 |
| 정기 | — | 월 1회 또는 새 benign 라벨 1,000개 누적 | 재학습 + S3 |
재보정할 때마다 `thresholds.json` 버전과 함께 test 지표(S5 표의 형식)를 남긴다. 재학습한 뒤에는 calib 셋을 반드시 새로 뽑는다. 학습에 쓴 점수로 다시 calibration하면 보장이 깨진다.

### S8. 하지 않는 것 (YAGNI)
- 학습된 router(SafeRoute)와 activation probe: API 전용이라 불가능하다.
- 확률 calibration(Platt, isotonic): 분위수 threshold라서 필요 없다.
- MAPIE와 crepes 런타임 의존성: 검증용 테스트에서만 쓴다(R4).
- 범주별 다중 헤드: 단일 헤드의 혼동 분석에서 "인젝션 vs 유해요청"의 τ 충돌이 확인되면 그때 추가한다.

---

## 출처
- [EBC] Ayub & Majumdar, "Embedding-based classifiers can detect prompt injection attacks", arXiv:2410.22284 — https://arxiv.org/html/2410.22284
- [PG2] Meta, Llama Prompt Guard 2 86M 카드 — https://huggingface.co/meta-llama/Llama-Prompt-Guard-2-86M
- [PAI] ProtectAI deberta-v3-base-prompt-injection-v2 카드 — https://huggingface.co/protectai/deberta-v3-base-prompt-injection-v2
- [PS] Jacob et al., "PromptShield: Deployable Detection for Prompt Injection Attacks", CODASPY 2025, arXiv:2501.15145 — https://arxiv.org/html/2501.15145v2
- [IG] Li et al., "InjecGuard / PIGuard", arXiv:2410.22770, ACL 2025 — https://arxiv.org/html/2410.22770v3 , https://aclanthology.org/2025.acl-long.1468
- [SEN] "Sentinel: SOTA model to protect against prompt injections", arXiv:2506.05446 — https://arxiv.org/html/2506.05446
- [GT] "GenTel-Safe", arXiv:2409.19521 — https://arxiv.org/html/2409.19521
- [WBL] Fomin, "When Benchmarks Lie: Evaluating Malicious Prompt Classifiers Under True Distribution Shift", arXiv:2602.14161 — https://arxiv.org/html/2602.14161
- [CW] "Confidently Wrong: Severity-Aware Calibration of Prompt-Injection Detectors under Attack Shift", arXiv:2606.22659 — https://arxiv.org/abs/2606.22659
- [T3] "Trust The Typical", arXiv:2602.04581 — https://arxiv.org/abs/2602.04581 (benign만으로 학습한 OOD 방식, 기존 대비 FPR 최대 40x 감소 주장. abstract만 확인. 대안 아이디어로만 기록)
- [CP] Angelopoulos & Bates, arXiv:2107.07511 — https://arxiv.org/abs/2107.07511
- [MCP] Ding et al., "Class-Conditional Conformal Prediction with Many Classes", arXiv:2306.09335 (Mondrian/classwise 정의, Vovk et al. 2005 인용) — https://arxiv.org/abs/2306.09335
- [NP] Tong, Xia, Wang, Feng, "Neyman-Pearson classification: parametrics and sample size requirement", JMLR 21 (umbrella 알고리즘은 Tong, Feng, Li 2018) — https://ar5iv.arxiv.org/html/1802.02557
- [WCP] Tibshirani, Barber, Candès, Ramdas, "Conformal Prediction Under Covariate Shift", arXiv:1904.06019 — https://arxiv.org/abs/1904.06019
- [NEX] Barber, Candès, Ramdas, Tibshirani, "Conformal prediction beyond exchangeability", arXiv:2202.13415 — https://arxiv.org/abs/2202.13415
- [ACI] Gibbs & Candès, "Adaptive Conformal Inference Under Distribution Shift", arXiv:2106.00170 — https://arxiv.org/abs/2106.00170
- [JEV] "JEV vs. LLMs as Rubric Judges", arXiv:2609.29769 (요약 페이지 기반) — https://www.emergentmind.com/papers/2609.29769
- [M2S] Kim, "Defensive M2S", arXiv:2601.00454 — https://arxiv.org/abs/2601.00454
- [DC] "DeepContext", arXiv:2602.16935 — https://arxiv.org/abs/2602.16935
- [XB] "X-Boundary", arXiv:2502.09990 — https://arxiv.org/pdf/2502.09990
- [XT] "X-Teaming", arXiv:2504.13203 — https://arxiv.org/pdf/2504.13203
- [WGM] allenai/wildguardmix 데이터 카드 — https://huggingface.co/datasets/allenai/wildguardmix ; WildGuard arXiv:2406.18495
- [KURE] https://huggingface.co/nlpai-lab/KURE-v1
- [BGEONNX] https://huggingface.co/a-ivanovitch/bge-m3-onnx ; [OVINT8] https://huggingface.co/stellars/bge-m3-openvino-int8 ; [USERINT8] https://huggingface.co/skatzR/USER-BGE-M3-ONNX-INT8
- [INT8P] "Is INT8 Portable?", arXiv:2609.16085 — https://arxiv.org/pdf/2609.16085
- 수치 계산 스크립트(2-4절 표): stdlib 이항/Beta 역함수, 이 세션 scratchpad에서 실행. 저장소에는 포함하지 않음.
