"""런박스 진입점(통합 PRD IN-05): 가드 골격 하나에 `/guard`와 튜터 `/tutor`를 같이 띄운다.

인증 · 메타 로그 · 예외→200 · lifespan은 가드 `create_app`이 맡는다. 가드 판정기 선택
(guard_factory)은 가드 기본값을 쓴다.
"""

from guardrail.server import create_app

import tutor

app = create_app(
    tutor_router=tutor.router,
    defaults={"/tutor": tutor.TUTOR_DEFAULT},
    startups=[tutor.startup],
)
