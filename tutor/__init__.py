"""대회 POST /tutor 튜터(TU-01).

통합 app.py(IN-05)가 가드 create_app에 주입한다:
    create_app(make_guard, tutor_router=tutor.router,
               defaults={"/tutor": tutor.TUTOR_DEFAULT}, startups=[tutor.startup])

tutor.router와 tutor.startup은 같은 이름의 모듈이 아니라 그 안의 APIRouter와 함수다(통합이 쓰는
이름). 모듈 자체가 필요하면 importlib.import_module("tutor.router")로 가져온다.
"""

from tutor.pipeline import handle
from tutor.router import router
from tutor.runtime.response import TUTOR_DEFAULT
from tutor.startup import startup

__all__ = ["TUTOR_DEFAULT", "handle", "router", "startup"]
