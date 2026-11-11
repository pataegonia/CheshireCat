"""S1.2 ConstantGuard: 상수 판정 (C1 §S1.2, master-plan §4.1)."""

import anyio

from guardrail.guard import ConstantGuard, Judgement
from guardrail.schema import GuardRequest, GuardResponse, Message, OutputVerdict
from guardrail.signals import Signals


def _judge(req: GuardRequest) -> Judgement:
    return anyio.run(ConstantGuard().judge, req)


def test_constant_without_candidate():
    j = _judge(GuardRequest((Message("user", "q"),), None, False))
    assert j.response == GuardResponse("normal", None)
    assert j.input_source == "default" and j.calls == 0 and j.errors == ()
    assert j.signals == Signals(False, False, False)
    assert (j.output_tier, j.nano_grounded, j.knn_major, j.lr_pred) == (None,) * 4
    assert dict(j.latency_ms) == {}


def test_constant_with_candidate_even_if_unreadable():
    for answer in ("", "답", None):
        j = _judge(GuardRequest((), answer, True))
        assert j.response.output == OutputVerdict(True, (), True)
