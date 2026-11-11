"""LLM 판정 출력 스키마 (master-plan §4.3, GR-04·GR-07).

필드 순서가 곧 생성 순서다: 근거(evidence·reason)를 먼저 쓰고 판정을 낸다. 순서를 바꾸지 않는다.
S2.6·S2.7은 이 스키마에 맞춰 프롬프트만 쓴다.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from guardrail.schema import Category


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)  # "true"·1 같은 느슨한 변환 거부


class InputOut(_Out):
    evidence: str  # 한 문장
    category: Category


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
