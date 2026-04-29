#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Tests for phase_b_runner.py.

Run:  pytest founder-skills/tests/test_phase_b_runner.py -v

Tests cover:
- Schema validation: missing fields, wrong types, duplicate ids, cycles, unknown deps.
- Gate 2 verification: missing required Phase A artifact, invalid JSON in artifact.
- Step types: subprocess, rename, noop-halt, with stdin_from piping.
- Halt protocol: halt: true exits with halted_for_user, --resume-after picks up.
- --step single-step mode.
- --auto-continue treats halts as no-ops.
- Failure propagation: non-zero exit skips downstream dependents.
- Per-step timeout enforced.
- Placeholder substitution.
- Unknown step id in --step / --resume-after errors.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
RUNNER = SCRIPT_DIR.parent / "scripts" / "phase_b_runner.py"
PLUGIN_ROOT = SCRIPT_DIR.parent  # founder-skills/


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def _make_workspace(
    manifest: dict,
    artifacts: dict[str, dict] | None = None,
    *,
    extra_files: dict[str, str] | None = None,
) -> tuple[Path, Path, Path, Callable[[], None]]:
    """Create a tempdir with WORK_DIR + scripts dir + manifest + Phase A artifacts.

    Returns (work_dir, scripts_dir, manifest_path, cleanup_fn).
    """
    tmp = tempfile.mkdtemp()
    work_dir = Path(tmp) / "work"
    scripts_dir = Path(tmp) / "scripts"
    work_dir.mkdir()
    scripts_dir.mkdir()
    manifest_path = work_dir / "RUN_MANIFEST.json"
    _write_json(manifest_path, manifest)
    for name, payload in (artifacts or {}).items():
        _write_json(work_dir / name, payload)
    for name, content in (extra_files or {}).items():
        path = scripts_dir / name
        path.write_text(content)
        path.chmod(0o755)

    def cleanup() -> None:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    return work_dir, scripts_dir, manifest_path, cleanup


def _run_runner(
    manifest_path: Path,
    work_dir: Path,
    scripts_dir: Path,
    *,
    extra_args: list[str] | None = None,
) -> tuple[int, str, str, dict]:
    cmd = [
        sys.executable,
        str(RUNNER),
        "--manifest",
        str(manifest_path),
        "--work-dir",
        str(work_dir),
        "--scripts",
        str(scripts_dir),
        "--shared-scripts",
        str(PLUGIN_ROOT / "scripts"),
        "--founder-skills-root",
        str(PLUGIN_ROOT),
    ]
    if extra_args:
        cmd.extend(extra_args)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    result_path = work_dir / "RUN_RESULT.json"
    result = json.loads(result_path.read_text()) if result_path.is_file() else {}
    return proc.returncode, proc.stdout, proc.stderr, result


def _minimal_manifest(steps: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "skill": "test-skill",
        "plugin_version": "0.4.0",
        "run_id": "test-run-1",
        "phase_a_complete": True,
        "phase_a_artifacts": [],
        "phase_a_missing": [],
        "phase_b_pending": True,
        "phase_b_steps": steps,
    }


# --------------------------------------------------------------------------
# Schema validation
# --------------------------------------------------------------------------


def test_missing_required_top_level_field() -> None:
    m = _minimal_manifest([])
    del m["run_id"]
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, stderr, result = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 1
        assert "run_id" in stderr
        assert result["phase_b_status"] == "failed"
        assert "manifest_error" in result
    finally:
        cleanup()


def test_wrong_schema_version() -> None:
    m = _minimal_manifest([])
    m["schema_version"] = 999
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, stderr, result = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 1
        assert "schema_version" in stderr.lower()
        assert result["phase_b_status"] == "failed"
    finally:
        cleanup()


def test_duplicate_step_ids() -> None:
    m = _minimal_manifest(
        [
            {"id": "alpha", "step_type": "noop-halt"},
            {"id": "alpha", "step_type": "noop-halt"},
        ]
    )
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, stderr, _ = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 1
        assert "Duplicate" in stderr
    finally:
        cleanup()


def test_cyclic_dependencies() -> None:
    m = _minimal_manifest(
        [
            {"id": "a", "step_type": "noop-halt", "depends_on": ["b"]},
            {"id": "b", "step_type": "noop-halt", "depends_on": ["a"]},
        ]
    )
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, stderr, _ = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 1
        assert "Cycle" in stderr or "cycle" in stderr
    finally:
        cleanup()


def test_dependency_on_unknown_step() -> None:
    m = _minimal_manifest(
        [
            {"id": "a", "step_type": "noop-halt", "depends_on": ["nonexistent"]},
        ]
    )
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, stderr, _ = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 1
        assert "unknown step" in stderr or "nonexistent" in stderr
    finally:
        cleanup()


def test_subprocess_step_missing_cmd() -> None:
    m = _minimal_manifest(
        [
            {"id": "a", "step_type": "subprocess"},  # no cmd
        ]
    )
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, stderr, _ = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 1
        assert "cmd" in stderr.lower()
    finally:
        cleanup()


# --------------------------------------------------------------------------
# Gate 2 — Phase A verification
# --------------------------------------------------------------------------


def test_required_phase_a_artifact_missing() -> None:
    m = _minimal_manifest([])
    m["phase_a_artifacts"] = [{"path": "inputs.json", "required": True}]
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, stderr, result = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 1
        assert "missing" in stderr.lower()
        assert result["phase_b_status"] == "failed"
        assert "phase_a_errors" in result
    finally:
        cleanup()


def test_required_phase_a_artifact_invalid_json() -> None:
    m = _minimal_manifest([])
    m["phase_a_artifacts"] = [{"path": "broken.json", "required": True}]
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        # Write malformed JSON
        (work_dir / "broken.json").write_text("{not valid json")
        rc, _, stderr, result = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 1
        assert "valid JSON" in stderr or "valid json" in stderr.lower()
        assert result["phase_b_status"] == "failed"
    finally:
        cleanup()


def test_phase_a_complete_false_blocks_phase_b() -> None:
    m = _minimal_manifest(
        [
            {
                "id": "noop",
                "step_type": "subprocess",
                "cmd": ["python3", "-c", "print('should not run')"],
            }
        ]
    )
    m["phase_a_complete"] = False
    m["phase_a_missing"] = ["expected.json"]
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, stderr, result = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 1
        assert "phase_a_complete is false" in stderr or "phase_a_complete" in stderr
        # Should not have run any steps
        assert result["steps"] == []
    finally:
        cleanup()


def test_optional_phase_a_artifact_missing_is_ok() -> None:
    m = _minimal_manifest([{"id": "noop", "step_type": "noop-halt"}])
    m["phase_a_artifacts"] = [{"path": "optional.json", "required": False}]
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, _, result = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 0
        assert result["phase_b_status"] == "success"
    finally:
        cleanup()


# --------------------------------------------------------------------------
# Step execution: subprocess
# --------------------------------------------------------------------------


def test_subprocess_success() -> None:
    m = _minimal_manifest(
        [
            {
                "id": "echo",
                "step_type": "subprocess",
                "cmd": ["python3", "-c", "print('hello world')"],
            }
        ]
    )
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, _, result = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 0
        assert result["phase_b_status"] == "success"
        assert result["steps"][0]["status"] == "success"
        assert "hello world" in result["steps"][0]["stdout_excerpt"]
    finally:
        cleanup()


def test_subprocess_failure_skips_dependents() -> None:
    m = _minimal_manifest(
        [
            {
                "id": "fail",
                "step_type": "subprocess",
                "cmd": ["python3", "-c", "import sys; sys.exit(7)"],
            },
            {
                "id": "downstream",
                "step_type": "subprocess",
                "cmd": ["python3", "-c", "print('should not run')"],
                "depends_on": ["fail"],
            },
        ]
    )
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, _, result = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 1
        # First step failed
        fail_step = next(s for s in result["steps"] if s["id"] == "fail")
        assert fail_step["status"] == "failed"
        assert fail_step["exit_code"] == 7
        # Downstream skipped
        ds_step = next(s for s in result["steps"] if s["id"] == "downstream")
        assert ds_step["status"] == "skipped"
        assert result["phase_b_status"] == "failed"
    finally:
        cleanup()


def test_partial_when_independent_step_succeeds_alongside_failure() -> None:
    m = _minimal_manifest(
        [
            {
                "id": "fail",
                "step_type": "subprocess",
                "cmd": ["python3", "-c", "import sys; sys.exit(1)"],
            },
            {
                "id": "ok",
                "step_type": "subprocess",
                "cmd": ["python3", "-c", "print('ok')"],
            },
        ]
    )
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, _, result = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 1
        assert result["phase_b_status"] == "partial"
    finally:
        cleanup()


def test_stdin_from_piping() -> None:
    # Script reads stdin and writes the input length to a file
    m = _minimal_manifest(
        [
            {
                "id": "consume",
                "step_type": "subprocess",
                "cmd": [
                    "python3",
                    "-c",
                    "import sys, pathlib; data=sys.stdin.read(); pathlib.Path('out.txt').write_text(str(len(data)))",
                ],
                "stdin_from": "input.json",
            }
        ]
    )
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m, artifacts={"input.json": {"a": 1, "b": 2}})
    try:
        rc, _, _, _ = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 0
        # Verify stdin was actually piped
        out = (work_dir / "out.txt").read_text()
        original = (work_dir / "input.json").read_text()
        assert out == str(len(original))
    finally:
        cleanup()


def test_stdin_from_missing_file() -> None:
    m = _minimal_manifest(
        [
            {
                "id": "needs_stdin",
                "step_type": "subprocess",
                "cmd": ["python3", "-c", "import sys; print(sys.stdin.read())"],
                "stdin_from": "nonexistent.json",
            }
        ]
    )
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, _, result = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 1
        step = result["steps"][0]
        assert step["status"] == "failed"
        assert "missing" in step["stderr_excerpt"].lower()
    finally:
        cleanup()


def test_per_step_timeout_enforced() -> None:
    m = _minimal_manifest(
        [
            {
                "id": "slow",
                "step_type": "subprocess",
                "cmd": ["python3", "-c", "import time; time.sleep(5)"],
                "timeout_seconds": 1,
            }
        ]
    )
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, _, result = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 1
        step = result["steps"][0]
        assert step["status"] == "failed"
        assert "timeout" in step["stderr_excerpt"].lower()
    finally:
        cleanup()


def test_placeholder_substitution() -> None:
    # Use $WORK_DIR and $SCRIPTS in cmd
    m = _minimal_manifest(
        [
            {
                "id": "echo_paths",
                "step_type": "subprocess",
                "cmd": ["python3", "-c", "import os, sys; print('OK' if os.environ.get('PWD') else 'NO_PWD')"],
            }
        ]
    )
    # Create a script under SCRIPTS that the cmd points to via placeholder
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(
        m,
        extra_files={"noop.py": "#!/usr/bin/env python3\nprint('hello from $SCRIPTS')\n"},
    )
    try:
        # Replace cmd to use $SCRIPTS placeholder pointing at our script
        m["phase_b_steps"][0]["cmd"] = ["python3", "$SCRIPTS/noop.py"]
        _write_json(manifest_path, m)
        rc, _, _, result = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 0
        assert "hello from $SCRIPTS" in result["steps"][0]["stdout_excerpt"]
    finally:
        cleanup()


# --------------------------------------------------------------------------
# Step execution: rename
# --------------------------------------------------------------------------


def test_rename_step_replaces_target() -> None:
    m = _minimal_manifest(
        [
            {
                "id": "mv_corrected",
                "step_type": "rename",
                "from_path": "corrected_inputs.json",
                "to_path": "inputs.json",
            }
        ]
    )
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(
        m,
        artifacts={
            "corrected_inputs.json": {"corrected": True},
            "inputs.json": {"corrected": False},
        },
    )
    try:
        rc, _, _, result = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 0
        assert result["phase_b_status"] == "success"
        # corrected_inputs.json gone, inputs.json overwritten with the corrected payload
        assert not (work_dir / "corrected_inputs.json").exists()
        loaded = json.loads((work_dir / "inputs.json").read_text())
        assert loaded == {"corrected": True}
    finally:
        cleanup()


def test_rename_step_missing_source_fails() -> None:
    m = _minimal_manifest(
        [
            {
                "id": "mv_missing",
                "step_type": "rename",
                "from_path": "nonexistent.json",
                "to_path": "inputs.json",
            }
        ]
    )
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, _, result = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 1
        assert result["steps"][0]["status"] == "failed"
    finally:
        cleanup()


# --------------------------------------------------------------------------
# Halt protocol
# --------------------------------------------------------------------------


def test_halt_step_exits_with_halted_for_user() -> None:
    m = _minimal_manifest(
        [
            {
                "id": "first",
                "step_type": "subprocess",
                "cmd": ["python3", "-c", "print('first')"],
            },
            {
                "id": "halt_here",
                "step_type": "noop-halt",
                "halt": True,
                "halt_message": "Caller, do something",
                "halt_data": {"consumer": "human", "expected_upload_artifact": "upload.json"},
                "depends_on": ["first"],
            },
            {
                "id": "after_halt",
                "step_type": "subprocess",
                "cmd": ["python3", "-c", "print('should not run')"],
                "depends_on": ["halt_here"],
            },
        ]
    )
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, _, result = _run_runner(manifest_path, work_dir, scripts_dir)
        assert rc == 0  # halted_for_user exits 0
        assert result["phase_b_status"] == "halted_for_user"
        assert result["halted_after"] == "halt_here"
        assert result["user_action_required"] == "Caller, do something"
        assert result["halt_data"]["consumer"] == "human"
        assert result["halt_data"]["expected_upload_artifact"] == "upload.json"
        # after_halt should NOT have run
        ids = [s["id"] for s in result["steps"]]
        assert "after_halt" not in ids
    finally:
        cleanup()


def test_resume_after_skips_through_named_step() -> None:
    m = _minimal_manifest(
        [
            {
                "id": "skipped_step",
                "step_type": "subprocess",
                "cmd": ["python3", "-c", "print('should not run')"],
            },
            {
                "id": "ran_step",
                "step_type": "subprocess",
                "cmd": ["python3", "-c", "print('did run')"],
                "depends_on": ["skipped_step"],
            },
        ]
    )
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, _, result = _run_runner(
            manifest_path,
            work_dir,
            scripts_dir,
            extra_args=["--resume-after", "skipped_step"],
        )
        assert rc == 0
        ids = [s["id"] for s in result["steps"]]
        assert "skipped_step" not in ids
        assert "ran_step" in ids
        assert result["steps"][0]["status"] == "success"
    finally:
        cleanup()


def test_resume_after_unknown_step_id() -> None:
    m = _minimal_manifest([{"id": "only_step", "step_type": "noop-halt"}])
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, stderr, _ = _run_runner(
            manifest_path,
            work_dir,
            scripts_dir,
            extra_args=["--resume-after", "nonexistent"],
        )
        assert rc == 2
        assert "unknown" in stderr.lower() or "nonexistent" in stderr
    finally:
        cleanup()


def test_auto_continue_treats_halt_as_noop() -> None:
    m = _minimal_manifest(
        [
            {
                "id": "halt_here",
                "step_type": "noop-halt",
                "halt": True,
                "halt_message": "Should be skipped under --auto-continue",
            },
            {
                "id": "after_halt",
                "step_type": "subprocess",
                "cmd": ["python3", "-c", "print('after halt')"],
                "depends_on": ["halt_here"],
            },
        ]
    )
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, _, result = _run_runner(manifest_path, work_dir, scripts_dir, extra_args=["--auto-continue"])
        assert rc == 0
        assert result["phase_b_status"] == "success"
        # Both ran
        ids = [s["id"] for s in result["steps"]]
        assert "halt_here" in ids
        assert "after_halt" in ids
        # after_halt successfully ran
        after = next(s for s in result["steps"] if s["id"] == "after_halt")
        assert after["status"] == "success"
    finally:
        cleanup()


# --------------------------------------------------------------------------
# --step single-step mode
# --------------------------------------------------------------------------


def test_step_runs_only_named_step() -> None:
    m = _minimal_manifest(
        [
            {
                "id": "first",
                "step_type": "subprocess",
                "cmd": ["python3", "-c", "print('first')"],
            },
            {
                "id": "second",
                "step_type": "subprocess",
                "cmd": ["python3", "-c", "print('second')"],
                "depends_on": ["first"],
            },
        ]
    )
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, _, result = _run_runner(manifest_path, work_dir, scripts_dir, extra_args=["--step", "second"])
        assert rc == 0
        # Only `second` ran
        assert len(result["steps"]) == 1
        assert result["steps"][0]["id"] == "second"
    finally:
        cleanup()


def test_step_unknown_id_errors() -> None:
    m = _minimal_manifest([{"id": "only", "step_type": "noop-halt"}])
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, stderr, _ = _run_runner(manifest_path, work_dir, scripts_dir, extra_args=["--step", "wrong"])
        assert rc == 2
        assert "unknown" in stderr.lower() or "wrong" in stderr
    finally:
        cleanup()


def test_step_and_resume_after_mutually_exclusive() -> None:
    m = _minimal_manifest([{"id": "a", "step_type": "noop-halt"}])
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, stderr, _ = _run_runner(
            manifest_path,
            work_dir,
            scripts_dir,
            extra_args=["--step", "a", "--resume-after", "a"],
        )
        assert rc == 2
        assert "mutually exclusive" in stderr.lower()
    finally:
        cleanup()


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------


def test_dry_run_does_not_execute() -> None:
    m = _minimal_manifest(
        [
            {
                "id": "would_fail",
                "step_type": "subprocess",
                "cmd": ["python3", "-c", "import sys; sys.exit(1)"],
            }
        ]
    )
    work_dir, scripts_dir, manifest_path, cleanup = _make_workspace(m)
    try:
        rc, _, _, result = _run_runner(manifest_path, work_dir, scripts_dir, extra_args=["--dry-run"])
        # Dry run reports success because no step actually executed
        assert rc == 0
        assert result["steps"][0]["status"] == "dry-run"
    finally:
        cleanup()
