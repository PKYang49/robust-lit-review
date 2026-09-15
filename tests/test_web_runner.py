"""Tests for the tool-free Claude transport used by the web relay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from litreview.pipeline.brief_flow import Dispatch
from litreview.web.runner import ClaudeRunner, WorkError


def _grade_answer() -> dict:
    return {
        "body_of_evidence": ["@Trial"],
        "starting_level": "low",
        "domains": [
            {"name": name, "rating": "not_serious", "downgrade": 0, "justification": "ok"}
            for name in ("risk_of_bias", "inconsistency", "indirectness", "imprecision", "publication_bias")
        ],
        "upgrades": [],
        "effect_direction": "harmful",
        "final_certainty": "low",
        "n_studies": 1,
        "n_rct": 0,
        "summary": "One observational study.",
    }


def _task(tmp_path: Path, kind: str = "GRADE") -> Dispatch:
    task_path = tmp_path / "tasks" / "grade_pico_01.md"
    task_path.parent.mkdir()
    task_path.write_text("Return JSON", encoding="utf-8")
    (tmp_path / "pico_01.body.json").write_text("{}", encoding="utf-8")
    return Dispatch(kind, "pico_01", task_path)


def test_grade_output_without_pico_id_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runner = ClaudeRunner()
    monkeypatch.setattr(runner, "complete", lambda _prompt, _model: _grade_answer())

    runner.dispatch(tmp_path, _task(tmp_path))

    assert json.loads((tmp_path / "grade_pico_01.json").read_text(encoding="utf-8"))["n_studies"] == 1


def test_explicit_wrong_pico_id_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runner = ClaudeRunner()
    answer = _grade_answer()
    answer["pico_id"] = "pico_02"
    monkeypatch.setattr(runner, "complete", lambda _prompt, _model: answer)

    with pytest.raises(WorkError, match="PICO 識別碼"):
        runner.dispatch(tmp_path, _task(tmp_path))


def test_screen_output_missing_study_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    task_path = tmp_path / "tasks" / "screen_pico_01_01.md"
    task_path.parent.mkdir()
    task_path.write_text(
        "PICO ID: pico_01\n\ncitation_key: StudyA\ntitle: A\n\n"
        "citation_key: StudyB\ntitle: B\n",
        encoding="utf-8",
    )
    task = Dispatch("Screen", "pico_01", task_path, model="haiku")
    answers = iter([
        {"pico_id": "pico_01", "decisions": [
            {"citation_key": "StudyA", "include": True},
        ]},
        {"pico_id": "pico_01", "decisions": [
            {"citation_key": "StudyA", "include": True},
            {"citation_key": "StudyB", "include": False},
        ]},
    ])
    runner = ClaudeRunner()
    prompts: list[str] = []

    def complete(prompt: str, _model: str) -> dict:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(runner, "complete", complete)
    runner.dispatch(tmp_path, task)

    result = json.loads((tmp_path / "screen_pico_01_01.json").read_text(encoding="utf-8"))
    assert {item["citation_key"] for item in result["decisions"]} == {"StudyA", "StudyB"}
    assert len(prompts) == 2
    assert "Required citation_keys" in prompts[1]
