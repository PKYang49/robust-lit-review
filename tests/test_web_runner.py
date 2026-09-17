"""Tests for the tool-free Claude transport used by the web relay."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

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


def _screen_task(tmp_path: Path, keys: list[str]) -> Dispatch:
    """A screening task shaped like write_screen_tasks output."""
    task_path = tmp_path / "tasks" / "screen_pico_01_01.md"
    task_path.parent.mkdir(exist_ok=True)
    blocks = "\n\n".join(
        f"citation_key: {k}\ntitle: T{k}\nyear: 2020\npub_type: RCT\njournal: J\nabstract: A{k}"
        for k in keys
    )
    task_path.write_text(
        f"Screen this batch for the body of evidence.\n\nPICO ID: pico_01\n"
        f"Population: adults\n\nStudies:\n{blocks}\n\n"
        'Return ONLY JSON with this schema and write it to /tmp/out.json:\n'
        '{"pico_id":"pico_01","batch":1,"decisions":[]}\n',
        encoding="utf-8",
    )
    return Dispatch("Screen", "pico_01", task_path, model="haiku")


def _run(runner: ClaudeRunner, tmp_path: Path, task: Dispatch,
         answers: list[dict], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    prompts: list[str] = []
    it = iter(answers)

    def complete(prompt: str, _model: str) -> dict:
        prompts.append(prompt)
        return next(it)

    monkeypatch.setattr(runner, "complete", complete)
    runner.dispatch(tmp_path, task)
    return prompts


def test_screen_retry_asks_only_for_the_missing_study(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    task = _screen_task(tmp_path, ["StudyA", "StudyB", "StudyC"])
    prompts = _run(ClaudeRunner(), tmp_path, task, [
        {"pico_id": "pico_01", "batch": 1, "decisions": [
            {"citation_key": "StudyA", "include": True}, {"citation_key": "StudyC", "include": False}]},
        {"pico_id": "pico_01", "decisions": [{"citation_key": "StudyB", "include": True}]},
    ], monkeypatch)

    assert len(prompts) == 2
    retry = prompts[1]
    assert "abstract: AStudyB" in retry            # the omitted study travels
    assert "abstract: AStudyA" not in retry        # the answered ones do not
    assert "abstract: AStudyC" not in retry
    result = json.loads((tmp_path / "screen_pico_01_01.json").read_text(encoding="utf-8"))
    assert [d["citation_key"] for d in result["decisions"]] == ["StudyA", "StudyB", "StudyC"]
    assert result["batch"] == 1                     # original metadata kept


def test_screen_repair_completing_on_the_final_attempt_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Completeness is checked after the last retry, not only before it.

    Regression: an earlier version only returned from the top of the retry
    loop, so a batch completed by the final attempt was still reported as
    incomplete and the whole job failed.
    """
    task = _screen_task(tmp_path, ["StudyA", "StudyB", "StudyC"])
    prompts = _run(ClaudeRunner(), tmp_path, task, [
        {"pico_id": "pico_01", "batch": 1, "decisions": [{"citation_key": "StudyA", "include": True}]},
        {"pico_id": "pico_01", "decisions": [{"citation_key": "StudyB", "include": True}]},
        {"pico_id": "pico_01", "decisions": [{"citation_key": "StudyC", "include": False}]},
    ], monkeypatch)

    assert len(prompts) == 3  # initial + both allowed repairs
    result = json.loads((tmp_path / "screen_pico_01_01.json").read_text(encoding="utf-8"))
    assert [d["citation_key"] for d in result["decisions"]] == ["StudyA", "StudyB", "StudyC"]


def test_screen_repair_stops_once_a_retry_adds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A retry that returns nothing new ends the loop; asking again would not help."""
    task = _screen_task(tmp_path, ["StudyA", "StudyB"])
    with pytest.raises(WorkError, match="StudyB"):
        _run(ClaudeRunner(), tmp_path, task, [
            {"pico_id": "pico_01", "decisions": [{"citation_key": "StudyA", "include": True}]},
            {"pico_id": "pico_01", "decisions": []},
        ], monkeypatch)
    assert not (tmp_path / "screen_pico_01_01.json").exists()


def test_screen_extra_keys_do_not_trigger_a_full_rerun(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A hallucinated or repeated key is ignored; a complete batch needs no retry."""
    task = _screen_task(tmp_path, ["StudyA", "StudyB"])
    prompts = _run(ClaudeRunner(), tmp_path, task, [
        {"pico_id": "pico_01", "batch": 1, "decisions": [
            {"citation_key": "@StudyA", "include": True},          # @ prefix tolerated
            {"citation_key": "StudyB", "include": False},
            {"citation_key": "StudyB", "include": True},           # duplicate: first wins
            {"citation_key": "Invented2020", "include": True},     # not asked about
        ]},
    ], monkeypatch)
    assert len(prompts) == 1
    result = json.loads((tmp_path / "screen_pico_01_01.json").read_text(encoding="utf-8"))
    assert [d["citation_key"] for d in result["decisions"]] == ["StudyA", "StudyB"]
    assert [d["include"] for d in result["decisions"]] == [True, False]


def test_screen_missing_after_all_attempts_names_the_gap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    task = _screen_task(tmp_path, ["StudyA", "StudyB"])
    answers = [
        {"pico_id": "pico_01", "decisions": [{"citation_key": "StudyA", "include": True}]},
        {"pico_id": "pico_01", "decisions": [{"citation_key": "Invented", "include": True}]},
    ]
    with pytest.raises(WorkError, match="StudyB"):
        _run(ClaudeRunner(), tmp_path, task, answers, monkeypatch)


# ---------------------------------------------------------------------------
# Per-question year window and the expert checkpoint timeout fallback
# ---------------------------------------------------------------------------

def test_pico_draft_year_window_defaults_to_2000_and_is_bounded():
    from litreview.web.runner import PicoDraft

    pico = {"pico_id": "pico_01", "population": "adults", "intervention": "HIIT",
            "comparator": "MICT", "outcome": "VO2max", "question_text": "q",
            "claim_direction": "benefit", "primary_terms": ["HIIT"], "secondary_terms": ["VO2max"]}
    assert PicoDraft.model_validate({"picos": [pico]}).min_year == 2000
    assert PicoDraft.model_validate({"picos": [pico], "min_year": 1990}).min_year == 1990
    with pytest.raises(ValidationError):
        PicoDraft.model_validate({"picos": [pico], "min_year": 1800})


def test_job_config_uses_the_question_s_year_window():
    from litreview.web.runner import BriefWorker

    worker = BriefWorker.__new__(BriefWorker)          # config helper needs no store/runner
    assert worker.job_config({}).min_year == 2000       # default for an older job record
    assert worker.job_config({"min_year": 2016}).min_year == 2016
    # A question is not a claim appraisal: unranked journals are screened, not dropped.
    assert worker.job_config({}).strict_quartile is False


def test_expert_review_needs_human_halts_and_invites_a_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from litreview.web.runner import BriefWorker, ExpertReview

    updates: list[dict] = []

    class _Store:
        def update(self, _job_id: str, **changes):
            updates.append(changes)
            return changes

    worker = BriefWorker.__new__(BriefWorker)
    worker.store, worker.cfg, worker.runner = _Store(), None, None
    monkeypatch.setattr(worker, "evidence", lambda _base: {"studies": {}, "gaps": {}})
    monkeypatch.setattr(BriefWorker, "_expert_review", lambda *_a, **_k: None, raising=False)

    review = ExpertReview(note="pico_01 的納入研究多為錯誤族群。", needs_human=True)
    monkeypatch.setattr(worker, "runner", type("R", (), {"expert_checkpoint": staticmethod(lambda _b: review)})())
    recorded: list = []
    monkeypatch.setattr("litreview.web.runner.brief_flow.record_checkpoint",
                        lambda *a, **k: recorded.append(a))

    halted = worker._run_expert_checkpoint("job", tmp_path)
    assert halted is True
    assert updates[-1]["status"] == "checkpoint"          # the cloud accepts a checkpoint command here
    assert "錯誤族群" in updates[-1]["message"]
    assert not recorded                                   # the stage stays locked

    # A person sending the checkpoint command overrides the halt.
    updates.clear()
    assert worker._run_expert_checkpoint("job", tmp_path, force=True) is False
    assert recorded and updates[-1]["status"] == "running"


def test_timed_checkpoint_forces_model_continuation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from litreview.web.runner import BriefWorker

    calls: list[tuple[str, bool]] = []

    class _Store:
        def get(self, _job_id: str):
            return {"id": "job", "phase": "auto_checkpoint", "min_year": 2000}

        def base(self, _job_id: str) -> Path:
            return tmp_path

        def update(self, _job_id: str, **_changes):
            return _changes

    worker = BriefWorker.__new__(BriefWorker)
    worker.store = _Store()
    worker.runner = None

    def fake_checkpoint(job_id: str, _base: Path, *, force: bool = False):
        calls.append((job_id, force))
        return False

    monkeypatch.setattr(worker, "_run_expert_checkpoint", fake_checkpoint)

    async def completed(_base: Path, _cfg):
        return SimpleNamespace(status="done", states=[], fulltext_enabled=False)

    monkeypatch.setattr("litreview.web.runner.brief_flow.run_next", completed)
    worker._execute("job")

    assert calls == [("job", True)]
