"""LLM 판정 출력 스키마 (master-plan §4.3, GR-04·GR-07).

필드 순서가 곧 생성 순서다: 근거(evidence·reason)를 먼저 쓰고 판정을 낸다. 순서를 바꾸지 않는다.
S2.6·S2.7은 이 스키마에 맞춰 프롬프트만 쓴다.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from guardrail.schema import Category


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)  # "true"·1 같은 느슨한 변환 거부


class InputOut(_Out):
    evidence: str  # 한 문장
    category: Category


# S3.2 선별용 변형(R14 B3): nano는 요청 유형을 고르고 범주는 코드가 대응시킨다.
# v1 실측에서 nano가 요청은 맞게 읽고 범주 이름을 틀리게 고른 데서 나왔다
# (results/2026-11-13-input-screening-1.md)
Act = Literal[
    "no_meaningful_content",
    "reveal_tutor_instructions",
    "override_tutor_role",
    "find_or_spread_others_private_info",
    "write_scam_or_impersonation",
    "insult_or_hate",
    "make_work_to_submit",
    "make_offcourse_output",
    "lasting_emotional_distress",
    "shares_own_personal_info",
    "only_final_answer",
    "asks_value_not_in_material",
    "studies_risky_topic",
    "learning_question",
]
ACT_TO_CATEGORY: dict[str, Category] = {
    "no_meaningful_content": "low_signal_input",
    "reveal_tutor_instructions": "jailbreak_injection",
    "override_tutor_role": "jailbreak_injection",
    "find_or_spread_others_private_info": "harmful_content",
    "write_scam_or_impersonation": "harmful_content",
    "insult_or_hate": "harmful_content",
    "make_work_to_submit": "academic_dishonesty",
    "make_offcourse_output": "off_topic",
    "lasting_emotional_distress": "emotional_distress",
    "shares_own_personal_info": "pii_disclosure",
    "only_final_answer": "answer_seeking",
    "asks_value_not_in_material": "out_of_material",
    "studies_risky_topic": "normal",
    "learning_question": "normal",
}


class InputActOut(_Out):
    request: str  # 학습자가 튜터에게 해 달라는 것, 한 문장
    act: Act


class OutputOut(_Out):
    reason: str  # 한 문장
    grounded: bool


class OutputBaselineOut(_Out):
    """output.mode: baseline 전용. nano가 grounded·safe를 바로 낸다."""

    reason: str
    grounded: bool
    safe: bool


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """OpenAI json_schema strict용: 모든 object에 additionalProperties=false, required=전 필드."""

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(v) for v in node]
        if not isinstance(node, dict):
            return node
        out = {k: walk(v) for k, v in node.items()}
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        return out

    return walk(model.model_json_schema())
