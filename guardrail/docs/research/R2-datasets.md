# R2. 가드레일 학습·평가용 데이터셋 카탈로그

- 작성일: 2026-10-29
- 범위: 한국어 RAG 학습 도우미(사용자=학생)의 가드레일 계층. (a) CPU 분류기(임베딩 + 로지스틱 회귀) 학습·보정, (b) 공격 차단율, 과잉거부, 멀티턴 상승, 간접 프롬프트 인젝션, PII 평가.
- 방법: HF 데이터셋 카드, GitHub README, arXiv 초록을 직접 열람했다(데이터 다운로드 없음). 직접 확인하지 못한 항목은 **[미확인]**으로 표시했다.
- 표기: 턴 = S(싱글)/M(멀티). 대조 = 정상(benign) 대조셋 포함 여부. 용도 = T(학습) / C(보정) / E(평가 전용).

## 요약

1. **한국어 원본 공격·유해 데이터는 아직 적지만, 2024~2026년에 쓸 만한 것이 생겼다.** KorSET(9,249개, CC BY-NC 4.0, 게이트), K-OverRefusal(485개, MIT, 과잉거부+대조), iknow-lab의 WildGuardMix/WildJailbreak 한국어 번역본, PolyGuardMix(17개 언어, ko 포함, CC-BY-4.0), MultiJail(ko 포함, MIT), KSAFE-MM(멀티모달)이 있다.
2. **한국어 혐오·편향 데이터는 충분하다.** K-HATERS(19.2만, CC-BY-4.0), KoSBi v2(6.8만, MIT 저장소), KOLD(4만, 라이선스 미기재), AI Hub 텍스트 윤리검증(45만 문장, 내국인 신청 필요)가 있다. 다만 이들은 "유해 질의"가 아니라 "유해 발화" 데이터다. 학생 대상 욕설·혐오 차단용으로만 쓴다.
3. **과잉거부(benign-but-scary) 한국어 데이터는 K-OverRefusal이 거의 유일하다.** 나머지는 XSTest, OR-Bench, PHTest, FalseReject를 번역해야 한다. K-OverRefusal은 XSTest·OR-Bench·PHTest·OKTest·HarmBench·StrongREJECT에서 파생됐으므로 이 원본들을 학습에 넣으면 평가가 오염된다.
4. **멀티턴과 간접 인젝션용 한국어 데이터는 찾지 못했다.** MHJ, SafeMTData, XGuard-Train, SafeDialBench, CoSafe, BIPIA, LLMail-Inject는 모두 영어(일부 중국어)다. 번역하거나 직접 생성해야 한다.
5. **아동·교육 특화 데이터:** MinorBench(299개, MIT, 수학 튜터 시스템 프롬프트 포함)가 가장 쓰기 좋다. Safe-Child-LLM(200개)도 공개돼 있다. EduZone, CAREBench, KIDBench는 2026년 논문이며 데이터 공개 여부는 **[미확인]**이다. 한국어 아동·교육 안전셋은 없다.
6. **한국어 PII:** KDPII(Zenodo, CC BY 4.0), BCCard openpii-finance(ko/en, CC-BY-4.0, SSN 라벨)가 있다. Faker `ko_KR`의 SSN 생성기는 주민번호 형식만 흉내 내고 체크섬은 없다.
7. **라이선스:** NC 계열(BeaverTails, PKU-SafeRLHF, ToxicChat, FalseReject, MHJ, KorSET, KorNAT, Do-Not-Answer)은 대회나 상용 제출물의 학습에 쓰기 전에 주최 측 규정을 확인해야 한다.

---

## 1. 탈옥·유해 프롬프트 (주로 영어)

| 데이터셋 | 링크 | 규모 | 언어 | 카테고리 | 턴 | 대조 | 라이선스 | 게이트 | 용도 | 주의 |
|---|---|---|---|---|---|---|---|---|---|---|
| JBB-Behaviors (JailbreakBench) | hf.co/datasets/JailbreakBench/JBB-Behaviors | 행동 200개(유해 100 + 정상 100), judge_comparison 300 | EN | OpenAI 정책 기반 10개 | S | O | MIT | X | E (holdout) | 평가 표준이라 학습 금지. 번역해서 사용 |
| HarmBench | github.com/centerforaisafety/HarmBench | 텍스트 400개(standard 200 / contextual 100 / copyright 100) + 멀티모달 110 | EN | 7개 의미 범주 | S | X | MIT(저장소) | X | E | 규모는 2차 출처 기준 **[원 논문 미열람]**. K-OverRefusal·MHJ의 원천이므로 학습 금지 |
| AdvBench | hf.co/datasets/walledai/AdvBench | 500개 행동 | EN | 일반 유해 | S | X | MIT | O(HF 동의) | C/E | 중복과 템플릿성이 심함. 수많은 논문에 쓰여 오염 가능성이 큼 |
| StrongREJECT | github.com/alexandrasouly/strongreject | 313개(small 60) | EN | 6개 | S | X | 코드·자체 데이터 MIT. 일부 문항은 원출처 라이선스(무라이선스 포함) | X | E | K-OverRefusal 원천이므로 학습 금지 |
| SORRY-Bench (2025-03) | hf.co/datasets/sorry-bench/sorry-bench-202503 | 기본 440 + 변형 8.8K = 9.2K | EN + 번역 5종(fr, mr, zh, ml, ta) | 44개 세부 | S | X | 자체 "sorry-bench" 계약(연구·상업 허용, **재배포 금지**, 삭제 요구 가능) | O | E | 한국어 없음. 20종 언어 변형을 생성 레시피로 참고 |
| WildJailbreak | hf.co/datasets/allenai/wildjailbreak | 학습 262K(vanilla 유해 50,050 / 정상 50,050 / adversarial 유해 82,728 / 정상 78,706), eval 2,210 | EN | 적대적 전술 조합 | S | **O**(adversarial benign) | ODC-BY + AI2 Responsible Use | O | T | 정상 대조가 강해 과잉거부 학습에 좋음 |
| WildGuardMix | hf.co/datasets/allenai/wildguardmix | Train 86,759 / Test 1,725 | EN | 프롬프트 유해, 응답 유해, 거부 | S | O | ODC-BY + AI2 RUG | O | T(train) / E(test) | 한국어 번역본은 5장 참고 |
| Aegis 2.0 | hf.co/datasets/nvidia/Aegis-AI-Content-Safety-Dataset-2.0 | 33,416개(train 30,007 / val 1,445 / test 1,964) | EN | 핵심 12개 + 세부 9개, Safe 및 Needs Caution | S(대화 일부) | O | CC-BY-4.0 | X | T | 카드에 val 크기가 1,245로도 적혀 있어 불일치 |
| BeaverTails | hf.co/datasets/PKU-Alignment/BeaverTails | 약 364K QA(330k / 30k 분할) | EN | 14개 | S | O | **CC BY-NC 4.0** | X(표시 없음) | T(비상용 한정) | NC |
| PKU-SafeRLHF | hf.co/datasets/PKU-Alignment/PKU-SafeRLHF | 82.1K(train 73.9K / test 8.21K) | EN | 선호 쌍 | S | O | **CC BY-NC 4.0** | X | 참고용 | 분류기 학습보다는 응답 측 평가용 |
| Do-Not-Answer | hf.co/datasets/LibrAI/do-not-answer | 939개 | EN | 5영역, 12유형, 61세부 | S | X | 데이터는 **CC BY-NC-SA 4.0**, 코드는 Apache-2.0(헤더 표기와 다름) | X | E | NC-SA |
| ToxicChat | hf.co/datasets/lmsys/toxic-chat | 버전당 10,165개(0124 권장). 유해 7.33%, 탈옥 2.01% | EN | toxicity, jailbreaking 라벨 | S | O(실사용자 정상 다수) | **CC BY-NC 4.0** | X | C/E | 실제 사용자 분포라 보정에 유용하나 NC |
| In-the-wild jailbreak (Shen et al.) | github.com/verazuo/jailbreak_llms, hf.co/datasets/TrustAIRLab/in-the-wild-jailbreak-prompts | 프롬프트 15,140(탈옥 1,405), 질문 390(13개 시나리오) | EN 위주 | DAN류 템플릿 | S | O(일반 프롬프트) | MIT | X | T(탈옥 템플릿) | 템플릿과 유해 질문을 조합해 한국어 변형을 만들 수 있음 |

## 2. 과잉거부 (benign-but-scary)

| 데이터셋 | 링크 | 규모 | 언어 | 구성 | 턴 | 대조 | 라이선스 | 게이트 | 용도 | 주의 |
|---|---|---|---|---|---|---|---|---|---|---|
| XSTest | hf.co/datasets/walledai/XSTest | 안전 250 + 유해 대조 200 | EN | 안전 프롬프트 10유형 | S | O | CC-BY-4.0(프롬프트) | O | E (holdout) | K-OverRefusal 원천이므로 학습 금지 |
| OR-Bench | hf.co/datasets/bench-llm/or-bench | 80k 80.4K / hard-1k 1.32K / toxic 655 | EN | 10개 범주 | S | O(toxic) | CC-BY-4.0 | X | 80k는 T/C, hard-1k는 E | K-OverRefusal이 일부를 차용하므로 학습 전 중복 제거 필수 |
| PHTest | hf.co/datasets/furonghuang-lab/PHTest | 3,269개 | EN | harmless와 controversial 라벨 | S | (controversial 분리) | MIT | X | C/E | K-OverRefusal 원천이므로 중복 제거 |
| FalseReject (Amazon) | hf.co/datasets/AmazonScience/FalseReject | train 14,624 / test 1,187 | EN | 44개 유형(카드상 46값) | S | X(전부 benign-looking) | **CC BY-NC 4.0** | X | T(비상용) / E | NC |
| K-OverRefusal | github.com/Marker-Inc-Korea/K-OverRefusal | 485개(safe 185 + unsafe 300) | **KO** | k_idioms 50, legality_framing, privacy, euphemism 등 | S | **O** | MIT | X | **E (holdout)** | 한국어 관용구("마약김밥")를 포함한 최적의 KO 과잉거부 평가셋 |

## 3. 멀티턴

| 데이터셋 | 링크 | 규모 | 언어 | 특징 | 대조 | 라이선스 | 게이트 | 용도 | 주의 |
|---|---|---|---|---|---|---|---|---|---|
| MHJ (Scale AI) | hf.co/datasets/ScaleAI/mhj | 대화 537개, 프롬프트 2,912개(평균 약 5.4턴) | EN | 사람 레드팀이 HarmBench 기반으로 작성 | X | **CC BY-NC 4.0** | O | E (holdout) | NC. HarmBench 행동과 겹침 |
| SafeMTData | hf.co/datasets/SafeMTData/SafeMTData | Attack_600(600) + SafeMTData_1K(1.68K) | EN | ActorAttack 계열 멀티턴 공격과 안전 응답 | 부분 | MIT | X | T(1K) / E(Attack_600) | 번역 후 사용 |
| XGuard-Train (X-Teaming) | hf.co/datasets/marslabucla/XGuard-Train | 대화 30,695개(평균 5.10턴) | EN | WildJailbreak vanilla 유해 행동 기반 | X | **카드에 미기재** | O | T(조건부) | 라이선스가 불명확하므로 사용 전 확인 |
| SafeDialBench | hf.co/datasets/HongyeCao/SafeDialBench | 4,053행(대화 4천 개 이상), 22개 시나리오, 7개 공격 전략 | ZH, EN | 6개 안전 차원 | X | apache-2.0(HF 태그). 논문 윤리 문구는 "학술 연구 한정" | X | E | 태그와 논문 문구가 상충함 |
| CoSafe | github.com/ErxinYu/CoSafe-Dataset | 1,400문항, 14개 범주 | EN | 공지시(coreference) 기반 멀티턴 공격 | X | **LICENSE 파일 없음**(논문은 학술 한정) | X | E(내부용) | 재배포 불가로 간주 |
| MultiBreak (2026) | arXiv 2605.01687 | 멀티턴 프롬프트 10,389개, 의도 2,665개 | [미확인] | 액티브 러닝으로 생성 | ? | 데이터 공개 링크 없음 | - | 참고 | 공개 여부 **[미확인]** |
| MT-JailBench (2026) | arXiv 2605.11002 | 프레임워크 | - | Crescendo, X-Teaming 재조합 | - | [미확인] | - | 생성 레시피 | Crescendo 단독 공개셋은 찾지 못함 |

## 4. 프롬프트 인젝션 (직접·간접)

| 데이터셋 | 링크 | 규모 | 언어 | 유형 | 대조 | 라이선스 | 게이트 | 용도 | 주의 |
|---|---|---|---|---|---|---|---|---|---|
| deepset/prompt-injections | hf.co/datasets/deepset/prompt-injections | 662개(train 546 / test 116) | EN 위주, 독일 주제 다수 | 직접 인젝션 | O(label 0) | Apache-2.0 | X | T | 작고 쉬움 |
| Lakera gandalf_ignore_instructions | hf.co/datasets/Lakera/gandalf_ignore_instructions | 1,000개(777 / 111 / 112) | EN | 직접(비밀번호 유출 시도) | X | MIT | X | T | Gandalf 컬렉션에 9개 항목이 있음 |
| Tensor Trust | github.com/HumanCompatibleAI/tensor-trust-data | hijacking·extraction 벤치마크 + 원시 덤프 v2 | EN | 직접(하이재킹, 추출) | 부분 | **[미확인]**(README에 없음) | X | T/E | 라이선스 확인 필요 |
| BIPIA (Microsoft) | github.com/microsoft/BIPIA | 5개 태스크(Web QA, Email, Table, Summ, Code) | EN | **간접** | O(정상 컨텍스트) | 저장소 LICENSE(종류 **[미확인]**). Web QA와 요약은 원출처 약관에 따름 | X | E | 저장소가 보관 처리됨 |
| LLMail-Inject (Microsoft, 2025) | hf.co/datasets/microsoft/llmail-inject-challenge | 462K건(Phase1 371K / Phase2 90.9K) | EN | **간접**(이메일 RAG 유출) | X(공격 위주) | MIT | X | T(간접 공격 표본) / E | RAG 문서 속 지시문 탐지에 가장 가까움 |
| InjecAgent | github.com/uiuc-kang-lab/InjecAgent | 1,054개 케이스 | EN | 간접(도구 응답), 직접 피해와 데이터 탈취 | X | MIT | X | E | 에이전트 도구 맥락 |
| AgentDojo | github.com/ethz-spylab/agentdojo | 환경 기반(태스크 수는 **[미확인]**) | EN | 간접(에이전트) | O(유틸리티) | MIT | X | E(참고) | 정적 데이터셋이 아니라 실행 환경 |
| Open-Prompt-Injection | github.com/liu00222/Open-Prompt-Injection | 툴킷(SST-2, SMS spam 등에 주입) | EN | 직접·간접 합성 | O | MIT | X | 생성 도구 | DataSentinel 탐지기 포함 |
| PINT (Lakera) | github.com/lakeraai/pint-benchmark | 4,314개(EN 3,016 / 비영어 1,298, **한국어 포함**) | 다국어 | 인젝션 5.2%, 탈옥 0.9%, 헷갈리는 정상 20.9% 등 | **O** | 저장소 MIT. 데이터는 독점 혼합으로 공개 여부 **[미확인]** | 요청제 추정 | E | 외부 평가 받을 때 비교 기준 |

## 5. 한국어 안전·탈옥·편향·혐오

| 데이터셋 | 링크 | 규모 | 유형 | 턴 | 대조 | 라이선스 | 게이트 | 용도 | 주의 |
|---|---|---|---|---|---|---|---|---|---|
| **KorSET** (CAGE, ICLR 2026, SelectStar/Datumo) | hf.co/datasets/datumo/KorSET | 9,249개. 5개 도메인, 12개 범주(차별 1,175, 편향·혐오 1,352, 허위 1,404 등) | 한국 문화·법에 맞춘 레드팀 프롬프트 | S로 추정 | X | **CC BY-NC 4.0** | **O(수동 심사, 학술·안전 연구 한정)** | E (holdout) 또는 비상용 T | 원어민 맥락의 유해 질의로는 최대 규모 |
| **K-OverRefusal** | github.com/Marker-Inc-Korea/K-OverRefusal | 485개 | 과잉거부와 유해 대조 | S | O | MIT | X | **E** | 2장 참고 |
| iknow-lab/wildguardmix-train-ko | hf.co/datasets/iknow-lab/wildguardmix-train-ko | 86,414행 | WildGuardMix 기계번역(llama3-instrucTrans-enko-8b) | S | O | **카드에 미기재**(원본은 ODC-BY + AI2 RUG) | [미확인] | **T** | 기계번역 품질. 원본 약관 승계 여부 확인 필요 |
| iknow-lab/wildguardmix-test-ko | hf.co/datasets/iknow-lab/wildguardmix-test-ko | 원본 기준 1,725 | 번역 | S | O | 미기재 | [미확인] | E | |
| iknow-lab/wildjailbreak-eval-ko | hf.co/datasets/iknow-lab/wildjailbreak-eval-ko | 2,210행(`prompt_ko` 포함) | WildJailbreak eval 번역 | S | O(adversarial_benign 210) | 미기재 | [미확인] | E | |
| PolyGuardMix (ko 부분집합) | hf.co/datasets/ToxicityPrompts/PolyGuardMix | 전체 1.91M행, 17개 언어 | 프롬프트 유해, 거부, 응답 유해 + 범주 | S | O | CC-BY-4.0 | X | **T** | 번역 + in-the-wild 혼합. ko 행 수는 **[미확인]**. 평가용 PolyGuardPrompts(29K)는 별도 |
| MultiJail | hf.co/datasets/DAMO-NLP-SG/MultiJail | 315개 × 10개 언어(ko 컬럼) | 사람이 번역한 유해 질의 | S | X | MIT | X | E | 소규모지만 번역 품질이 좋음 |
| KSAFE-MM (KT, 고려대, 2026) | arXiv 2605.28013 / HF | 질의-이미지 쌍 14,135개, 11개 범주, 탈옥 10종(KSAFE-MM-C) | 멀티모달 | S | X | CC BY-NC 4.0(검색 요약 기준, **카드 미열람**) | [미확인] | 참고 | 텍스트 질의만 떼어 쓸 수 있는지 확인 필요 |
| XL-SafetyBench (KT, 2026-06) | 보도자료만 확인 | 10개국 5,500 프롬프트(ko 포함) | 문화 민감 안전 | S | ? | **[미확인]** | ? | 후보 | 공개 위치와 라이선스 **[미확인]** |
| KoSBi v2 (NAVER) | github.com/naver-ai/korean-safety-benchmarks | 약 68K(safe 34.2K / unsafe 33.8K), train/valid/test | 사회적 편향 문장 | S | O | 저장소 MIT(데이터 별도 표기 없음) | X | **T/C** | 질의가 아니라 발화임 |
| SQuARe (NAVER) | 위 저장소 | 민감 질문과 수용 가능한 응답(규모 미기재) | 민감 질문 | S | O(수용 응답) | MIT(저장소) | X | T/C | 한국 사회 특이 주제 |
| K-HATERS | hf.co/datasets/humane-lab/K-HATERS | 192,158(172K / 10K / 10K) | 혐오 4단계 + 대상 | S | O(normal) | CC-BY-4.0 | X | **T** | 뉴스 댓글 도메인 |
| KOLD | github.com/boychaboy/KOLD | 40,429 댓글 | 공격성, 대상, 스팬 | S | O | **미기재**("공격적 언어 생성 학습 금지" 문구) | X | T(내부) | 라이선스가 없어 재배포 위험 |
| KoBBQ | hf.co/datasets/naver-ai/kobbq | 템플릿 268개, 76K 샘플(뷰어 81K) | 편향 QA, 12개 범주 | S | - | MIT | X | 참고 | 분류기 학습용은 아님 |
| KorNAT | hf.co/datasets/jiyounglee0523/KorNAT | 14,024행(사회가치 4K + 상식 6K 등) | 정렬 MCQ | S | - | **CC BY-NC 2.0** | X | 참고 | |
| AI Hub 텍스트 윤리검증 데이터 | aihub.or.kr dataSetSn=558 | 453,340문장 / 대화 132,807세트 | 차별, 혐오, 비난, 폭력, 범죄, 성, 욕설 + 강도 0~3 | M(대화 세트) | O(IMMORAL_NONE) | AI Hub 약관: **내국인만 신청**, 학술 또는 챗봇 악성 콘텐츠 방지 목적 | O(승인) | T(내부) | 재배포 금지. 팀원 전원이 내국인이어야 함 |
| Kanana Safeguard 평가셋 | kakaocorp 모델 카드 | 비공개("내부 평가셋") | - | - | - | - | - | 없음 | 공개 데이터 없음 |
| NIA, TTA, AI안전연구소 LLM 안전성 평가셋 | - | 공개된 것 찾지 못함 | - | - | - | - | - | - | KISA 레드팀 사업은 서비스 지원이지 데이터 공개가 아님 |
| AdvBench, HarmBench 공식 한국어 번역 | - | 찾지 못함 | - | - | - | - | - | - | K-OverRefusal의 unsafe 300개가 사실상 HarmBench/StrongREJECT 한국어 번역 |

## 6. 아동·학생·교육 특화

| 데이터셋 | 링크 | 규모 | 언어 | 구성 | 라이선스 | 용도 | 주의 |
|---|---|---|---|---|---|---|---|
| **MinorBench** (GovTech SG, 2025) | hf.co/datasets/govtech/MinorBench | 299개 | EN | 성, 욕설, 혐오, 위험, 자해, 약물 + **시스템 프롬프트 4종(12세 대상 수학 튜터 등)** | MIT | **E**(번역) | 시나리오가 우리 서비스와 가장 가까움 |
| Safe-Child-LLM (2025) | arXiv 2506.13510, GitHub(The-Responsible-AI-Initiative) | 200개(7~12세 100 / 13~17세 100) | EN | 기존 탈옥셋 적응 + 거부 0~5 척도 | **[미확인]** | E | JBB, AdvBench, HarmBench와 겹침 |
| EduZone (2026) | arXiv 2608.02024 | 미기재 | ? | K-12 대상, 6개 위험·28개 세부, 싱글·정적 멀티·동적 멀티턴 | [미확인] | 참고(분류 체계) | 데이터 공개 **[미확인]** |
| CAREBench (2026) | arXiv 2606.29685 | 500개, 12개 범주(그루밍, 정서적 의존 등) | ? | 아동 위험 | [미확인] | 참고 | 공개 링크 없음 |
| KIDBench (2026) | arXiv 2605.25510 | [미확인] | ? | 7~11세, 발달심리 기반 루브릭 | [미확인] | 참고 | |

## 7. 한국어 PII

| 리소스 | 링크 | 규모 | 내용 | 라이선스 | 용도 | 주의 |
|---|---|---|---|---|---|---|
| KDPII (IEEE Access 2024) | zenodo.org/records/16759166 | JSON 33.3MB(건수 미기재) | 한국어 대화 속 PII. 범용 PII와 한국어 특화 PII 구분 | CC BY 4.0 | T/E | 합성 여부 **[미확인]** |
| BCCard/pii-masking-openpii-finance | hf.co/datasets/BCCard/pii-masking-openpii-finance | 72.4K(train 57.9K / val 14.5K) | ko/en. PERSON, PHONE, SSN, CARD_NUMBER, PASSPORT 등 | CC-BY-4.0 | T/E | 일부 합성·변형 행 포함. 주민번호가 SSN 라벨로 들어간 것으로 보임 **[추정]** |
| Faker `ko_KR` | github.com/joke2k/faker (providers/ssn/ko_KR) | 생성기 | 주민번호 형식 `##0#0#-1######` 등 6종, 주소·우편번호·번호판 | MIT | 합성 생성 | **체크섬 없음.** 검증 로직이 있는 탐지기를 시험하려면 체크섬 계산을 직접 추가해야 함 |
| 공개 KO PII 탐지 모델 | hf.co/id4thomas/ko-pii-detector-ax-crf 등 | - | RRN 라벨 F1 0.988(자체 보고) | 모델별 | 비교 기준 | 데이터 아님 |

## 8. 한국어 변형 생성 도구

| 도구 | 한국어·번역 지원 | 근거 |
|---|---|---|
| garak | O. 프로브·탐지기 트리거 번역 지원(DeepL, Riva, Google, 로컬 m2m100). 내부 처리는 아직 `en` 중심 | reference.garak.ai/en/latest/translation.html |
| promptfoo redteam | O. `multilingual` 전략. 기본값은 bn/sw/jv이며 ISO 코드로 `ko` 지정 가능. 테스트 수 = 언어 수 배 | promptfoo.dev/docs/red-team/strategies/multilingual |
| PyRIT | 부분. 변환기(Base64, ROT13, 호모글리프 등)가 있음. LLM 기반 번역 변환기(TranslationConverter)도 있다고 알고 있으나 **[문서 미확인]** | 2차 출처만 확인 |
| Open-Prompt-Injection | 언어 무관 주입 합성 툴킷 | 4장 참고 |
| CAGE (KorSET 생성 프레임워크) | O. 한국어 문화 적응 생성("Semantic Mold") | arXiv 2602.20170 |

---

## 추천 조합

원칙은 세 가지다. (1) 평가셋과 그 원천 데이터(HarmBench, StrongREJECT, XSTest, OR-Bench, PHTest, JBB, AdvBench)는 학습에 넣지 않는다. (2) 학습과 보정에 넣는 모든 데이터는 평가셋과 문장 임베딩 기준(cos > 0.9)으로 중복을 제거한다. (3) 번역은 학습셋과 평가셋을 따로 번역한다. 같은 원문이 양쪽에 들어가지 않도록 하기 위해서다.

**학습 (라이선스가 허용적인 것 우선)**
- 유해·탈옥 양성: PolyGuardMix ko 부분집합(CC-BY-4.0), iknow-lab wildguardmix-train-ko(라이선스 확인 후), WildJailbreak train 표본 번역(ODC-BY), Aegis 2.0 train 번역(CC-BY-4.0), Shen 탈옥 템플릿 × 한국어 유해 질의 조합(MIT)
- 정상 대조(과잉거부 억제): WildJailbreak adversarial/vanilla benign 번역, OR-Bench-80k 번역(K-OverRefusal과 중복 제거), **RAG 교과 질문 실데이터**(주최 측 정상 질의, 교과서 문장)
- 한국어 혐오·욕설: K-HATERS train(CC-BY-4.0), KoSBi v2 train(MIT)
- 인젝션: deepset(Apache), Lakera gandalf train(MIT), LLMail-Inject 표본(MIT. 간접 인젝션이며 RAG 문서에 끼워 넣어 변형)
- 멀티턴: SafeMTData_1K 번역(MIT). 분류기 입력은 "최근 N턴 연결"이나 "누적 위험 점수"로 설계
- PII: BCCard openpii-finance train과 Faker ko_KR 합성(정규식과 체크섬 탐지기 검증용)
- 주최 측 한국어 공격셋 train 분할(아래 holdout 분할과 분리)

**보정 (임계값과 Platt/isotonic 보정, 학습과 분리)**
- 주최 측 공격셋 dev 분할, KoSBi valid, K-HATERS validation, gandalf validation, PHTest 일부 번역(K-OverRefusal과 중복 제거). 실사용 분포를 대신할 데이터가 필요하면 ToxicChat 번역(NC이므로 내부 보정에만 사용)

**개발 평가 (반복 측정용)**
- 차단율: wildguardmix-test-ko, wildjailbreak-eval-ko, MultiJail ko
- 과잉거부: OR-Bench-hard-1k 번역
- 멀티턴: SafeMTData Attack_600 번역, SafeDialBench 영어 부분 번역 표본
- 간접 인젝션: BIPIA(Email, Table, Code) 번역 + 우리 RAG 문서에 지시문을 삽입한 합성셋
- 아동: MinorBench 번역(시스템 프롬프트 4종 그대로 사용)
- PII: KDPII test

**holdout 성격 평가 (마지막에 한 번만, 튜닝 금지)**
- **K-OverRefusal**(KO 과잉거부 + 대조), **KorSET**(승인을 받을 경우. KO 원어민 맥락), JBB-Behaviors 100/100 번역, XSTest 번역, StrongREJECT 번역, MHJ 번역(NC, 게이트), 주최 측 공격셋 test 분할

## 라이선스 위험 메모

- **NC(비상용) 계열:** BeaverTails, PKU-SafeRLHF, ToxicChat, FalseReject, MHJ, KorSET, KSAFE-MM은 CC BY-NC 4.0, Do-Not-Answer는 CC BY-NC-SA 4.0, KorNAT은 CC BY-NC 2.0이다. 대회 제출물이 상업적 이용에 해당하는지, 학습 데이터에 NC를 허용하는지 주최 측 규정을 확인해야 한다. 규정을 확인하기 전에는 평가에만 쓰고 가중치에는 넣지 않는다. (참고: arXiv 2608.21570은 라이선스 경계로 코퍼스를 나눠 "배포용"과 "연구용" CPU 분류기를 따로 학습하는 방식을 제안한다.)
- **약관 제한:** WildJailbreak와 WildGuardMix에는 AI2 Responsible Use가 붙는다(게이트). SORRY-Bench는 **제3자 재배포 금지**이고 라이선서가 삭제를 요구할 수 있다. AI Hub 윤리검증 데이터는 **내국인만** 신청할 수 있고 목적이 제한되며 재배포가 금지된다.
- **라이선스 미기재:** iknow-lab 한국어 번역본 3종, XGuard-Train, CoSafe, KOLD, Tensor Trust(미확인), PINT 데이터가 해당한다. 기본값은 "재배포 불가, 내부 실험만"으로 둔다. iknow-lab 번역본은 Llama 3 기반 번역 모델의 출력물이므로 원본 ODC-BY와 Llama 3 라이선스가 함께 적용되는지 **[미확인]**이다.
- **약관 상충:** SafeDialBench는 HF 태그가 apache-2.0인데 논문은 "학술 연구 한정"이라고 쓴다. 보수적으로 학술 한정으로 해석한다.
- **오염:** HarmBench, AdvBench, JBB, XSTest는 공개 가드 모델들의 학습셋에 섞였을 가능성이 크다. 사전학습 임베딩 모델을 쓰더라도 상대 비교만 하고, 절대 수치로 주장하지 않는다.
- **유해 원문 취급:** 공격 텍스트는 저장소에 평문으로 커밋하지 말고 게이트 데이터의 재배포 조건을 지킨다. 대회 산출물에는 ID 참조만 남긴다.

## 출처

- JBB-Behaviors: https://huggingface.co/datasets/JailbreakBench/JBB-Behaviors
- HarmBench: https://github.com/centerforaisafety/HarmBench, arXiv 2402.04249. 행동 수는 2차 출처 https://huggingface.co/datasets/giskardai/harmbench-scenarios 기준
- AdvBench: https://huggingface.co/datasets/walledai/AdvBench
- StrongREJECT: https://github.com/alexandrasouly/strongreject
- SORRY-Bench: https://huggingface.co/datasets/sorry-bench/sorry-bench-202503
- WildJailbreak: https://huggingface.co/datasets/allenai/wildjailbreak
- WildGuardMix: https://huggingface.co/datasets/allenai/wildguardmix
- Aegis 2.0: https://huggingface.co/datasets/nvidia/Aegis-AI-Content-Safety-Dataset-2.0
- BeaverTails: https://huggingface.co/datasets/PKU-Alignment/BeaverTails
- PKU-SafeRLHF: https://huggingface.co/datasets/PKU-Alignment/PKU-SafeRLHF
- Do-Not-Answer: https://huggingface.co/datasets/LibrAI/do-not-answer
- ToxicChat: https://huggingface.co/datasets/lmsys/toxic-chat
- In-the-wild jailbreak: https://github.com/verazuo/jailbreak_llms
- XSTest: https://huggingface.co/datasets/walledai/XSTest
- OR-Bench: https://huggingface.co/datasets/bench-llm/or-bench
- PHTest: https://huggingface.co/datasets/furonghuang-lab/PHTest
- FalseReject: https://huggingface.co/datasets/AmazonScience/FalseReject
- K-OverRefusal: https://github.com/Marker-Inc-Korea/K-OverRefusal, K-SafeGuard 모델: https://huggingface.co/MarkrAI/ksafeguard-8b
- MHJ: https://huggingface.co/datasets/ScaleAI/mhj
- SafeMTData: https://huggingface.co/datasets/SafeMTData/SafeMTData
- XGuard-Train: https://huggingface.co/datasets/marslabucla/XGuard-Train, arXiv 2504.13203
- SafeDialBench: https://safedialbench.github.io/, https://huggingface.co/datasets/HongyeCao/SafeDialBench, arXiv 2502.11090
- CoSafe: https://github.com/ErxinYu/CoSafe-Dataset, arXiv 2406.17626
- MultiBreak: https://arxiv.org/abs/2605.01687 / MT-JailBench: https://arxiv.org/pdf/2605.11002
- deepset: https://huggingface.co/datasets/deepset/prompt-injections
- Lakera Gandalf: https://huggingface.co/datasets/Lakera/gandalf_ignore_instructions
- Tensor Trust: https://github.com/HumanCompatibleAI/tensor-trust-data
- BIPIA: https://github.com/microsoft/BIPIA
- LLMail-Inject: https://huggingface.co/datasets/microsoft/llmail-inject-challenge, arXiv 2506.09956
- InjecAgent: https://github.com/uiuc-kang-lab/InjecAgent
- AgentDojo: https://github.com/ethz-spylab/agentdojo
- Open-Prompt-Injection: https://github.com/liu00222/Open-Prompt-Injection
- PINT: https://github.com/lakeraai/pint-benchmark
- KorSET / CAGE: https://huggingface.co/datasets/datumo/KorSET, https://github.com/selectstar-ai/CAGE-paper, https://arxiv.org/abs/2602.20170
- iknow-lab 한국어 번역본: https://huggingface.co/iknow-lab, https://huggingface.co/datasets/iknow-lab/wildguardmix-train-ko, https://huggingface.co/datasets/iknow-lab/wildjailbreak-eval-ko
- PolyGuardMix: https://huggingface.co/datasets/ToxicityPrompts/PolyGuardMix, arXiv 2504.04377
- MultiJail: https://huggingface.co/datasets/DAMO-NLP-SG/MultiJail
- KSAFE-MM: https://arxiv.org/pdf/2605.28013, https://oecd.ai/en/catalogue/tools/ksafe-mm-%C2%B7-datasets-at-hugging-face
- XL-SafetyBench(KT): https://en.sedaily.com/news/2026/06/04/kt-develops-multilingual-benchmark-to-test-ais-cultural
- KoSBi / SQuARe: https://github.com/naver-ai/korean-safety-benchmarks
- K-HATERS: https://huggingface.co/datasets/humane-lab/K-HATERS
- KOLD: https://github.com/boychaboy/KOLD, arXiv 2205.11315
- KoBBQ: https://huggingface.co/datasets/naver-ai/kobbq
- KorNAT: https://huggingface.co/datasets/jiyounglee0523/KorNAT
- AI Hub 텍스트 윤리검증: https://www.aihub.or.kr/aihubdata/data/view.do?dataSetSn=558
- Kanana Safeguard: https://www.kakaocorp.com/page/detail/11655
- MinorBench: https://huggingface.co/datasets/govtech/MinorBench, arXiv 2503.10242
- Safe-Child-LLM: https://arxiv.org/abs/2506.13510 / EduZone: https://arxiv.org/abs/2608.02024 / CAREBench: https://arxiv.org/abs/2606.29685 / KIDBench: https://arxiv.org/html/2605.25510
- KDPII: https://zenodo.org/records/16759166
- BCCard openpii: https://huggingface.co/datasets/BCCard/pii-masking-openpii-finance
- Faker ko_KR SSN: https://raw.githubusercontent.com/joke2k/faker/master/faker/providers/ssn/ko_KR/__init__.py
- garak 번역: https://reference.garak.ai/en/latest/translation.html
- promptfoo multilingual: https://promptfoo.dev/docs/red-team/strategies/multilingual
- 라이선스 경계 분리 CPU 분류기: https://arxiv.org/abs/2608.21570
