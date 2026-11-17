"""harness.complied_probe: 유해-비하 고정 문장 응함 판정 탐침(가짜 판정기, 운영진 API 0회)."""

from harness import complied_probe
from tutor.answer.templates import TEXT, Fixed


def test_counts_complied_per_arm_with_a_fake_judge(monkeypatch, capsys):
    seen = []

    def fake(kind, payload):
        seen.append((kind, payload["answer"]))
        return {"complied": payload["answer"] == TEXT[Fixed.REDIRECT_HARMFUL]}

    monkeypatch.setattr(complied_probe, "judge_from_env", lambda: fake)
    assert complied_probe.main(["--repeat", "2"]) == 0
    out = capsys.readouterr().out
    rows = sum(1 for r in complied_probe.cv_rows() if r["labels"]["category"] == "harmful_content"
               and r["labels"]["action"] == "redirect")  # fmt: skip
    assert rows > 0
    assert {k for k, _ in seen} == {"complied"}
    assert len(seen) == rows * 3 * 2
    assert f"- template: 응함 {rows * 2}/{rows * 2} (판정 실패 0)" in out
    assert f"- candidate: 응함 0/{rows * 2}" in out


def test_stops_after_repeated_judge_failures(monkeypatch, capsys):
    def broken(kind, payload):
        raise RuntimeError("판정 실패")

    monkeypatch.setattr(complied_probe, "judge_from_env", lambda: broken)
    assert complied_probe.main(["--repeat", "1"]) == 1
    assert "멈췄다" in capsys.readouterr().out
