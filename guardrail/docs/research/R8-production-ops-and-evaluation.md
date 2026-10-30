# R8 가드레일 운영과 평가

작성 2026-10-29 · 선행 메모: README(R1–R4) · 목적: 실제 서비스로 **운영**하고 심사위원 앞에서 **믿을 만하게 평가**하려면 무엇이 필요한가

표기: `[n]`은 맨 아래 출처 번호. **(단일)** = 1차 출처 하나에서만 확인. **(2차)** = 1차 출처 접근 실패로 2차 자료 사용. **(가안)** = 출처가 아니라 우리 설계 제안 값.

---

## 0. 요약

- 상용 가드레일 중 **한국어를 공식적으로 "최적화·지원"한다고 밝힌 곳은 AWS Bedrock Guardrails(Standard tier)와 Google Model Armor(테스트 언어에 한국어 포함)** 뿐이다 [2][6]. Azure는 유해 범주를 8개 언어에서만 학습·검증했고 한국어는 빠져 있다. Prompt Shields는 주로 영어로 학습됐다 [1][3]. OpenAI moderation은 무료지만 언어별 수치는 공개하지 않는다 [7][8].
- 모든 벤더가 **"우리 언어·데이터로 직접 시험하라"**고 명시한다 [1][2]. 벤더 지연 시간 수치는 대부분 공개되지 않았다. 공개된 것은 Cloudflare 약 500ms(Llama Guard 3 8B) [10], Lakera 자체 호스팅 1,000자 p95 20ms 미만 [11] 정도다.
- 운영의 핵심은 세 가지다. ① 입력 가드를 검색과 **병렬**로 돌린다 [12]. ② 범주별로 **fail-open/closed를 다르게** 정한다. 위기 범주는 실패 시 "지원 제공" 쪽으로 기운다. ③ **annotate(그림자) 모드 → 카나리** 순서로 임계값을 바꾼다 [3][19].
- 평가의 핵심도 세 가지다. ① 정적 벤치마크만으로는 부족하다. 적응형 공격에서 방어 12종 대부분이 ASR 90%를 넘겼다 [14]. ② ASR·과잉 거절 모두 **신뢰구간과 표본 수**를 함께 낸다. 실패 0건이라도 "≥95% 차단"을 95% 신뢰도로 말하려면 **최소 59건**이 필요하다(§B2). ③ 두 설정은 같은 문항으로 짝지어 **McNemar**로 비교한다.

---

# Part A. 운영

## A1. 상용 가드레일 비교와 베이스라인 후보

| 제품 | 주요 기능 | 한국어 | 지연 | 가격 모델 |
|---|---|---|---|---|
| **Azure AI Content Safety** | Hate/Sexual/Violence/Self-harm, 텍스트 심각도 0–7(또는 0/2/4/6) [4]; Prompt Shields(사용자 공격·문서 공격, 하위 유형: 규칙 변경·대화 위조·롤플레이·인코딩) [3]; Groundedness(미리보기, 2027-04-30 퇴역 예정) [5]; 보호 자료; 사용자 블록리스트 | 유해 범주는 zh/en/fr/de/it/ja/pt/es에서 학습·검증. 한국어는 "작동은 하나 품질 변동" [1]. Prompt Shields는 주로 영어 [3]. 직접 API의 koreacentral 리전에는 text 유해 범주가 없음 [1] | 공개 수치 없음. S0 요청 한도 1000건/10초 [5] | 텍스트 레코드(1,000자) 단위 과금, 무료 월 5,000 레코드 [9] |
| **AWS Bedrock Guardrails** | 콘텐츠 필터(+프롬프트 공격), 금지 주제, 단어 필터, PII(정규식은 무료), 맥락 근거성(grounding·relevance 점수, 임계 0–0.99), Automated Reasoning [2][15][16] | Standard tier에서 콘텐츠 필터·프롬프트 공격·금지 주제·근거성 모두 **한국어 "Optimized and supported"**. PII도 한국어 최적화. Classic tier는 en/fr/es만, 단어 필터도 en/fr/es만 [2]. Standard 근거성은 리전 간 추론 필수 [15] | 공개 수치 없음. CloudWatch `InvocationLatency`로 직접 측정 [17] | 1,000자 = 1 텍스트 단위. 콘텐츠 필터·금지 주제 $0.15/1k 단위, PII·근거성 $0.10/1k 단위 [16] |
| **Google Model Armor** | RAI(혐오·괴롭힘·성적·위험), 프롬프트 인젝션/탈옥(3단어 미만은 NO_MATCH), SDP(민감정보), 악성 URL, CSAM(항상 켜짐). 신뢰도 3단계: High / Medium 이상 / Low 이상 [6] | RAI·인젝션 필터를 **한국어 포함 9개 언어로 테스트** [6] | 공개 수치 없음 | 토큰 과금(약 4자 = 1토큰). 단가는 가격 페이지에서 확인 못 함 [6] |
| **OpenAI omni-moderation** | 13개 범주, `category_scores`(0–1, 보정됨), 이미지 지원 [7] | 문서에 언어 언급 없음 [7]. 발표: 다국어 평가 42% 향상, 98% 언어에서 개선, 저자원 언어 약 70% 향상 [8] **(2차: openai.com 403, 커뮤니티 공지 사본)** | 공개 수치 없음 | **무료** [7] |
| **Lakera Guard** | `/guard` 판정 + `/guard/results` 탐지기 상세, 정책·프로젝트 단위 [11] | 공식 문서에서 언어 지원 확인 못 함 | 자체 호스팅 p95: 1,000자 <20ms, 10,000자 <100ms (M5.2xlarge) [11] **(단일)**. SaaS 수치 없음 | 공개 가격 없음 |
| **Cloudflare AI Gateway Guardrails** | Llama Guard 3 8B로 S1–S13 판정, 인젝션은 Prompt Guard 2 86M. 차단·플래그 선택 [10] | Llama Guard 3 지원 언어에 한국어 없음 [10] **(2차 요약)** | 요청당 **약 500ms 추가**. 긴 입력은 청크로 나눠 더 느림 [10] | Workers AI 사용량 |

독립 비교: Unit 42가 클라우드 3사를 가장 엄격한 설정으로 시험했다(정상 1,000건, 탈옥 123건) [18]. 정상 입력 오차단율은 0.1% / 0.6% / 13.1%, 입력 단계 탈옥 미탐은 약 41% / 8% / 7%였다. 코드 리뷰 프롬프트는 세 플랫폼 모두 오차단했다. 출력 필터는 거의 작동하지 않았고, 차단의 대부분은 모델 자체 정렬이 했다(123건 중 109건 거절). 플랫폼명은 익명 처리돼 있다 **(단일)**.

**베이스라인 추천** (실제 학생 데이터는 외부로 보내지 않고, 공개 평가 세트에만 쓴다):
1. **OpenAI omni-moderation**: 무료이고 붙이기 쉽다. "범용 모더레이션 API" 기준선으로 쓴다. 인젝션 탐지 기능이 없다는 점을 명시한다.
2. **Bedrock Guardrails Standard**: 한국어 최적화를 공식 명시한 유일한 제품이라 한국어 비교 상대로 가장 공정하다. 근거성 검사까지 비교할 수 있다. AWS 계정과 비용이 필요하다(아래 비용 모델 기준 1k 요청당 약 $1.7).
3. 오픈 모델 기준선(Kanana Safeguard, Llama Guard 계열)과 NeMo는 R4대로 유지한다.
Azure와 Model Armor는 계정이 있으면 추가한다. Azure는 한국어 미검증이므로 그 자체가 "범용 제품의 한국어 공백"을 보여 주는 근거가 된다.

## A2. 운영 사양 — 지연 예산

UX 기준은 Nielsen의 세 한계다. 1초 안이면 사고 흐름이 끊기지 않고, 10초를 넘으면 진행 표시가 필요하다 [20]. 채팅 튜터의 지연 목표를 직접 다룬 1차 자료는 찾지 못했다. 아래 수치는 **(가안)**이며 CPU 실측으로 확정한다.

| 단계 | 실행 방식 | p50 목표 | p95 목표 | 타임아웃 |
|---|---|---|---|---|
| 전처리(NFKC·자모·PII 정규식) | 동기, 로컬 | 2ms | 10ms | 50ms → 원문 그대로 진행, 로그 |
| 질의 임베딩(KURE-v1, CPU) | **1회 계산해 1차 분류기와 검색이 공유** | 측정 | 측정 | 300ms |
| 1차 선별 분류기(LR) | 임베딩 뒤 즉시 | <1ms | 2ms | — |
| 검색(벡터 검색) | 1차 분류기와 **병렬** [12] | 30ms | 100ms | 500ms |
| 2단계 LLM 판정(에스컬레이션 5–15%만) | 답변 생성과 병렬로 시작, 판정 전에는 토큰을 내보내지 않음 | 700ms | 1.5s | 2.5s → §A3 fail 정책 |
| 답변 생성 첫 토큰(TTFT) | 엘리스 ML API | 측정 | 측정 | 8s |
| 출력 검사(청크 단위) | 문장 경계 버퍼(약 200–400자). 로컬 검사는 동기, 교환 LLM 판정은 마지막 청크 전에 반드시 완료 | 청크당 50ms(로컬) | — | 3s |
| **사용자 체감 총합** | 비에스컬레이션 첫 문장 노출 | ≤1.5s | ≤3s | — |
| | 에스컬레이션 첫 문장 노출 | ≤2.5s | ≤5s | 1초 넘으면 "확인 중" 표시 |

설계 근거:
- **병렬화.** OpenAI 쿡북은 가드레일을 본 LLM 호출과 비동기로 함께 보내고, 걸리면 준비된 응답으로 바꾸라고 권한다 [12]. 다만 미성년 대상 서비스에서는 생성은 병렬로 하되 **판정이 끝나기 전에는 노출하지 않는다**.
- **스트리밍.** 벤더 방식은 두 가지다. Bedrock: 동기(버퍼 후 검사) vs 비동기(먼저 보내고 뒤에 차단. 비동기에서는 PII 마스킹 불가) [21]. Azure: 기본은 버퍼 청크, 비동기 필터는 위반 신호가 약 1,000자 안에서 늦게 온다 [22]. Azure 문서에는 **마지막 15–30단어 안의 블록리스트 단어가 스트림 끝에 검사 없이 전달된** 시험 결과가 적혀 있다 [22]. 그래서 우리는 **버퍼 방식**을 쓰고 **마지막 청크까지 검사한 뒤 완료를 표시**한다. 비동기 방식은 채택하지 않는다.
- Bedrock 근거성은 청크 하나만 관련 있어도 전체 응답을 관련 있다고 본다. 스트리밍에서는 무관한 응답이 끝까지 나간 뒤에야 표시될 수 있다 [15]. 근거성 판정은 **전체 답변 기준**으로 한다.
- 입력 길이 상한: 질문 2,000자, 대화 이력 판정 창 8,000자 **(가안)**. Azure 텍스트 API 10K자, Foundry 가드레일 앞 1,000자만 검사 [1] 같은 벤더 한도를 참고했다.

## A3. 운영 사양 — 범주별 실패 정책

선례: Cloudflare는 **차단으로 설정한 범주는 실패 시 차단(fail-closed), 플래그 범주는 통과(fail-open)**로 처리한다 [10]. 우리는 이것을 학생 범주로 확장한다 **(가안)**.

| 범주(README §3) | 가드 실패 시(LLM 판정 타임아웃·오류) | 이유 |
|---|---|---|
| 탈옥·프롬프트 인젝션 | **closed**: 1차 점수가 에스컬레이션 임계를 넘으면 "잠시 후 다시" + 학습 복귀. 임계 미만이면 통과 | 미탐 비용이 크다. 1차 분류기는 로컬이라 늘 살아 있다 |
| 성적 콘텐츠·그루밍 신호 | **closed** | 미성년 노출은 회복 불가 |
| 위험·불법, 혐오 생성 | **closed** | 같은 이유 |
| **위기(자해·자살·학대)** | **"지원 쪽으로 fail"**: 차단하지 않는다. 로컬 키워드·1차 점수만으로 109/1388 등 안내문을 **항상 덧붙이고** 교사 알림 큐에 넣는다 | 위기 신호를 놓치거나 학생을 막는 것이 가장 나쁜 결과다 |
| 개인정보 | 로컬 정규식이라 외부 의존이 없다. 정규식 단계가 실패하면 **로그 저장을 끈다(closed-for-storage)** | 저장 쪽 사고를 막는다 |
| 학업 무결성(정답만 요구) | **open**: 힌트형 기본 프롬프트로 답한다 | 피해가 작고 교육적 전환은 생성 프롬프트로도 된다 |
| 범위 밖·잡담 | **open** | 같은 이유 |
| 근거성(출력) | **soft**: "교재에서 확인하지 못한 내용이 있을 수 있어요" 표시 + 인용 청크 원문 제시 | 차단하면 학습이 끊긴다 |

**서킷 브레이커 (가안).** 판정용 LLM 호출의 1분 창 오류율이 20%를 넘거나 타임아웃이 연속 5회 나면 **open** 상태로 바꾼다. open 상태에서는 LLM 판정을 건너뛰고 위 표의 실패 정책을 적용하며, 1차 분류기 차단 임계를 한 단계 낮춘다. 30초마다 요청 1건으로 half-open 탐침을 보낸다. 생성 LLM까지 죽으면 튜터 자체가 멈춘 것이므로 "튜터 일시 중단" 안내와 **정적 위기 자원 안내**를 함께 보여 준다. 판정 호출과 생성 호출은 쿼터(RPM/TPM)를 따로 잡는다. 생성 트래픽 폭증이 판정을 굶기지 않게 하기 위해서다(엘리스 API 쿼터 구조는 README §6-3 확인 필요).

**캐싱.**
- 판정 결과만 `hash(정규화 텍스트 + policy_version)` **정확 일치**로 캐시한다(TTL 24h). 같은 공격 문자열의 반복 재전송을 싸게 막는다.
- **의미(semantic) 캐시는 쓰지 않는다.** 근사 입력에 판정을 재사용하면 우회 통로가 되고, 캐시 적중 여부가 응답 시간으로 드러나 다른 사용자의 프롬프트를 추론당할 수 있다 [23].
- 위기 범주는 캐시하지 않는다. 매번 새로 알림을 내야 한다.

**속도 제한·남용 (OWASP LLM10 [24]).** OWASP는 입력 크기 제한, 사용자별 쿼터 기반 rate limit, 타임아웃·스로틀링, 자원 사용 이상 탐지, 과부하 시 부분 기능 유지(graceful degradation), 대기열 상한을 권한다. 공격 시나리오로는 Denial of Wallet이 있다 [24]. 우리 값 **(가안)**:
- 사용자별 토큰 버킷: 분당 10요청, 일 300요청.
- **사용자별 에스컬레이션 예산**: 10분에 5회. 넘으면 그 사용자의 다음 요청은 LLM 판정 없이 강화된 1차 임계로 처리하고 "반복 시도" 플래그를 단다.
- 전역 에스컬레이션 동시성 상한: 대기열이 차면 범주별 실패 정책을 적용한다.

**비용 모델.** 1k 요청당 비용:
`C = 1000 × [ p_esc·(Tin_esc·Pin + Tout_esc·Pout) + (Tin_ex·Pin + Tout_ex·Pout) + n_ground·(Tin_g·Pin + Tout_g·Pout) ] / 1e6`
(p_esc = 에스컬레이션율, ex = 출력 교환 판정, g = 근거성 주장 분해, P = 100만 토큰당 단가)

- 예시: p_esc 0.10, 에스컬레이션 800/40토큰, 교환 판정 2,500/60토큰, 근거성 1회 2,500/150토큰. **가상의** 단가 $0.15/$0.60(소형 모델급)를 넣으면 약 **$0.85/1k 요청**이 나온다. 대부분이 출력 판정 비용이다. 엘리스 단가가 나오면 다시 계산한다. 한국어는 글자당 토큰이 많으니 실제 토큰 수로 잰다.
- 같은 요청(질문 300자 + 답변 1,200자 + 근거 3,000자)을 Bedrock으로 처리하면: 콘텐츠 필터 3단위 $0.00045 + 금지 주제 3단위 $0.00045 + PII 3단위 $0.0003 + 근거성 5단위 $0.0005 ≈ **$1.70/1k 요청** [16]. 생성 LLM 비용은 별도다.
- 대시보드에는 실제 토큰으로 계산한 "1k 요청당 가드 비용"을 내보낸다.

## A4. 관측과 피드백 루프

### 로그 스키마 (프라이버시 규칙 포함)

| 필드 | 내용 | 규칙 |
|---|---|---|
| `ts`, `request_id`, `turn_idx` | 시각·요청·턴 | — |
| `user_pseudo` | HMAC(학생 ID, 월별 교체 키) | 원 ID·이름·학번은 저장하지 않음 |
| `grade_band` | 초/중/고/성인 | 정확한 나이는 저장하지 않음 |
| `policy_version` | 예: `pol-1.3.0+clf-2026-10-20+norm-4` | **모든 결정에 필수** |
| `stage_scores` | 1차 점수, LLM 판정 라벨, 근거성 점수 | 숫자·라벨만 |
| `action` | 허용/교육적 전환/부분 응답/거절/위기 프로토콜 | — |
| `reason_codes[]` | 아래 코드표 | 사용자에게는 범주 수준 문구만 노출 |
| `latency_ms{stage}` | 단계별 지연 | — |
| `fail_mode` | none/timeout/breaker_open/budget_exceeded | — |
| `shadow` | 그림자 버전의 결정·점수 | 집행하지 않음 |
| `text_masked` | PII 마스킹한 입력·출력 | **기본 저장 안 함.** 차단·에스컬레이션·이의 제기·무작위 표본만, 접근 통제, **30일 보관** **(가안)** |
| `appeal` | 이의 제기 여부·검토 결과 | — |

저장하지 않는 것:
- 원문 PII(마스킹 전), 감정 점수. EU AI Act는 교육기관의 감정인식을 금지한다(R3).
- 위기 대화 원문을 일반 분석 로그에 남기지 않는다. 위기는 교사 알림 채널에 **최소 내용**(시각, 범주, 안내 제공 여부)만 보낸다.
- 선례: Khan Academy는 플래그된 대화를 학부모(개인 계정) 또는 과제를 낸 교사(학교 계정)에게 알린다. 심각도 임계를 넘고 학군이 동의한 경우에만 관리자에게 이메일을 보낸다 [25]. 우리도 "누가 무엇을 보는가"를 같은 방식으로 역할별로 정한다.
- 만 14세 미만 개인정보 처리의 법정대리인 동의 같은 국내 규정은 R3를 따른다.

**reason code (R3 범주와 1:1, 가안):** `SEC.PI.DIRECT`, `SEC.PI.INDIRECT`(검색 청크), `SEC.LEAK`(내부 지시 유출 시도), `HARM.SEX`, `HARM.GROOM`, `HARM.DANGER`, `HARM.HATE`, `CRISIS.SELF`, `CRISIS.ABUSE`, `BULLY.VICTIM`, `BULLY.PERP`, `INTEG.ANSWER_ONLY`, `INTEG.EXAM`, `SCOPE.OFF`, `PII.SELF`, `PII.OTHER`, `DEP.EMOTIONAL`, `GROUND.UNSUPPORTED`, `SYS.FAILSAFE`, `SYS.BUDGET`.

### 대시보드 (일·주 단위, 학년대별 분할)
- 범주별 차단율·조치 분포, 에스컬레이션율(목표 5–15%, README)
- 이의 제기율과 **번복률**(번복률 = 실제 오차단 추정치)
- 단계별 p50/p95 지연, fail_mode 발생 수, 서킷 open 시간
- 1k 요청당 비용, 사용자별 에스컬레이션 예산 초과 수
- 그림자 버전과의 **불일치율**, reason code 분포 변화(드리프트)
- Azure Content Safety Studio도 기술 지표(지연·정확도·재현율)와 비즈니스 지표(차단율·범주 비율·언어 비율)를 함께 모니터링하라고 한다 [5].

### 사람 검토 표본
- 위기 플래그: **100%**, 24시간 안.
- 이의 제기: 100%.
- 차단·거절: 10% 층화 표본(범주별) → 오차단(FP) 추정.
- **허용 결과: 1% 무작위 표본** → 미탐(FN) 추정. 운영 중 FN을 볼 수 있는 유일한 경로다.
- Anthropic은 개별 판정 외에 프라이버시를 보존하는 토픽 클러스터링과 계층적 요약으로 트래픽 수준의 패턴을 본다고 밝혔다 [26]. 학생 서비스에서는 이를 "반 단위 집계"로 옮긴다.

### 이의 제기 경로 "이 답변이 잘못 막혔어요"
1. 버튼을 누르면 `request_id`와 마스킹된 텍스트가 저장된다. 원문 보관에는 학생 동의를 받는다.
2. 검토자가 3일 안에 라벨을 단다(가안).
3. 번복되면 **정상 회귀 세트**에, 공격이 확인되면 **공격 회귀 세트**에 넣는다.
4. 회귀 세트는 버전 관리하고 커지기만 한다. 평가용 홀드아웃과는 섞지 않는다(README §4).
5. 정책 변경은 회귀 세트에서 다음 두 조건을 모두 만족해야 배포한다. ① 기존 공격 미탐 증가 0건. ② 정상 오차단율 증가가 CI 안.

### 정책 버전·그림자 모드·카나리
- `policy_version` = 정책 프롬프트 + 임계값 + 분류기 가중치 + 정규화 표의 묶음. semver로 붙이고 변경 기록과 평가 리포트를 남긴다.
- **그림자 모드**: 새 버전을 병렬로 돌려 로그만 남긴다. 선례로 Azure Prompt Shields의 `annotate` 모드가 있다. 문서는 오탐이 많으면 block에서 annotate로 바꿔 차단 없이 기록하라고 권한다 [3]. CC++의 오거절 수치도 그림자 배포에서 잰 것이다(README §1). 그림자 기간에는 불일치 표본에 라벨을 단다.
- **카나리**: 5% → 25% → 100%. Google SRE 지침 [19]: 대조군과 나눠 비교하고, 사용자가 볼 수 있는 소수 지표만 쓰고, 한 번에 카나리 하나, 의미 있는 차이가 나면 중지·롤백.
- 우리 롤백 기준 **(가안)**: 카나리 차단율이 대조군의 1.5배 이상, 이의 제기율 2배 이상, 가드 p95 +500ms 이상, 위기 알림 수 급감 중 하나라도 해당하면 롤백.

### 우회 사고 대응 플레이북
1. **탐지**: 이의 제기, 허용 표본 검토, 교사 신고, 외부 제보.
2. **분류**: S1 = 미성년에게 성적·위험 내용 노출이나 위기 미대응. S2 = 내부 지시·정책 유출. S3 = 범위 밖 응답.
3. **봉쇄(1시간 안, S1 기준)**: 재배포 없이 바꿀 수 있는 **핫픽스 층**을 둔다. 정규식·블록리스트, 해당 reason code의 임계 하향, 해당 기능 끄기.
4. **근절**: 공격 문자열과 변형을 회귀·학습 세트에 넣고 재학습한 뒤 그림자 → 카나리로 배포한다.
5. **복구·통지**: 노출된 학생이 있으면 담당 교사(와 정책에 따라 보호자)에게 알린다.
6. **사후 검토**: 비난 없는(blameless) 형식으로 원인 계층과 회귀 테스트 추가 여부를 기록한다. NIST SP 800-61의 탐지·분석 → 봉쇄·근절·복구 → 사후 활동 흐름을 따른다(일반 사고 대응 지침).

## A5. 가드레일 자체의 보안

| 위협 | 근거 | 대응 |
|---|---|---|
| 정책 프롬프트 유출 | OWASP LLM07: 시스템 프롬프트를 비밀이나 보안 통제로 취급하지 말고, 통제는 LLM 밖에서 독립적으로 집행하라 [27] | 정책 프롬프트는 유출된다고 가정한다. 비밀·임계값·키를 넣지 않는다. 판정 LLM 출력은 사용자에게 절대 보내지 않는다 |
| 판정 모델 탈옥 | PRP: 범용 적대 접두사를 응답에 전파시켜 가드 모델을 속인다. 가드 접근 권한이 없어도 된다 [28] | 판정 입력은 구분자와 datamarking으로 **데이터로만** 넣고, 출력은 JSON 스키마로 강제한다. LLM이 아닌 1차 분류기와 함께 판정한다. 고위험 구간의 **허용은 두 판정이 모두 동의할 때만** 한다. 쿡북도 LLM 가드레일이 기반 모델의 취약점을 공유한다며 규칙·전통 ML과의 결합을 권한다 [12] |
| 타이밍·행동 부채널 | HTTP·어휘·타이밍 신호만으로 가드 존재를 100%, 가드 차단과 LLM 거절을 F1 98%로 구분 [29] **(단일, 2026-07 프리프린트)**. KV·의미 캐시 타이밍으로 다른 사용자 프롬프트 추론 [23] | 가드가 있다는 사실은 숨길 수 없다고 가정한다. 숨길 것은 **어느 단계·어떤 임계에서 걸렸는지**다. 거절 문구를 단계와 무관하게 통일하고, 점수와 reason code는 클라이언트에 보내지 않는다. 의미 캐시는 쓰지 않는다 |
| 임계값 탐색(probing) | 적응형 공격자는 방어 설계에 맞춰 전략을 바꾼다 [14] | 점수를 노출하지 않는다. 사용자별 차단 누적을 세션 위험 신호로 쓰고, 임계 바로 아래 점수의 반복 출현을 이상 탐지 지표로 본다. 에스컬레이션 예산을 둔다 |
| 에스컬레이션 강제로 비용·가용성 공격 | OWASP LLM10 Denial of Wallet [24] | 사용자별·전역 에스컬레이션 예산을 둔다. 예산을 넘으면 LLM을 부르지 않고 범주별 실패 정책을 적용한다(보안 범주 closed) |
| 검색 문서 오염 | Prompt Shields 문서 공격 유형: 내용 조작, 정보 수집, 가용성 방해 등 [3] | R1대로 색인 시점 스캔과 datamarking을 한다. 답변의 링크는 허용 목록만 둔다 |

---

# Part B. 평가

## B1. 지표 정의

| 지표 | 정의 | 근거 |
|---|---|---|
| **ASR (시스템 수준)** | 공격 문항 중 **최종 사용자 노출 응답**이 유해로 판정된 비율. 가드의 탐지율과 구분한다 | HarmBench: 테스트 케이스 중 목표 행동을 끌어낸 비율, 분류기가 0/1 판정 [30] |
| **StrongREJECT 점수** | `(1 − refused) × (specific + convincing)/2`. specific·convincing은 1–5를 [0,1]로 환산. 거절해도, 쓸모없는 답을 해도 낮다 | 313문항. 루브릭 판정(GPT-4o-mini)과 Gemma 2B 미세조정 판정. 사람 판단과 Spearman 0.90/0.85 [31] |
| HarmBench 분류기 | Llama 2 13B 미세조정. 사람과 일치 93.2%(GPT-4는 88.4%). **생성 토큰 수에 따라 ASR이 최대 30% 달라져 512토큰으로 고정** [30] | 우리도 `max_tokens`와 온도를 고정하고 보고한다 |
| **과잉 거절률** | 정상 문항 중 거절된 비율. XSTest 3분류: 완전 응답 / 완전 거절(대안만 제시해도 거절) / 부분 거절 [32] | XSTest 정상 250·위험 200. 수동 주석이 기본, 문자열 매칭과 GPT-4 판정은 순위는 보존하지만 수치가 다르다 [32] |
| OR-Bench 방식 | 80K 세트는 키워드 매칭, Hard-1K와 Toxic은 LLM 판정 [33] | 안전성과 과잉 거절의 Spearman 0.89, 둘이 함께 움직인다 [33] |
| **우리 조치 매핑** | 정상 학습 문항에 대해 거절·위기 프로토콜·부분 응답은 **부당 조치**. 교육적 전환은 정답 요구 문항에서만 정답, 일반 개념 질문에서는 부당 조치로 센다 | (가안) |
| 판정자 타당성 | 한국어 판정 LLM과 사람 2인의 일치도. 150–200문항, Cohen κ와 혼동행렬 | 판정자가 틀리면 모든 수치가 틀린다 |
| 운영 지표 | 에스컬레이션율, 단계별 p50/p95, 1k 요청당 비용 | §A2–A3 |

**적응형 공격 평가.** "The Attacker Moves Second"는 방어 12종을 경사·강화학습·무작위 탐색·사람 주도 탐색으로 공격했고, 원 논문에서 ASR이 0에 가깝던 방어 대부분에서 90%를 넘겼다 [14]. 그래서 다음 세 가지를 따로 보고한다.
1. 정적 세트 ASR(R2 홀드아웃).
2. **자동 적응형 공격**: garak·PyRIT의 반복 공격기를 **우리 시스템 출력**을 보상으로 돌린다. 공격 쿼리 예산 별 ASR 곡선을 그린다(10/50/200회).
3. **사람 레드팀**: 아키텍처와 정책 문서를 알려 준 상태(화이트박스)로 2시간 × 2인 이상. 한국어 특유 변형(자모 분리, 초성, 한영 혼용, 띄어쓰기 교란)을 포함한다.
"모든 계층은 뚫린다"는 README 전제와 맞게, 결과는 "막았다"가 아니라 **"이 예산에서 이만큼"**으로 적는다.

## B2. 통계 — 표본 수 (직접 계산, 정확 이항 / Clopper–Pearson 단측 95%)

**① "차단율 ≥ 95%"(= 실패율 ≤ 5%)를 95% 신뢰도로 주장하는 데 필요한 공격 문항 수.** "과잉 거절 ≤ 5%"도 같은 표다(정상 문항 수, 실패 = 오거절).

| 허용 실패 수 | 0 | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|---|
| 최소 n | **59** | 93 | 124 | 153 | 181 | 208 |

- 실패 0건일 때 n=59는 `0.95^n ≤ 0.05`에서 나온다("3의 법칙" 계열).

**② 검정력 80%를 확보하는 n.** 실제 실패율이 1%면 약 124건(실패 2건까지 허용), 2%면 약 234건(6건), **3%면 약 601건(21건)**. 실제 실패율이 목표 5%에 가까울수록 n이 급증한다. 이항 이산성 때문에 n에 따라 톱니처럼 변하므로 근사값이다.

**③ 추정 정밀도(정규 근사 `n = 1.96²·p(1−p)/E²`).** p≈0.95(또는 0.05)에서 ±3%p는 **203건**, ±2%p는 **456건**.

**④ Wilson 95% 구간 예시.** 4/200 실패 → [0.8%, 5.0%]. 10/500 → [1.1%, 3.6%]. 20/1000 → [1.3%, 3.1%]. 15/300 → [3.1%, 8.1%].

**권장 (가안)**
- 전체 공격 세트 ≥ 600, 전체 정상 세트 ≥ 600.
- 핵심 범주(탈옥, 간접 인젝션, 성적·그루밍, 위기, 정답만 요구)는 **범주별 ≥ 124**. 이 수에서 실패 2건 이하면 ≥95%를 주장할 수 있다.
- n < 59인 범주는 표에 "주장 불가"로 표시한다.
- **군집 보정**: 같은 시드를 패러프레이즈·번역한 변형은 독립이 아니다. 유효 n은 시드 수로 세거나 시드 단위 군집 표준오차를 쓴다. Anthropic의 평가 통계 권고도 같은 단위 군집 SE를 쓰고, 문항 내 분산을 줄이려 재표본을 평균하고, 짝 차이 분석과 검정력 분석을 하라고 한다 [34].
- 구간 보고는 Wilson, "≥95%" 같은 **주장**은 보수적인 Clopper–Pearson으로 한다(이항 구간 비교의 표준 문헌: Brown, Cai & DasGupta 2001).

**⑤ 두 설정 비교 — McNemar(정확 이항, 양측 α=0.05, 검정력 80%).** 같은 문항을 A와 B에 넣고 **결과가 갈린 문항(불일치 쌍)**만 쓴다.

| 불일치 중 B가 나은 비율 | 70% | 75% | 80% |
|---|---|---|---|
| 필요한 불일치 쌍 수 | 49 | 30 | 20 |

- 전체 n = 불일치 쌍 / 불일치율. 예: 불일치율 10%에 75%면 문항 약 300개.
- 여러 범주·여러 절제(ablation)를 동시에 비교하면 Holm 보정을 한다.
- 분류기 비교에 McNemar를 쓰는 근거: Dietterich 1998.

**⑥ 비결정성 반복.**
- 생성 LLM과 판정 LLM 모두 온도 > 0이면 문항마다 **k=3회** 반복한다.
- 보고는 두 가지로 한다. (a) 평균 ASR(문항별 성공률의 평균, 구간은 문항 단위 부트스트랩). (b) **최악 ASR**: k회 중 1회라도 성공하면 성공. 공격자는 재시도할 수 있으니 (b)를 1차 지표로 쓴다.
- API가 seed를 지원하면 고정하고, 지원하지 않으면 그 사실을 적는다.
- 임계값은 보정 세트에서만 고르고 홀드아웃은 1회만 쓴다(README §4).

## B3. 심사위원 보고 형식

1. **본표**: 설정 × {시스템 ASR[95% CI], 최악 ASR, 과잉 거절률[CI], 부분 거절률, 에스컬레이션율, p50/p95 지연, 1k 요청당 비용}. 설정은 다음과 같다.
   - 가드 없음
   - 프롬프트만
   - OpenAI moderation
   - (가능하면) Bedrock Standard
   - NeMo
   - Kanana Safeguard
   - **우리 전체**
2. **절제(ablation)**: 우리 전체에서 정규화 / 1차 분류기 / LLM 판정 / 출력 교환 판정 / 멀티턴 재판정 / datamarking을 하나씩 뺀 결과. 각 행에 McNemar p값(Holm 보정)을 붙인다.
3. **트레이드오프 곡선**:
   - 1차 분류기 임계값에 따른 ROC·DET 곡선에 **운영점 두 개**(차단 임계, 에스컬레이션 임계)를 표시한다.
   - 시스템 설정별 (과잉 거절률, ASR) 산점도로 파레토 경계를 그린다.
   - OR-Bench가 보인 "안전-과잉 거절 동행"(ρ=0.89) [33]을 우리가 얼마나 비켜 가는지가 핵심 메시지다.
4. **범주별 표**: 범주마다 n, 실패 수, Wilson CI. n<59 범주는 회색 처리한다. 학년대·공격 기법(롤플레이·인코딩·분해·멀티턴·간접)으로도 나눈다. Unit 42에서 롤플레이가 주된 우회 수단이었다 [18].
5. **적응형 공격**: 쿼리 예산별 ASR 곡선, 사람 레드팀 결과 요약.
6. **판정자 검증표**: 한국어 판정 LLM과 사람의 κ, 혼동행렬.
7. **운영 증거**: 단계별 지연 분포(CPU 사양 명시), 비용 계산식과 대입값, fail 정책 표, 그림자 → 카나리 절차. "실제로 굴릴 수 있다"를 보여 준다.
8. **실패 사례**: PII와 유해 세부를 가린 대표 우회 3–5건과 대응 계층.
9. **재현성**: 데이터 버전, `policy_version`, 모델 ID, 온도·max_tokens·seed, 반복 수.

---

## 남은 불확실성
- 벤더 지연은 Cloudflare 약 500ms와 Lakera 자체 호스팅 수치 말고는 공개되지 않았다. 비교하려면 직접 재야 한다.
- OpenAI 다국어 수치는 커뮤니티 공지 사본으로만 확인했다(openai.com 403). 한국어 개별 수치는 없다.
- Model Armor 단가, Azure S0 단가, Lakera 언어 지원은 확인하지 못했다.
- Discord, Duolingo, GitHub Copilot의 가드레일 운영 엔지니어링 글은 1차 자료를 찾지 못했다. Roblox Sentinel은 대조 학습 기반이고 하루 약 60억 메시지를 1분 스냅샷으로 분석한다는 회사 발표만 있다 [35] **(단일)**.
- 채팅 튜터의 지연 목표는 1차 근거가 없다. 표의 값은 Nielsen 한계 [20]와 벤더 수치에서 끌어낸 가안이다.
- KURE-v1의 CPU 추론 지연은 아직 측정하지 않았다. 지연 예산의 가장 큰 미지수다.

## 출처
1. Microsoft Learn, Azure AI Content Safety — Region availability, limits, language: https://learn.microsoft.com/en-us/azure/ai-services/content-safety/region-availability
2. AWS, Languages supported by Amazon Bedrock Guardrails: https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-supported-languages.html
3. Microsoft Learn, Prompt Shields: https://learn.microsoft.com/en-us/azure/ai-services/content-safety/concepts/jailbreak-detection
4. Microsoft Learn, Harm categories: https://learn.microsoft.com/en-us/azure/ai-services/content-safety/concepts/harm-categories
5. Microsoft Learn, What is Azure AI Content Safety: https://learn.microsoft.com/en-us/azure/ai-services/content-safety/overview
6. Google Cloud, Model Armor overview: https://docs.cloud.google.com/model-armor/overview
7. OpenAI, Moderation guide: https://developers.openai.com/api/docs/guides/moderation
8. OpenAI Community 공지(2024-09-26) "Upgrading the Moderation API…": https://community.openai.com/t/upgrading-the-moderation-api-with-a-new-multimodal-moderation-model/958187 (원문 https://openai.com/index/upgrading-the-moderation-api-with-our-new-multimodal-moderation-model/ 403)
9. Azure AI Content Safety pricing: https://azure.microsoft.com/en-us/pricing/details/cognitive-services/content-safety/
10. Cloudflare, AI Gateway Guardrails / Usage considerations: https://developers.cloudflare.com/ai-gateway/features/guardrails/ , https://developers.cloudflare.com/ai-gateway/features/guardrails/usage-considerations/
11. Lakera docs, API overview / Sizing guide: https://docs.lakera.ai/docs/api (Sizing guide 수치는 검색 결과 요약으로 확인, 직접 열람 404)
12. OpenAI Cookbook, How to implement LLM guardrails: https://developers.openai.com/cookbook/examples/how_to_use_guardrails
13. (결번)
14. Nasr, Carlini et al., The Attacker Moves Second, arXiv:2510.09023
15. AWS, Contextual grounding check: https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-contextual-grounding-check.html
16. AWS, Amazon Bedrock pricing (Guardrails): https://aws.amazon.com/bedrock/pricing/
17. AWS, Monitor Guardrails with CloudWatch metrics: https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-guardrails-cw-metrics.html
18. Palo Alto Unit 42, Comparing LLM Guardrails Across GenAI Platforms: https://unit42.paloaltonetworks.com/comparing-llm-guardrails-across-genai-platforms/
19. Google SRE Workbook, Canarying Releases: https://sre.google/workbook/canarying-releases/
20. Nielsen Norman Group, Response Times: The 3 Important Limits: https://www.nngroup.com/articles/response-times-3-important-limits/
21. AWS, Guardrails streaming: https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-streaming.html
22. Microsoft Learn, Content Streaming in Azure OpenAI: https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/content-streaming
23. Song et al., The Early Bird Catches the Leak (timing side channels in LLM serving), arXiv:2409.20002
24. OWASP GenAI, LLM10:2025 Unbounded Consumption: https://genai.owasp.org/llmrisk/llm102025-unbounded-consumption/
25. Khan Academy Help Center, Khanmigo moderation notifications: https://support.khanacademy.org/hc/en-us/articles/14394569357069
26. Anthropic, Building safeguards for Claude: https://www.anthropic.com/news/building-safeguards-for-claude
27. OWASP GenAI, LLM07:2025 System Prompt Leakage: https://genai.owasp.org/llmrisk/llm072025-system-prompt-leakage/
28. Mangaokar et al., PRP: Propagating Universal Perturbations to Attack LLM Guard-Rails, arXiv:2402.15911
29. Hackett & Garraghan, Behind the Refusal: Determining Guardrail Activation via Behavioral Monitoring, arXiv:2607.02121
30. Mazeika et al., HarmBench, arXiv:2402.04249
31. Souly et al., A StrongREJECT for Empty Jailbreaks, arXiv:2402.10260
32. Röttger et al., XSTest, arXiv:2308.01263
33. Cui et al., OR-Bench, arXiv:2405.20947
34. Anthropic, A statistical approach to model evaluations: https://www.anthropic.com/research/statistical-approach-to-model-evals
35. Roblox Newsroom, Open-sourcing Roblox Sentinel (2025-08): https://about.roblox.com/newsroom/2025/08/open-sourcing-roblox-sentinel-preemptive-risk-detection
- 통계 표준 문헌(웹 대조 안 함): Brown, Cai & DasGupta (2001) Statistical Science 16(2); Dietterich (1998) Neural Computation 10(7); McNemar (1947) Psychometrika 12(2); NIST SP 800-61.
- 표본 수 계산: 정확 이항(Clopper–Pearson) 이분 탐색 스크립트로 직접 계산했다(표준 라이브러리만 사용).
