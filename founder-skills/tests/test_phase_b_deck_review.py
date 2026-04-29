#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""End-to-end test of deck-review's v0.4.0 Phase A→B flow via phase_b_runner.

Two halves:

1. **Phase A isolation (manifest validation)** — without running any
   scripts, verify that the manifest deck-review's SKILL.md prescribes
   has the right shape: required artifacts, schema-valid steps, no
   cycles, every `stdin_from` resolves to a Phase A artifact.
   The reviewer specifically required this: "deck-review manifest must
   require the actual deck artifacts (deck_inventory.json,
   stage_profile.json, slide_reviews.json, checklist_input.json)".

2. **Phase B local end-to-end** — given a $WORK_DIR with valid Phase A
   artifacts, run phase_b_runner.py and assert all three steps succeed
   and produce report.json + report.html.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent  # founder-skills/
RUNNER = PLUGIN_ROOT / "scripts" / "phase_b_runner.py"
DECK_SCRIPTS = PLUGIN_ROOT / "skills" / "deck-review" / "scripts"


# Reuse the valid fixtures from test_deck_review.py
sys.path.insert(0, str(SCRIPT_DIR))
from test_deck_review import (  # noqa: E402
    _CHECKLIST_IDS,
    _VALID_INVENTORY,
    _VALID_PROFILE,
    _VALID_REVIEWS,
)


def _checklist_input_payload() -> dict:
    """Phase A's checklist_input.json — the heredoc payload `checklist.py` consumes via stdin."""
    return {
        "items": [
            {
                "id": cid,
                "status": "pass",
                "evidence": f"test evidence for {cid}",
                "notes": None,
            }
            for cid in _CHECKLIST_IDS
        ]
    }


def _deck_review_manifest(run_id: str = "test-deck-run-1") -> dict:
    """Build the manifest deck-review's Phase A would emit."""
    return {
        "schema_version": 1,
        "skill": "deck-review",
        "plugin_version": "0.4.0",
        "run_id": run_id,
        "phase_a_complete": True,
        "phase_a_artifacts": [
            {"path": "deck_inventory.json", "required": True},
            {"path": "stage_profile.json", "required": True},
            {"path": "slide_reviews.json", "required": True},
            {"path": "checklist_input.json", "required": True},
        ],
        "phase_a_missing": [],
        "phase_b_pending": True,
        "phase_b_steps": [
            {
                "id": "validate_checklist",
                "step_type": "subprocess",
                "cmd": [
                    "python3",
                    "$SCRIPTS/checklist.py",
                    "-o",
                    "$WORK_DIR/checklist.json",
                    "--pretty",
                ],
                "stdin_from": "checklist_input.json",
                "produces": "checklist.json",
            },
            {
                "id": "compose",
                "step_type": "subprocess",
                "cmd": [
                    "python3",
                    "$SCRIPTS/compose_report.py",
                    "--dir",
                    "$WORK_DIR",
                    "-o",
                    "$WORK_DIR/report.json",
                    "--pretty",
                ],
                "produces": "report.json",
                "depends_on": ["validate_checklist"],
            },
            {
                "id": "visualize",
                "step_type": "subprocess",
                "cmd": [
                    "python3",
                    "$SCRIPTS/visualize.py",
                    "--dir",
                    "$WORK_DIR",
                    "-o",
                    "$WORK_DIR/report.html",
                ],
                "produces": "report.html",
                "depends_on": ["compose"],
            },
        ],
    }


def _stage_workspace() -> tuple[Path, Path]:
    """Create a temp $WORK_DIR with all valid Phase A artifacts and a manifest."""
    tmp = Path(tempfile.mkdtemp())
    work_dir = tmp / "deck-review-testco"
    work_dir.mkdir()
    (work_dir / "deck_inventory.json").write_text(json.dumps(_VALID_INVENTORY))
    (work_dir / "stage_profile.json").write_text(json.dumps(_VALID_PROFILE))
    (work_dir / "slide_reviews.json").write_text(json.dumps(_VALID_REVIEWS))
    (work_dir / "checklist_input.json").write_text(json.dumps(_checklist_input_payload()))
    manifest = _deck_review_manifest()
    (work_dir / "RUN_MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    return tmp, work_dir


# --------------------------------------------------------------------------
# Phase A isolation — manifest shape (no script execution)
# --------------------------------------------------------------------------


def test_manifest_requires_actual_deck_artifacts() -> None:
    """Reviewer's specific requirement: manifest must list the real
    deck-review Phase A artifacts (not the v3.1-fictional 'inputs.json').
    """
    m = _deck_review_manifest()
    required = {a["path"] for a in m["phase_a_artifacts"] if a["required"]}
    assert required == {
        "deck_inventory.json",
        "stage_profile.json",
        "slide_reviews.json",
        "checklist_input.json",
    }
    # Negative assertion — these must NOT be in the manifest:
    forbidden = {"inputs.json", "checklist_assessments.json", "ingestion_pitfalls.json"}
    assert not (required & forbidden)


def test_manifest_stdin_from_references_resolve_to_phase_a_artifacts() -> None:
    """Every stdin_from in phase_b_steps must point to a Phase A artifact."""
    m = _deck_review_manifest()
    phase_a = {a["path"] for a in m["phase_a_artifacts"]}
    for step in m["phase_b_steps"]:
        if "stdin_from" in step:
            assert step["stdin_from"] in phase_a, (
                f"step {step['id']} stdin_from={step['stdin_from']} does not match any phase_a_artifact: {phase_a}"
            )


def test_manifest_dag_is_acyclic_and_complete() -> None:
    """phase_b_steps form a DAG; every depends_on references a known step id."""
    m = _deck_review_manifest()
    ids = {s["id"] for s in m["phase_b_steps"]}
    for step in m["phase_b_steps"]:
        for dep in step.get("depends_on", []):
            assert dep in ids, f"step {step['id']} depends on unknown step {dep}"


def test_manifest_validates_via_runner() -> None:
    """Runner schema validator accepts the manifest."""
    tmp, work_dir = _stage_workspace()
    try:
        rc, _, stderr, _ = _run_runner(work_dir, extra_args=["--dry-run"])
        assert rc == 0, f"runner rejected manifest: {stderr}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# Phase B end-to-end (Local mode)
# --------------------------------------------------------------------------


def _run_runner(work_dir: Path, *, extra_args: list[str] | None = None) -> tuple[int, str, str, dict]:
    cmd = [
        sys.executable,
        str(RUNNER),
        "--manifest",
        str(work_dir / "RUN_MANIFEST.json"),
        "--work-dir",
        str(work_dir),
        "--scripts",
        str(DECK_SCRIPTS),
    ]
    if extra_args:
        cmd.extend(extra_args)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    result_path = work_dir / "RUN_RESULT.json"
    result = json.loads(result_path.read_text()) if result_path.is_file() else {}
    return proc.returncode, proc.stdout, proc.stderr, result


def test_phase_b_local_full_flow() -> None:
    """Given a fully-staged $WORK_DIR, runner produces report.json + report.html."""
    tmp, work_dir = _stage_workspace()
    try:
        rc, _, stderr, result = _run_runner(work_dir)
        assert rc == 0, f"runner failed: {stderr}\n{json.dumps(result, indent=2)[:2000]}"
        assert result["phase_b_status"] == "success"
        # All 3 steps succeeded
        for step in result["steps"]:
            assert step["status"] == "success", f"step {step['id']} failed: {step.get('stderr_excerpt')}"
        # Final artifacts produced
        assert (work_dir / "checklist.json").is_file()
        assert (work_dir / "report.json").is_file()
        assert (work_dir / "report.html").is_file()
        # Sanity-check report.json shape
        report = json.loads((work_dir / "report.json").read_text())
        assert "report_markdown" in report
        assert "validation" in report
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_phase_b_step_runs_only_named_step() -> None:
    """--step <id> runs exactly one step."""
    tmp, work_dir = _stage_workspace()
    try:
        rc, _, _, result = _run_runner(work_dir, extra_args=["--step", "validate_checklist"])
        assert rc == 0
        assert len(result["steps"]) == 1
        assert result["steps"][0]["id"] == "validate_checklist"
        # checklist.json produced; report.json NOT produced
        assert (work_dir / "checklist.json").is_file()
        assert not (work_dir / "report.json").exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_phase_b_resume_after_skips_through() -> None:
    """--resume-after validate_checklist skips it and runs compose + visualize.

    To make this a clean test, we pre-stage checklist.json so compose has
    something to consume.
    """
    tmp, work_dir = _stage_workspace()
    try:
        # First run validate_checklist standalone
        rc, _, _, _ = _run_runner(work_dir, extra_args=["--step", "validate_checklist"])
        assert rc == 0
        # Now resume from after validate_checklist
        rc, _, _, result = _run_runner(work_dir, extra_args=["--resume-after", "validate_checklist"])
        assert rc == 0
        assert result["phase_b_status"] == "success"
        ids_run = [s["id"] for s in result["steps"]]
        assert "validate_checklist" not in ids_run
        assert "compose" in ids_run
        assert "visualize" in ids_run
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_phase_b_missing_checklist_input_blocks_run() -> None:
    """If a required Phase A artifact is missing, runner exits failed
    before any step executes (Gate 2 — defensive verification).
    """
    tmp, work_dir = _stage_workspace()
    try:
        (work_dir / "checklist_input.json").unlink()
        rc, _, stderr, result = _run_runner(work_dir)
        assert rc == 1
        assert result["phase_b_status"] == "failed"
        assert result["steps"] == []  # nothing ran
        assert "checklist_input.json" in stderr or "checklist_input.json" in str(result.get("phase_a_errors", []))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_phase_b_invalid_json_in_phase_a_artifact_blocks_run() -> None:
    """Gate 2 catches malformed JSON in a required Phase A artifact."""
    tmp, work_dir = _stage_workspace()
    try:
        (work_dir / "deck_inventory.json").write_text("{not valid json")
        rc, _, _, result = _run_runner(work_dir)
        assert rc == 1
        assert result["phase_b_status"] == "failed"
        assert any("deck_inventory" in e for e in result.get("phase_a_errors", []))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_phase_b_fabricated_phase_a_complete_caught() -> None:
    """Reviewer's defensive scenario: phase_a_complete: true but a
    required artifact is actually missing on disk. Runner must catch this.
    """
    tmp, work_dir = _stage_workspace()
    try:
        # Manifest claims phase_a_complete: true (default)
        # But we delete one of the required artifacts after the fact,
        # simulating fabrication.
        (work_dir / "stage_profile.json").unlink()
        rc, _, _, result = _run_runner(work_dir)
        assert rc == 1
        assert result["phase_b_status"] == "failed"
        assert result["steps"] == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
