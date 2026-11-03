"""테스트 전용 가짜 객체. 패키지 코드는 이 파일을 import하지 않는다.

S0.2: FakeTutor(정해진 청크·답을 돌려주는 TutorAdapter).
"""

from guardrail.types import Chunk, Turn


class FakeTutor:
    """정해진 청크와 답을 돌려주고, 받은 인자를 기록한다."""

    def __init__(
        self,
        chunks: list[Chunk] | None = None,
        answer: str = "물은 0°C에서 언다. [S1]",
        system_prompt: str | None = "기본 지시",
    ) -> None:
        self.chunks = (
            chunks if chunks is not None else [Chunk("S1", "교재 1장", "물은 0°C에서 언다.")]
        )
        self.answer = answer
        self.system_prompt = system_prompt
        self.retrieve_calls: list[tuple[str, list[Turn]]] = []
        self.generate_calls: list[dict] = []

    async def retrieve(self, question: str, history: list[Turn]) -> list[Chunk]:
        self.retrieve_calls.append((question, history))
        return list(self.chunks)

    async def generate(
        self,
        question: str,
        history: list[Turn],
        chunks: list[Chunk],
        *,
        system_extra: str = "",
        instruction: str | None = None,
    ) -> str:
        self.generate_calls.append(
            {
                "question": question,
                "history": history,
                "chunks": chunks,
                "system_extra": system_extra,
                "instruction": instruction,
            }
        )
        return self.answer
