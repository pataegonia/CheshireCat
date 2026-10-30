# R3. 학생용 가드레일은 범용 LLM 가드레일과 어떻게 달라야 하는가

- 작성일: 2026-10-29
- 방법: 하위 질문 5개, 다중 검색, 핵심 1차 출처 직접 확인(접근 가능한 것만). 모든 주장에 출처 번호 표기. `[단일출처]`는 출처가 하나뿐인 주장, `[추론]`은 근거에서 우리가 도출한 설계 판단, `[미확인]`은 검색으로 확인하지 못한 항목.
- 전체 신뢰도: **중간**. 교육 AI 제품의 내부 가드레일 동작은 대부분 벤더 공개자료나 보도 수준이라 독립 검증이 부족함. 학술 근거(MinorBench, Bastani PNAS, 아첨 연구)와 규제 원문은 비교적 탄탄함.
- 접근 실패: openai.com 공식 블로그 2건, Khan Academy 도움말 원문(403). 해당 내용은 검색 요약·2차 출처로 대체하고 표기함.

---

## 요약

1. **학생용 가드레일의 핵심 차이는 "무엇을 막느냐"보다 "막은 뒤 무엇을 하느냐"다.** 교육 제품들은 공통으로 (a) 범위 제한(교과 밖 대화 축소), (b) 정답 대신 힌트·질문(소크라테스식), (c) 교사·보호자에게 보이는 플래그/로그, (d) 위기 신호 시 상담 자원 안내를 붙인다 [1][2][6][7][9][11].
2. **학업 무결성은 학생용에서 독립된 안전 범주다.** 가드레일 없는 GPT-4를 쓴 고등학생은 연습 점수가 48% 올랐지만 이후 단독 시험에서 대조군보다 17% 낮았고, 힌트 위주 가드레일을 넣은 버전은 이 손해가 사라졌다 [12]. 범용 가드레일에는 이런 범주가 없다.
3. **연령 맥락만 넣어도 거절률이 크게 바뀐다.** MinorBench에서 "12세 대상" 문맥을 시스템 프롬프트에 넣자 GPT-4o-mini 거절률이 4.3%→56.2%로 올랐다. 반면 추론 모델(o3-mini, R1 계열)은 계속 낮았다 [4]. 모델이나 프롬프트에만 맡기면 안 되고 별도 분류 계층이 필요하다는 근거다 [추론].
4. **과잉 거절도 학습 피해다.** 벤치마크들은 표면 단어 때문에 무해한 질문을 거절하는 현상을 측정한다 [14][15]. OpenAI는 이분법적 거절 대신 "안전한 범위에서 최대한 돕는(safe-completion)" 방식이 이중용도 질문에서 안전성과 유용성을 함께 높였다고 보고했다 [16]. 다만 교육 맥락에서 과잉 거절이 학습을 해친다는 직접 실증은 찾지 못했다 `[미확인]`.
5. **위기(자해·자살) 신호는 차단 대상이 아니다.** 공감하며 응답하고, 상담 자원(109, 1388)을 안내하고, 교사에게 플래그를 남겨야 한다. OpenAI 청소년 정책 [3], Khanmigo의 플래그→보호자/교사 알림 [1], MagicSchool의 교사 대시보드 알림 [9]이 이 패턴이다.
6. **아첨(sycophancy)은 교육에서 안전 문제다.** 학생이 틀린 답을 제시하면 LLM 정확도가 최대 15%p 떨어졌다(소형 모델은 최대 30%) [17].
7. **한국 규제:** 만 14세 미만 개인정보는 법정대리인 동의가 필요하다(개인정보보호법 제22조의2) [21]. 교육분야 AI 윤리원칙(2022)은 "학습자 주도성"과 "교수자 전문성 존중"을 명시한다 [19]. AIDT는 2025년 8월 법 개정으로 '교육자료'로 지위가 바뀌었다 [24]. AI 기본법(2026-01-22 시행)의 고영향 영역에 교육이 들어가는지는 확인하지 못했다 `[미확인]` [23]. EU AI Act는 학습성과 평가와 시험 부정행위 감시를 고위험으로 분류하고, 교육기관의 감정인식은 금지한다 [26].

---

## Q1. 기존 교육 AI 튜터의 안전·가드레일 구현

| 제품 | 차단/제한 | 리다이렉트(교육적 전환) | 교사·보호자 가시성 | 위기 대응 | 근거 수준 |
|---|---|---|---|---|---|
| **Khanmigo** (Khan Academy) | 모더레이션으로 부적절·유해·위험 콘텐츠 탐지. 대화를 검증된 활동 범위로 제한 [1][8] | 단계별 지원과 피드백(정답 직접 제공보다 코칭) [8] | 플래그 시 가족 계정은 보호자에게 이메일·앱 알림, 학교 계정은 과제를 낸 교사에게 알림. 교육청은 심각도 임계 이상만 관리자 이메일 수신 선택 가능. 교사에게 활동 요약 제공 [1][8] | 자해 전용 처리 절차는 공개 자료에서 찾지 못함 `[미확인]` | 공식 도움말(검색 요약), Common Sense 평가 |
| **CS50 Duck** (Harvard) | ChatGPT와 같은 API지만 "교육적 가드레일"로 학생이 직접 코드를 쓰게 유도 [10] | 정답 대신 힌트로 유도 [10]. few-shot 시스템 프롬프트와 파인튜닝으로 교육 목표에 정렬 [10] | 강의 정책 차원(세부는 원문 미확인) | `[미확인]` | 보도·논문 목록. 원문 PDF는 텍스트 추출 실패 |
| **Jill Watson** (Georgia Tech) | 강의 자료 맥락이 부족하면 답하지 않음. 생성 답변을 텍스트 함의(entailment)로 맥락과 대조한 뒤 반환 [11] | 근거 기반 답변만 제공 | `[미확인]` | `[미확인]` | 대학 보도자료(설계 주장) |
| **Coursera Coach** | 기관용 '채점 항목 잠금' 등 학업 무결성 기능 [13] | 강의 요약, 가이드 연습, 평가 전 복습 [13] | 기관(대학) 관리 기능 | `[미확인]` | 벤더 블로그. "채점 문항에 답하지 않음"은 확인 못함 |
| **엘리스 AI 헬피 (AIDT용)** | 부적절한 언어 방지 모더레이터, 팩트체커, 환각 방지, 연령에 맞춘 톤 변환 에이전트로 이루어진 멀티 에이전트 구조 [5] | 연령 친화 톤 변환 [5] | `[미확인]` | `[미확인]` | **[단일출처]** 언론 보도(회사 발표 기반) |
| **MagicSchool** (학생용 Rooms) | 다중 분류기, 단어·구문 탐지, AI 분석으로 실시간 탐지 [9]. 스레드당 약 50메시지 제한, AI 고지 [9] | `[미확인]` | 교사·교육청 관리자가 모든 학생 대화를 실시간 열람. 플래그 시 대시보드·이메일 알림. 교사가 방 잠금·일시정지·학생 퇴장 가능 [9] | Claude 기반 안전 계층으로 월 800만~1,000만 메시지를 처리하고 자해·정신적 고통 신호의 오탐을 3배 줄였다고 주장 [9] (벤더 사례) | 공식 도움말과 벤더 사례 |
| **ChatGPT Study mode** (2025-07-29) | — | 정보를 조금씩 주고 이해를 확인한 뒤 다음으로 진행. 사용자가 끌 수 있음 [6] | — | — | 보도 |
| **Gemini Guided Learning** (2025-08) | — | 단계별 설명, 탐색 질문, 막히면 힌트 [6] | — | — | 보도 |
| **Claude Learning mode** (2025-04-02) | — | 바로 풀이를 주지 않고 "어떻게 접근할래?" 같은 유도 질문 [7] | — | — | Anthropic 발표, 보도 |
| **OpenAI 청소년 정책** (2025-09~2026-01) | 18세 미만 추정 시 더 안전한 경험이 기본값. 연령이 불확실해도 안전한 쪽 적용 [3] | 자해·심각한 고통 대화는 즉흥 상담 대신 위기 자원으로 연결 [3](2차 출처) | 보호자 계정 연동, 사용 금지 시간 설정, 기억 기능 관리, 급성 고통 징후 시 보호자 알림 [3] | 위와 같음 | OpenAI 자료(검색 요약), 2차 해설 |
| **Character.AI** | 18세 미만은 자유형 대화 금지(2025-11-25 시행 발표), 전환기에는 하루 2시간 제한, 자체 연령 확인과 Persona 사용 [18] | 영상·스토리 등 창작형 18세 미만 경험 [18] | — | — | 보도 |
| **Anthropic API (미성년 서비스 조직)** | 연령 확인, 콘텐츠 필터링, 모니터링·신고, 아동안전 시스템 프롬프트 사용, COPPA 등 준수 공개, AI임을 고지 [20] | — | 조직 책임 | — | 공식 도움말 |
| **Duolingo** | 조사 범위 대비 관련 공개자료를 찾지 못함 `[미확인]` | | | | |

**정리 (사실):** 공개자료로 확인되는 공통 패턴은 ① 범위 제한, ② 정답 대신 유도, ③ 교사/보호자 알림 및 열람, ④ 연령 추정 실패 시 안전한 쪽 기본값이다. 위기 대응의 세부 절차(누가, 언제, 무엇을)는 거의 모든 교육 제품이 공개하지 않는다.

---

## Q2. 범용 유해성 이외의 학생 특화 위험 범주

### 2.1 기존 분류체계
- **MinorBench 6범주** [4]: Danger(위험·불법 행위), Sexual(그루밍·착취 포함), Profanities(욕설·비하어 설명이나 정상화 포함), Hateful, Self-harm(극단적 다이어트 포함), Substance Use(미성년 불법 약물·술·담배).
  - 중학교 실사용 로그(약 26,800개 프롬프트)에서 관찰된 것: 성적 질문(무해한 것과 우려되는 것 혼재), 불법 행위를 "합법적으로 하는 법"으로 포장한 질문, 챗봇을 향한 욕설과 인종 비하어, 자기 존재를 부정하는 자해 관련 발화. 교사들은 학생이 혼자 쓸 때 부적절한 질의가 훨씬 많았다고 했다 [4].
- **Safe-Child-LLM** [27]: 거절/응답의 이분법 대신 0~5 단계 행동 라벨로 평가.
- **UNICEF AI와 아동 지침 3.0 (2025)** [22]: 아동 중심 AI 10개 요건(안전, 데이터·프라이버시, 비차별, 설명가능성·책임, 아동 최선의 이익·발달·웰빙, 포용 등). 3.0판에 AI 동반자(companion) 이슈가 새로 들어감.
- **청소년보호법**: 유해매체물 판단 축은 선정성·폭력성·사행성·약물 [25]. 시행령 별표의 세부 문구는 확인하지 못함 `[미확인]`.

### 2.2 범용 분류에 없거나 약한 학생 특화 범주 (근거 포함)
| 범주 | 왜 학생에서 특별한가 | 근거 |
|---|---|---|
| 학업 무결성(정답만 요구, 과제·시험 대리) | 가드레일 없는 AI 사용은 단독 시험 성과를 17% 떨어뜨림 | [12] |
| 과의존·인지적 위임 | 같은 연구에서 학생이 AI를 "목발(crutch)"처럼 씀 | [12] |
| 아첨·잘못된 전제 수용 | 학생의 틀린 제안이 정확도를 최대 15~30%p 낮춤. 덜 아는 학생일수록 오개념이 강화됨 | [17] |
| 위기 노출(자해·자살·학대) | 실로그에 자해 발화가 나타남. 13~14세가 AI 조언을 더 신뢰함 | [4][28] |
| 정서적 의존·동반자화 | 미국 10대 72%가 AI 동반자를 써봤고 약 1/3은 중요한 일을 사람 대신 AI와 상의함. Common Sense는 18세 미만의 AI 동반자 사용 비권장 | [28] |
| 성적 콘텐츠·그루밍 위험 | MinorBench Sexual 범주가 그루밍·착취를 포함. 학생의 성적 호기심 질문은 무해한 것과 우려되는 것이 섞임 | [4] |
| 또래 괴롭힘·혐오 | 실로그에 비하어와 혐오 표현 존재. 학교폭력은 117 신고 체계가 따로 있음 | [4][31] |
| 본인·타인 PII(학번, 연락처, 얼굴) | 14세 미만은 법정대리인 동의 필요. 아동 대상 데이터 최소화·고프라이버시 기본값 | [21][29] |
| 연령 부적합 정보(약물·술·도박) | 미성년에게 불법인 행위에 대한 조장·미화 | [4][25] |
| 사실 오류에 대한 과신 | 신뢰받는 학습 플랫폼 안에 있으면 학생이 답을 덜 의심함 | [8] |

---

## Q3. 교육에서의 과잉 거절

- **현상 측정:** OR-Bench는 겉보기엔 유해하지만 실제로는 무해한 프롬프트 8만 개(10개 거절 범주)로 과잉 거절을 측정한다 [14]. XSTest(250개)는 안전/불안전 쌍으로 구성되며, 표면 어휘만으로 거절이 유발되는 현상을 보인다 [15].
- **대안 패러다임:** OpenAI의 safe-completion은 사용자 의도를 이분법으로 판정하지 않고 출력의 안전성을 기준으로 삼는다. GPT-5에서 이중용도 질문(예: 불꽃놀이 점화 에너지)에 대해 안전성과 유용성이 모두 개선됐고, 실패할 때도 심각도가 낮았다고 보고했다 [16] (자사 평가 **[단일출처]**).
- **교육 맥락의 함의 [추론]:**
  - "SQL 인젝션은 어떻게 동작해?", "염산과 표백제를 섞으면 왜 위험해?" 같은 질문은 교과(정보, 화학) 학습 목표와 직접 겹친다. 원리, 방어, 안전 수칙 수준에서 답하고 실행 가능한 공격 절차(실제 페이로드, 표적, 양·조건)만 빼는 부분 응답이 맞다.
  - MinorBench v3("관련 없는 것은 답하지 말라")은 거절률이 가장 높았다(GPT-4o-mini 97%) [4]. 이렇게 하면 과잉 거절이 늘어날 것으로 보이며, 교과 범위 내 호기심 질문까지 막을 위험이 있다 [추론].
  - Khanmigo처럼 범위를 좁히는 방식은 위험을 효과적으로 낮춘다는 평가를 받는다 [8]. 따라서 "범위 밖이면 거절"이 아니라 "범위 밖이면 교과로 연결해 리다이렉트"하는 것이 학습 손실이 적다 [추론].
- **교육적 거절 스타일:** CS50 Duck, Claude Learning mode, Study mode 모두 거절 대신 질문과 힌트로 전환한다 [6][7][10]. 공개 자료에는 훈계형 문구가 보이지 않는다. 훈계를 하지 않는 것이 더 효과적이라는 직접 실증은 찾지 못했다 `[미확인]`.

---

## Q4. 규제·정책

### 4.1 한국
| 항목 | 핵심 내용 | 상태 / 근거 |
|---|---|---|
| 교육분야 인공지능 윤리원칙 (교육부, 2022-08) | 대원칙 "사람의 성장을 지원하는 인공지능"과 10개 세부원칙: 학습자 주도성·다양성, 교수자 전문성 존중, 교육당사자 관계 유지, 공정성, 안전 보장, 데이터 투명성·설명가능성, 합목적적 활용과 프라이버시 등 | [19] (세부원칙 문구는 **[단일출처]** 보도) |
| AI 디지털교과서 | 2023-08 개발 가이드라인: CSAP '중' 이상 보안 인증, 중간·기말 평가 정보는 수집하지 않음 [30]. 2025-08-04 초중등교육법 개정으로 AIDT가 '교육자료'로 바뀜 [24] | 검정 기준의 유해성 세부 항목은 `[미확인]` |
| 개인정보보호법 제22조의2 | 만 14세 미만은 법정대리인 동의를 받고 확인해야 함. 아동에게는 이해하기 쉬운 형식과 언어로 고지 | [21] |
| 개인정보위 생성형 AI 개발·활용 개인정보 처리 안내서 (2025-08-06) | 목적 설정, 전략 수립, 학습·개발, 배포·관리의 4단계별 쟁점과 안전조치. AI 프라이버시 거버넌스 | [32] (아동 관련 세부 조항은 `[미확인]`) |
| 아동·청소년 개인정보 보호 가이드라인 (개인정보위) | 원문 확인 실패 | `[미확인]` |
| AI 기본법 (2026-01-22 시행) | 고영향 AI는 열거된 영역(에너지, 먹는 물, 보건의료 등)에 해당. 사업자는 해당 여부를 사전 검토하고 위험관리, 이용자 보호, 사람의 감독 의무를 짐. 과태료는 최소 1년 계도기간 | [23]. **제2조제4호 차목에 「교육기본법」 제9조제1항에 따른 유아·초·중등교육의 학생 평가가 포함됨 (2차 검증으로 확인, R9·verification.md).** 학습 보조는 해당 가능성이 낮지만 채점 기능을 넣으면 해당 [추론] |
| 청소년보호법 | 유해매체물 판단 축: 선정성, 폭력성, 사행성, 약물 | [25] |

**학생에게 안내할 위기 자원 (2026-10 기준 확인분)**
| 자원 | 번호/채널 | 근거 |
|---|---|---|
| 자살예방 상담전화 | **109** (2024-01부터 1393 등 통합, 24시간) | [33] |
| 자살예방 SNS 상담 '마들랜' | 카카오톡 '마들랜', 앱, 문자 109 (24시간) | [34] |
| 청소년상담 1388 | 전화 1388, 문자 1388, 카카오톡 '청소년상담1388' (24시간, 9~24세) | [35] |
| 학교폭력 신고 | 117 (24시간). 문자 #0117은 **[단일출처]** 블로그 | [31] |
| 긴급 | 112, 119 | 일반 상식 |

참고: 109는 수요 대비 응대율이 절반에 못 미친다는 보도가 있다 [34]. 그래서 109 하나만 안내하지 말고 1388, 마들랜(문자)을 함께 안내하는 편이 낫다 [추론].

### 4.2 해외
- **COPPA 개정 (FTC)**: 2025-06-23 시행, 일반 준수기한 2026-04-22 [36]. 13세 미만 대상.
- **UK Age Appropriate Design Code (ICO, 15개 표준)**: 아동 최선의 이익, 고프라이버시 기본값, 프로파일링은 기본 꺼짐, 개인정보 제공·사용시간 연장을 유도하는 넛지 금지, 위치정보 기본 꺼짐 [29].
- **EU AI Act**: Annex III 3항에 따라 학습성과 평가(3(b))와 시험 중 부정행위 감시(3(d))가 고위험 [26]. 고위험 적용은 Digital Omnibus로 2027-12-02로 연기됐다고 보도됨 [26] (2차 해설). 교육기관 감정인식은 2025-02-02부터 금지 [26].
- **UNICEF 지침 3.0** [22], **Anthropic 미성년 서비스 요건** [20] (Q1 참고).

---

## Q5. 설계 시사점 [추론, 근거 연결]

1. **차단하지 않고 공감으로 다룰 범주:** 자해·자살 신호, 학대·괴롭힘 피해 고백, 정서적 고통. 응답은 공감, 안전 확인, 위기 자원(109/1388/마들랜/117) 순서로 하고, 대화는 끊지 않는다. 교사 플래그는 심각도가 높을 때만 즉시 보낸다 [1][3][9].
2. **부분 거절(우회형)할 범주:** 정답·풀이만 요구하는 요청(→ 힌트, 단계 질문) [6][7][10][12], 이중용도 교과 질문(→ 원리·방어만 설명, 실행 절차는 제외) [16], 범위 밖 잡담(→ 교과로 연결).
3. **전면 거절할 범주:** 성적 콘텐츠, 그루밍성 요청(성인 역할극, 비밀 유지 요구, 만남 유도), 자해 방법, 미성년 약물 조달, 타인 PII 수집·유포, 혐오·괴롭힘 문구 생성 [4][21].
4. **응답 형식:** 1문장 이유 + 학습 대안 1개. 훈계 금지. 연령별로 톤을 바꾼다(엘리스도 톤 변환 에이전트를 둠 [5]).
5. **연령 미상이면 보수적으로:** 연령을 모르면 가장 어린 학년 정책을 적용한다 [3].
6. **교사용 로그:** 이벤트별로 범주, 심각도, 조치를 기록한다. 원문 전체 노출은 최소화하고 위기·안전 플래그만 원문 일부를 함께 남긴다. 기본값은 고프라이버시 [29][19]. 감정 "인식" 점수는 만들지 않는다(EU 금지 취지 [26]). 위기 키워드 플래그는 감정 추론이 아니라 내용 분류다.
7. **아첨 방지:** 학생이 반박할 때 근거 없이 정정을 철회하지 않도록 하고, 이를 출력 검사 항목으로 넣는다 [17]. 잘못된 전제는 RAG 근거와 대조한다(Jill Watson의 entailment 방식 [11]).
8. **PII:** 입력 단계에서 학번, 전화번호, 주소, 주민번호를 마스킹하고 저장하지 않는다 [21][32].
9. **시스템 프롬프트만으로는 부족:** 같은 프롬프트에서도 모델 간 거절률이 22~97%로 흩어졌다 [4]. 독립된 분류기와 규칙 계층이 필요하다.

---

## 학생용 가드레일 vs 범용 가드레일 차이

| 차원 | 범용 LLM 가드레일 | 우리(학생용) | 근거 |
|---|---|---|---|
| 보호 대상 가정 | 성인, 자율적 판단 | 미성년, 발달 단계, 연령 미상이면 최연소 기준 | [3][4][22] |
| 안전 범주 | 폭력, 성, 혐오, 자해, 불법, CBRN | + 학업 무결성, 과의존, 아첨, 정서 의존, 그루밍, 또래 괴롭힘, 학생 PII | [4][12][17][28] |
| 거절 단위 | 이분법(응답/거절) | 단계형: 허용 / 교육적 전환 / 부분 응답 / 거절 / 위기 프로토콜 | [16][27] |
| 정답 요청 | 그대로 답함 | 힌트·질문으로 전환(정답은 학생 시도 뒤에) | [6][7][10][12] |
| 위기 발화 | 거절하거나 핫라인 한 줄 | 공감 응답 + 한국 자원(109/1388/마들랜/117) + 교사 플래그, 대화 유지 | [1][3][33][34][35] |
| 이중용도 교과 질문 | 키워드 기반 거절 경향 | 원리·방어·안전 수칙은 답하고 실행 절차만 제외 | [14][15][16] |
| 범위 밖 질문 | 대부분 응답 | 교과로 리다이렉트(완전 거절은 과잉 거절 위험) | [4][8] |
| 성적 콘텐츠 | 성인은 일부 허용 | 전면 차단. 성교육성 질문은 교과 수준으로만 답하고 보호자·교사 안내 | [4][25] |
| 사실성 | 선택 사항 | 필수: RAG 근거 대조, 아첨 방지(반박 시 정정 유지) | [8][11][17] |
| 응답 톤 | 중립·성인 | 연령별 톤, 짧은 이유 + 학습 대안, 훈계 금지 | [5][7] |
| 가시성 | 사용자 본인만 | 교사(필요시 보호자)가 보는 플래그·요약. 심각도 임계 기반 알림 | [1][9] |
| 개인정보 | 일반 PII 마스킹 | 14세 미만 법정대리인 동의, 학번·연락처 마스킹, 고프라이버시 기본값, 감정인식 없음 | [21][26][29] |
| 사용 패턴 | 제한 없음 | 과의존·장시간 사용 신호 모니터링(MagicSchool 50메시지 제한 사례) | [9][28] |
| 평가 방법 | 일반 레드팀 | MinorBench식 아동 범주 + 과잉 거절 세트(교과 이중용도) + 아첨 테스트 | [4][14][17] |

### 제안 학생용 범주 체계와 기본 조치

| ID | 범주 | 예시 | 기본 조치 | 교사 플래그 |
|---|---|---|---|---|
| S0 | 정상 학습 | 개념 질문, 풀이 확인 | 허용 | 없음 |
| S1 | 정답만 요구 / 과제 대리 | "답만 줘", "이 에세이 써줘" | **교육적 전환**: 힌트, 단계 질문, 학생 시도 후 피드백 | 반복 시 요약에만 |
| S2 | 시험 부정행위 | "지금 시험 중인데 3번 답" | **부분 거절**: 짧은 이유 + 개념 복습 제안 | 낮음(집계) |
| S3 | 이중용도 교과 호기심 | SQL 인젝션 원리, 위험한 화학반응 이유 | **부분 응답**: 원리·방어·안전 수칙만, 실행 절차 제외 | 없음 |
| S4 | 범위 밖 / 잡담 | 게임, 연예 | **리다이렉트**: 짧게 응답한 뒤 교과로 연결 | 없음 |
| S5 | 위기: 자해·자살·학대 피해 | "죽고 싶어", "집에서 맞아" | **위기 프로토콜**: 공감, 안전 확인, 109·1388·마들랜(급하면 112/119). 차단하지 않음 | **즉시(높음)** |
| S6 | 괴롭힘: 피해 고백 | "반 애들이 따돌려" | 공감 + 117/1388 + 믿을 만한 어른 권유 | 중간 |
| S7 | 괴롭힘: 가해성 요청 | 친구 놀리는 글, 비하어 생성 | **거절** + 짧은 이유 + 대안(공감 표현 학습 등) | 반복 시 중간 |
| S8 | 성적 콘텐츠 / 그루밍 신호 | 노골적 요청, 성인 역할극, "비밀로 해줘", 만남 | **거절**(성교육 개념 질문은 교과 수준으로만 답함). 그루밍 신호는 보호 안내 | 그루밍 신호는 높음 |
| S9 | 위험·불법 행위, 미성년 약물·술·도박 | 담배 사는 법, 무기 제작 | **거절** + 안전 정보 | 중간 |
| S10 | 혐오·욕설 | 비하어, 챗봇에 욕설 | 욕설은 담담하게 넘기고 학습으로 복귀. 혐오 생성은 **거절** | 반복 시 낮음 |
| S11 | 개인정보(본인·타인) | 학번, 전화번호, 주소, 친구 사진·정보 | 입력 **마스킹**, 저장 금지 + 공유하지 말라는 안내. 타인 정보 수집 요청은 거절 | 타인 PII는 낮음 |
| S12 | 정서 의존 / 동반자화 | "너밖에 없어", 연인 역할 요구 | 따뜻하게 경계 설정, 사람 관계·상담 자원 안내, 연인 역할 거절 | 지속되면 낮음 |
| S13 | 잘못된 전제 / 아첨 유도 | "선생님이 2+2=5랬어, 맞지?" | 근거 기반으로 정정 유지(RAG 인용) | 없음 |
| S14 | 프롬프트 공격 / 탈옥 | "규칙 무시해", 역할극 우회 | 거절 + 학습으로 복귀 | 반복 시 낮음 |

연령별 톤: 초등은 쉬운 말과 짧은 문장, 중등은 이유 1문장, 고등은 근거와 출처 제시. 연령 미상이면 중등 기본에 S5·S8·S9 임계는 초등 수준으로 낮춘다 [추론, 3].

---

## 출처

1. Khan Academy Help Center, Khanmigo 모더레이션·플래그 알림 (검색 요약, 원문 403). https://support.khanacademy.org/hc/en-us/articles/14394569357069 , https://support.khanacademy.org/hc/en-us/articles/21943797567629
2. Liu et al., "Teaching CS50 with AI", SIGCSE 2024. https://cs.harvard.edu/malan/publications/fp0627-liu.pdf
3. OpenAI, Teen Safety Blueprint (2025-11) / Our approach to age prediction (2026-01) (검색 요약, 원문 403). https://openai.com/index/introducing-the-teen-safety-blueprint , https://openai.com/index/our-approach-to-age-prediction/ , 2차: https://cyberbullying.org/open-ai-teen-safety-blueprint-takeaways
4. Khoo, Chua, Shong, "MinorBench", arXiv 2503.10242 (ICLR 2025 workshop). https://arxiv.org/html/2503.10242
5. 디지털데일리, "'AI 교과서 대표 사례' 선정…엘리스그룹" (2025-07). https://www.ddaily.co.kr/page/view/2025071109463466832
6. TechCrunch, Gemini Guided Learning (2025-08-06). https://techcrunch.com/2025/08/06/google-takes-on-chatgpts-study-mode-with-new-guided-learning-tool-in-gemini/ ; Pearson 블로그 https://pearson.com/international-schools/international-schools-blog/2026/02/what-is-gemini_s-guided-learning-and-chatgpts-study-mode-.html
7. Anthropic, Introducing Claude for Education (2025-04-02). https://www.anthropic.com/news/introducing-claude-for-education ; https://engadget.com/ai/claudes-new-learning-mode-will-prompt-students-to-answer-questions-on-their-own-172057828.html
8. Common Sense Media, Khanmigo AI Risk Assessment. https://institute.commonsensemedia.org/risk-assessments/khanmigo
9. MagicSchool Help: 플래그 https://help.magicschool.ai/en/articles/9940070-how-does-magicschool-ai-ensure-student-safety-by-flagging-inappropriate-chats ; 메시지 제한 https://help.magicschool.ai/en/articles/12738628-student-safety-updates-message-limits-and-ai-disclosures ; https://www.magicschool.ai/privacy/quality ; Claude 사례 https://claude.com/customers/magicschool-qa
10. Harvard Magazine, "Embracing AI" (2023-08). https://www.harvardmagazine.com/2023/08/ai-in-education ; https://cs50.readthedocs.io/cs50.ai/
11. Georgia Tech, "Jill Watson outperforms ChatGPT in real classrooms". https://www.cc.gatech.edu/news/georgia-techs-jill-watson-outperforms-chatgpt-real-classrooms ; https://edsurge.com/news/2023-06-22-georgia-tech-is-trying-to-keep-a-chatgpt-powered-teaching-assistant-from-hallucinating
12. Bastani et al., "Generative AI without guardrails can harm learning", PNAS 122(26), 2025. https://ideas.repec.org/a/nas/journl/v122y2025pe2422633122.html ; https://knowledge.wharton.upenn.edu/article/without-guardrails-generative-ai-can-harm-education
13. Coursera Blog, academic integrity features. https://blog.coursera.org/deliver-academic-integrity-with-these-new-features ; https://www.coursera.org/campus/academic-integrity
14. Cui et al., OR-Bench. https://www.arxiv.org/pdf/2405.20947
15. The Refusal–Compliance Tradeoff audit (XSTest vs OR-Bench). https://arxiv.org/pdf/2605.05427
16. OpenAI, From Hard Refusals to Safe-Completions, arXiv 2508.09224. https://openai.com/index/gpt-5-safe-completions ; https://arxiv.org/pdf/2508.09224
17. Arvin, "Check My Work?: Measuring Sycophancy in a Simulated Educational Context". https://arxiv.org/html/2506.10297v1 ; "Sycophancy is an Educational Safety Risk" https://arxiv.org/pdf/2605.14604
18. The Record, Character.AI minors ban. https://therecord.media/character-ai-minors-banned ; https://www.law360.com/mlex/technology/articles/2405162/character-ai-to-introduce-age-assurance-block-under-18s-from-general-chat
19. 스마트투데이, 교육분야 AI 윤리원칙 최초 마련 (2022-08). https://www.smarttoday.co.kr/ko-kr/articles/24171
20. Anthropic, Guidelines for organizations serving minors. https://support.claude.com/en/articles/9307344-responsible-use-of-anthropic-s-models-guidelines-for-organizations-serving-minors
21. 개인정보 보호법 제22조의2. https://korea.legal/wiki/%ea%b0%9c%ec%9d%b8%ec%a0%95%eb%b3%b4-%eb%b3%b4%ed%98%b8%eb%b2%95-%ec%a0%9c22%ec%a1%b0%ec%9d%982/ (정본: law.go.kr)
22. UNICEF Innocenti, Guidance on AI and Children 3.0 (2025). https://www.unicef.org/innocenti/reports/policy-guidance-ai-children
23. AI 기본법 시행령 입법예고 해설: 대륜 https://www.daeryunlaw.com/newsletter/news/246 ; 다음/AI 기본법 톺아보기④ https://v.daum.net/v/20260304084731731 ; BKL https://www.bkl.co.kr/law/insight/newsletter/6245
24. 헤럴드경제, "AI교과서 끝내 '교육자료'로" (2025-08). https://www.heraldk.com/article/2025080323300795569 ; 이투데이 https://www.etoday.co.kr/news/view/2493911
25. 청소년 보호법 시행령. https://ko.wikisource.org/wiki/%EC%B2%AD%EC%86%8C%EB%85%84_%EB%B3%B4%ED%98%B8%EB%B2%95_%EC%8B%9C%ED%96%89%EB%A0%B9 ; 이투데이 https://www.etoday.co.kr/news/view/643771
26. EU AI Act Annex III 해설: TNW https://thenextweb.com/news/mit-ai-undergraduate-assignments-eu-ai-act-proctoring-annex-iii ; CASRAI https://casrai.org/news/ai-act-annex-iii ; https://edlaw.eduverse.com/docs/jurisdiction/int/eu/ai-act/
27. Safe-Child-LLM, arXiv 2506.13510. https://arxiv.org/pdf/2506.13510
28. Common Sense Media, "Talk, Trust, and Trade-Offs" (2025) 보도: https://sciencealert.com/almost-75-of-american-teens-have-used-ai-companions-study-finds ; https://www.psychologytoday.com/us/blog/smart-parenting-smarter-kids/202509/teens-trust-and-ai-companions-what-parents-need-to-know
29. ICO, Age Appropriate Design Code 2.1. https://ico.org.uk/media/for-organisations/uk-gdpr-guidance-and-resources/childrens-information/childrens-code-guidance-and-resources/age-appropriate-design-a-code-of-practice-for-online-services-2-1.pdf
30. 뉴시스, "AI 교과서, 학생 학습시간·성취도 기록 수집한다" (2023-08). https://www.newsis.com/view/NISX20230829_0002429906
31. 117 학교폭력 신고센터: 정책브리핑 https://www.korea.kr/news/examPassView.do?newsId=148725759 ; #0117은 [단일출처] https://braindetox.kr/posts/school_violence_response_guide.html
32. 개인정보위 생성형 AI 안내서 (2025-08-06): https://v.daum.net/v/20250806130146966 ; https://byline.network/2025/08/6-277/ ; 김앤장/신김 뉴스레터 https://shinkim.com/kor/media/newsletter/2941
33. 109 통합: 정책브리핑 https://www.korea.kr/news/policyNewsView.do?newsId=148921684 ; https://www.etoday.co.kr/news/view/2312343
34. 마들랜·109 현황: 복지로 https://www.bokjiro.go.kr/ssis-tbu/cms/pc/news/promotion/1307179_1118.html ; 세계일보 https://www.segye.com/newsView/20250929517078
35. 청소년상담 1388: 복지로 https://www.bokjiro.go.kr/ssis-tbu/cms/pc/news/promotion/1307645_1118.html ; https://www.mogef.go.kr/cc/ccy/cc_ccy_f001.do
36. Federal Register, COPPA Rule (2025-04-22). https://www.federalregister.gov/documents/2025/04/22/2025-05904/childrens-online-privacy-protection-rule
