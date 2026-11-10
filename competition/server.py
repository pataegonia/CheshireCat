import os
import secrets
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from openai import AsyncOpenAI
from pydantic import BaseModel

TOKEN = os.environ["TEAM_TOKEN"]
MODEL = os.environ["OPENAI_MODEL"]
SYSTEM = "당신은 온라인 학습 플랫폼의 AI 튜터입니다. 한국어로 답합니다."
FALLBACK = "지금은 답변을 만들 수 없어요. 잠시 후 다시 질문해 주세요."

llm = AsyncOpenAI(timeout=25, max_retries=0)


def check_token(authorization: str = Header("")):
    if not secrets.compare_digest(authorization.encode(), f"Bearer {TOKEN}".encode()):
        raise HTTPException(status_code=401, detail="invalid token")


app = FastAPI(redirect_slashes=False, dependencies=[Depends(check_token)])


class TutorRequest(BaseModel):
    messages: List[Dict[str, str]]


class GuardRequest(BaseModel):
    messages: List[Dict[str, str]]
    candidate_answer: Optional[str] = None


@app.post("/tutor")
async def tutor(req: TutorRequest):
    # TODO: 입력을 거르고, 지식 베이스에서 근거 섹션을 찾아 프롬프트에 넣고, action과 citations를 정하세요.
    try:
        r = await llm.chat.completions.create(
            model=MODEL, messages=[{"role": "system", "content": SYSTEM}, *req.messages],
            max_completion_tokens=2000, extra_body={"reasoning_effort": "low"})
        answer = (r.choices[0].message.content or "").strip()[:4000] or FALLBACK
    except Exception as e:
        print("LLM 호출 실패:", type(e).__name__)
        answer = FALLBACK
    return {"answer": answer, "action": "answer", "citations": []}


@app.post("/guard")
async def guard(req: GuardRequest):
    # TODO: 지금은 고정값입니다. 입력과 후보 답변을 실제로 판정하세요.
    result = {"input": {"decision": "allow", "category": "normal"}}
    if req.candidate_answer is not None:
        result["output"] = {"grounded": True, "unsupported": [], "safe": True, "category": "normal"}
    return result
