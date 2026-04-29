#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Phase B runner — execute the script pipeline a founder-skills sub-agent
declared in its RUN_MANIFEST.json.

Reads RUN_MANIFEST.json, validates schema, verifies Phase A artifacts,
runs phase_b_steps in dependency order, captures per-step results, emits
RUN_RESULT.json.

Step types:
- subprocess (default): exec cmd[]; pipe stdin_from if set; capture
  stdout/stderr; enforce timeout.
- rename: os.replace(from_path, to_path) under $WORK_DIR.
- noop-halt: no-op marker that pairs with halt: true to pause for the
  caller to write an artifact based on prior steps.

Halt protocol: a step with halt: true that runs successfully causes the
runner to exit immediately with phase_b_status: halted_for_user. Caller
re-invokes with --resume-after <step_id> after performing the halt action.

Placeholders expanded in cmd[]: $WORK_DIR, $SCRIPTS, $SHARED_SCRIPTS,
$FOUNDER_SKILLS_ROOT.

Exit codes:
- 0: phase_b_status in {success, halted_for_user}
- 1: phase_b_status in {failed, partial}
- 2: invocation error (bad args, schema invalid, manifest unreadable)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 300
EXCERPT_LIMIT = 4096

PLACEHOLDER_KEYS = ("$WORK_DIR", "$SCRIPTS", "$SHARED_SCRIPTS", "$FOUNDER_SKILLS_ROOT")


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


# --------------------------------------------------------------------------
# Manifest loading + schema validation
# --------------------------------------------------------------------------


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise InvocationError(f"Manifest not found: {path}")
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise InvocationError(f"Manifest is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise InvocationError("Manifest must be a JSON object at top level")
    return data


class InvocationError(Exception):
    """Bad inputs or unreadable manifest. Exit code 2."""


class ManifestSchemaError(Exception):
    """Manifest structure violates schema. Run is failed; exit code 1."""


def validate_manifest(m: dict[str, Any]) -> None:
    """Lightweight structural validation.

    We don't pull in jsonschema as a dependency — this validates the
    fields the runner actually uses.
    """
    required_top = (
        "schema_version",
        "skill",
        "plugin_version",
        "run_id",
        "phase_a_complete",
        "phase_a_artifacts",
        "phase_a_missing",
        "phase_b_pending",
        "phase_b_steps",
    )
    for k in required_top:
        if k not in m:
            raise ManifestSchemaError(f"Missing required top-level field: {k}")

    if m["schema_version"] != SCHEMA_VERSION:
        raise ManifestSchemaError(
            f"Unsupported schema_version: {m['schema_version']} (runner supports {SCHEMA_VERSION})"
        )

    if not isinstance(m["phase_a_complete"], bool):
        raise ManifestSchemaError("phase_a_complete must be bool")

    if not isinstance(m["phase_a_artifacts"], list):
        raise ManifestSchemaError("phase_a_artifacts must be a list")
    for i, a in enumerate(m["phase_a_artifacts"]):
        if not isinstance(a, dict) or "path" not in a or "required" not in a:
            raise ManifestSchemaError(f"phase_a_artifacts[{i}] missing path/required")
        if not isinstance(a["path"], str) or not isinstance(a["required"], bool):
            raise ManifestSchemaError(f"phase_a_artifacts[{i}] has wrong types")

    if not isinstance(m["phase_b_steps"], list):
        raise ManifestSchemaError("phase_b_steps must be a list")
    seen_ids: set[str] = set()
    for i, step in enumerate(m["phase_b_steps"]):
        if not isinstance(step, dict):
            raise ManifestSchemaError(f"phase_b_steps[{i}] must be object")
        if "id" not in step or not isinstance(step["id"], str) or not step["id"]:
            raise ManifestSchemaError(f"phase_b_steps[{i}] missing id")
        if step["id"] in seen_ids:
            raise ManifestSchemaError(f"Duplicate step id: {step['id']}")
        seen_ids.add(step["id"])
        st = step.get("step_type", "subprocess")
        if st not in ("subprocess", "rename", "noop-halt"):
            raise ManifestSchemaError(f"Step {step['id']}: unknown step_type {st!r}")
        if st == "subprocess":
            if not isinstance(step.get("cmd"), list) or not step["cmd"]:
                raise ManifestSchemaError(f"Step {step['id']}: subprocess requires non-empty cmd[]")
            if not all(isinstance(c, str) for c in step["cmd"]):
                raise ManifestSchemaError(f"Step {step['id']}: cmd entries must be strings")
        elif st == "rename":
            for k in ("from_path", "to_path"):
                if not isinstance(step.get(k), str) or not step[k]:
                    raise ManifestSchemaError(f"Step {step['id']}: rename requires {k}")
        elif st == "noop-halt":
            # noop-halt should typically pair with halt: true, but we don't
            # enforce that — a no-op step without halt is harmless.
            pass
        deps = step.get("depends_on", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise ManifestSchemaError(f"Step {step['id']}: depends_on must be array of strings")
        for d in deps:
            if d not in seen_ids and not any(s["id"] == d for s in m["phase_b_steps"]):
                # Note: forward refs are allowed; we verify on toposort.
                pass

    _toposort(m["phase_b_steps"])  # raises on cycle


def _toposort(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return steps in dependency-resolved order. Raises ManifestSchemaError on cycle.

    Stable wrt manifest order: ties are broken by original index.
    """
    by_id = {s["id"]: s for s in steps}
    for s in steps:
        for d in s.get("depends_on") or []:
            if d not in by_id:
                raise ManifestSchemaError(f"Step {s['id']} depends on unknown step {d!r}")

    indeg: dict[str, int] = {sid: 0 for sid in by_id}
    deps_of: dict[str, list[str]] = {sid: list(by_id[sid].get("depends_on") or []) for sid in by_id}
    # Build reverse adjacency: who depends on me
    rev: dict[str, list[str]] = {sid: [] for sid in by_id}
    for sid, deps in deps_of.items():
        for d in deps:
            rev[d].append(sid)
            indeg[sid] += 1

    # Kahn, preserving manifest order via stable selection
    order: list[dict[str, Any]] = []
    ready = [s["id"] for s in steps if indeg[s["id"]] == 0]
    while ready:
        nxt = ready.pop(0)
        order.append(by_id[nxt])
        for child in rev[nxt]:
            indeg[child] -= 1
            if indeg[child] == 0:
                # Insert preserving original manifest order
                idx_child = next(i for i, s in enumerate(steps) if s["id"] == child)
                inserted = False
                for j, q in enumerate(ready):
                    idx_q = next(i for i, s in enumerate(steps) if s["id"] == q)
                    if idx_child < idx_q:
                        ready.insert(j, child)
                        inserted = True
                        break
                if not inserted:
                    ready.append(child)
    if len(order) != len(steps):
        remaining = sorted(set(by_id) - {s["id"] for s in order})
        raise ManifestSchemaError(f"Cycle in phase_b_steps; cannot order: {remaining}")
    return order


# --------------------------------------------------------------------------
# Gate 2 — defensive verification of Phase A artifacts
# --------------------------------------------------------------------------


def verify_phase_a(work_dir: Path, manifest: dict[str, Any]) -> list[str]:
    """Return a list of human-readable errors. Empty = OK."""
    errors: list[str] = []
    if not manifest["phase_a_complete"]:
        errors.append("phase_a_complete is false; cannot run Phase B")
        if manifest.get("phase_a_missing"):
            errors.append(f"  missing: {', '.join(manifest['phase_a_missing'])}")
        if manifest.get("step_0_required"):
            errors.append(f"  step_0_required: {manifest['step_0_required'].get('reason', '')}")
        if manifest.get("cross_skill_import_required"):
            blk = manifest["cross_skill_import_required"]
            errors.append(f"  cross_skill_import_required: {blk.get('reason', '')}")
        return errors

    for entry in manifest["phase_a_artifacts"]:
        if not entry["required"]:
            continue
        rel = entry["path"]
        full = work_dir / rel
        if not full.is_file():
            errors.append(f"Required Phase A artifact missing: {rel}")
            continue
        try:
            with full.open(encoding="utf-8") as f:
                json.load(f)
        except json.JSONDecodeError as e:
            errors.append(f"Required Phase A artifact is not valid JSON: {rel} ({e})")
        except OSError as e:
            errors.append(f"Required Phase A artifact unreadable: {rel} ({e})")
    return errors


# --------------------------------------------------------------------------
# Placeholder substitution
# --------------------------------------------------------------------------


def expand_placeholders(s: str, paths: dict[str, Path]) -> str:
    out = s
    for key in PLACEHOLDER_KEYS:
        out = out.replace(key, str(paths[key]))
    return out


def _expand_argv(argv: Iterable[str], paths: dict[str, Path]) -> list[str]:
    return [expand_placeholders(a, paths) for a in argv]


# --------------------------------------------------------------------------
# Step execution
# --------------------------------------------------------------------------


def _excerpt(s: str, limit: int = EXCERPT_LIMIT) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n…[{len(s) - limit} bytes truncated]"


def run_subprocess_step(step: dict[str, Any], paths: dict[str, Path], verbose: bool) -> dict[str, Any]:
    cmd = _expand_argv(step["cmd"], paths)
    timeout = int(step.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    stdin_data: bytes | None = None
    if step.get("stdin_from"):
        stdin_path = paths["$WORK_DIR"] / step["stdin_from"]
        if not stdin_path.is_file():
            return {
                "id": step["id"],
                "status": "failed",
                "exit_code": None,
                "stdout_excerpt": "",
                "stderr_excerpt": f"stdin_from file missing: {step['stdin_from']}",
                "duration_ms": 0,
            }
        stdin_data = stdin_path.read_bytes()

    if verbose:
        _eprint(f"[runner] {step['id']}: {' '.join(cmd)}")
        if step.get("stdin_from"):
            _eprint(f"[runner]   stdin_from: {step['stdin_from']} ({len(stdin_data or b'')} bytes)")

    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            timeout=timeout,
            cwd=str(paths["$WORK_DIR"]),
            check=False,
        )
    except FileNotFoundError as e:
        return {
            "id": step["id"],
            "status": "failed",
            "exit_code": None,
            "stdout_excerpt": "",
            "stderr_excerpt": f"Executable not found: {e}",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    except subprocess.TimeoutExpired as e:
        return {
            "id": step["id"],
            "status": "failed",
            "exit_code": None,
            "stdout_excerpt": _excerpt((e.stdout or b"").decode("utf-8", errors="replace")),
            "stderr_excerpt": _excerpt(
                (e.stderr or b"").decode("utf-8", errors="replace") + f"\n[runner] timeout after {timeout}s"
            ),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    duration_ms = int((time.monotonic() - started) * 1000)
    stdout_text = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    stderr_text = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
    return {
        "id": step["id"],
        "status": "success" if proc.returncode == 0 else "failed",
        "exit_code": proc.returncode,
        "stdout_excerpt": _excerpt(stdout_text),
        "stderr_excerpt": _excerpt(stderr_text),
        "duration_ms": duration_ms,
    }


def run_rename_step(step: dict[str, Any], paths: dict[str, Path]) -> dict[str, Any]:
    work_dir = paths["$WORK_DIR"]
    src = work_dir / expand_placeholders(step["from_path"], paths)
    dst = work_dir / expand_placeholders(step["to_path"], paths)
    started = time.monotonic()
    try:
        os.replace(src, dst)
    except OSError as e:
        return {
            "id": step["id"],
            "status": "failed",
            "exit_code": None,
            "stdout_excerpt": "",
            "stderr_excerpt": f"rename failed: {e}",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    return {
        "id": step["id"],
        "status": "success",
        "exit_code": 0,
        "stdout_excerpt": f"renamed {step['from_path']} -> {step['to_path']}",
        "stderr_excerpt": "",
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def run_noop_halt_step(step: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": step["id"],
        "status": "success",
        "exit_code": 0,
        "stdout_excerpt": "noop-halt",
        "stderr_excerpt": "",
        "duration_ms": 0,
    }


# --------------------------------------------------------------------------
# Pipeline driver
# --------------------------------------------------------------------------


def _filter_steps_for_resume(
    steps: list[dict[str, Any]], resume_after: str | None, single: str | None
) -> list[dict[str, Any]]:
    if single is not None:
        match = [s for s in steps if s["id"] == single]
        if not match:
            raise InvocationError(f"--step references unknown step id: {single}")
        return match
    if resume_after is None:
        return list(steps)
    idx = next((i for i, s in enumerate(steps) if s["id"] == resume_after), -1)
    if idx == -1:
        raise InvocationError(f"--resume-after references unknown step id: {resume_after}")
    return list(steps[idx + 1 :])


def execute_pipeline(
    manifest: dict[str, Any],
    paths: dict[str, Path],
    *,
    resume_after: str | None,
    single_step: str | None,
    auto_continue: bool,
    dry_run: bool,
    verbose: bool,
) -> dict[str, Any]:
    sorted_steps = _toposort(manifest["phase_b_steps"])
    selected = _filter_steps_for_resume(sorted_steps, resume_after, single_step)

    step_results: list[dict[str, Any]] = []
    skipped_due_to_failure: set[str] = set()
    halted_after: str | None = None
    halt_data: dict[str, Any] | None = None
    user_action_required: str | None = None
    produced: list[str] = []
    failed_any = False
    succeeded_any = False

    for step in selected:
        # Skip if any dep failed
        deps = step.get("depends_on") or []
        if any(d in skipped_due_to_failure for d in deps):
            failed_deps = ", ".join(d for d in deps if d in skipped_due_to_failure)
            step_results.append(
                {
                    "id": step["id"],
                    "status": "skipped",
                    "exit_code": None,
                    "stdout_excerpt": "",
                    "stderr_excerpt": f"skipped: dependency failed ({failed_deps})",
                    "duration_ms": 0,
                }
            )
            skipped_due_to_failure.add(step["id"])
            continue

        if dry_run:
            step_results.append(
                {
                    "id": step["id"],
                    "status": "dry-run",
                    "exit_code": None,
                    "stdout_excerpt": f"would run step_type={step.get('step_type', 'subprocess')}",
                    "stderr_excerpt": "",
                    "duration_ms": 0,
                }
            )
            continue

        st = step.get("step_type", "subprocess")
        if st == "subprocess":
            res = run_subprocess_step(step, paths, verbose)
        elif st == "rename":
            res = run_rename_step(step, paths)
        elif st == "noop-halt":
            res = run_noop_halt_step(step)
        else:  # unreachable; schema validated
            res = {
                "id": step["id"],
                "status": "failed",
                "exit_code": None,
                "stdout_excerpt": "",
                "stderr_excerpt": f"unknown step_type: {st}",
                "duration_ms": 0,
            }

        step_results.append(res)
        if res["status"] == "success":
            succeeded_any = True
            if step.get("produces"):
                produced.append(step["produces"])

            if step.get("halt") and not auto_continue:
                halted_after = step["id"]
                halt_data = step.get("halt_data") or {}
                user_action_required = step.get("halt_message") or ""
                break
        elif res["status"] == "failed":
            failed_any = True
            skipped_due_to_failure.add(step["id"])

    if halted_after is not None:
        phase_b_status = "halted_for_user"
    elif failed_any and succeeded_any:
        phase_b_status = "partial"
    elif failed_any:
        phase_b_status = "failed"
    else:
        phase_b_status = "success"

    return {
        "schema_version": 1,
        "phase_b_status": phase_b_status,
        "halted_after": halted_after,
        "user_action_required": user_action_required,
        "halt_data": halt_data,
        "steps": step_results,
        "produced_artifacts": produced,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _autodetect_founder_skills_root(runner_path: Path) -> Path:
    # runner lives at <FOUNDER_SKILLS_ROOT>/scripts/phase_b_runner.py
    return runner_path.resolve().parent.parent


def _build_paths(args: argparse.Namespace) -> dict[str, Path]:
    work_dir = Path(args.work_dir).resolve()
    if not work_dir.is_dir():
        raise InvocationError(f"--work-dir does not exist or is not a directory: {work_dir}")
    scripts = Path(args.scripts).resolve()
    if not scripts.is_dir():
        raise InvocationError(f"--scripts does not exist or is not a directory: {scripts}")

    if args.founder_skills_root:
        fsr = Path(args.founder_skills_root).resolve()
    else:
        fsr = _autodetect_founder_skills_root(Path(__file__))
    if not fsr.is_dir():
        raise InvocationError(f"--founder-skills-root does not resolve to a directory: {fsr}")

    shared = Path(args.shared_scripts).resolve() if args.shared_scripts else fsr / "scripts"
    if not shared.is_dir():
        raise InvocationError(f"shared scripts directory missing: {shared}")

    return {
        "$WORK_DIR": work_dir,
        "$SCRIPTS": scripts,
        "$SHARED_SCRIPTS": shared,
        "$FOUNDER_SKILLS_ROOT": fsr,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else "")
    p.add_argument("--manifest", required=True, help="Path to RUN_MANIFEST.json")
    p.add_argument("--work-dir", required=True, help="Absolute path to skill's work directory")
    p.add_argument("--scripts", required=True, help="Absolute path to skill's scripts/ directory")
    p.add_argument(
        "--shared-scripts",
        help="Absolute path to shared scripts dir (defaults to $FOUNDER_SKILLS_ROOT/scripts)",
    )
    p.add_argument(
        "--founder-skills-root",
        help="Absolute path to founder-skills root (defaults to runner's parent.parent)",
    )
    p.add_argument(
        "--run-result",
        help="Path to write RUN_RESULT.json (defaults to $WORK_DIR/RUN_RESULT.json)",
    )
    p.add_argument("--step", help="Run only this step (Local-mode opt-in for FMR)")
    p.add_argument("--resume-after", help="Skip up through this step id (used after halt)")
    p.add_argument(
        "--auto-continue",
        action="store_true",
        help="Treat halt steps as no-ops (testing only)",
    )
    p.add_argument("--dry-run", action="store_true", help="Print plan, don't execute")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    if args.step and args.resume_after:
        _eprint("--step and --resume-after are mutually exclusive")
        return 2

    try:
        paths = _build_paths(args)
        manifest = load_manifest(Path(args.manifest))
    except InvocationError as e:
        _eprint(f"Invocation error: {e}")
        return 2

    try:
        validate_manifest(manifest)
    except ManifestSchemaError as e:
        _eprint(f"Manifest schema error: {e}")
        result = {
            "schema_version": 1,
            "phase_b_status": "failed",
            "halted_after": None,
            "user_action_required": None,
            "halt_data": None,
            "steps": [],
            "produced_artifacts": [],
            "manifest_error": str(e),
        }
        _write_result(args, paths, result)
        return 1

    # Gate 2 — defensive verification of Phase A
    pa_errors = verify_phase_a(paths["$WORK_DIR"], manifest)
    if pa_errors:
        _eprint("Phase A verification failed:")
        for pa_err in pa_errors:
            _eprint(f"  - {pa_err}")
        result = {
            "schema_version": 1,
            "phase_b_status": "failed",
            "halted_after": None,
            "user_action_required": None,
            "halt_data": None,
            "steps": [],
            "produced_artifacts": [],
            "phase_a_errors": pa_errors,
        }
        _write_result(args, paths, result)
        return 1

    try:
        result = execute_pipeline(
            manifest,
            paths,
            resume_after=args.resume_after,
            single_step=args.step,
            auto_continue=args.auto_continue,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
    except InvocationError as e:
        _eprint(f"Invocation error: {e}")
        return 2

    result["manifest"] = str(Path(args.manifest).resolve())
    _write_result(args, paths, result)

    if result["phase_b_status"] in ("success", "halted_for_user"):
        return 0
    return 1


def _write_result(args: argparse.Namespace, paths: dict[str, Path], result: dict[str, Any]) -> None:
    out_path = Path(args.run_result) if args.run_result else paths["$WORK_DIR"] / "RUN_RESULT.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, sort_keys=False)
        f.write("\n")
    if args.verbose:
        _eprint(f"[runner] RUN_RESULT.json written to {out_path}")


# Ensure shutil import isn't pruned by formatters (used by future expansions/tests).
_ = shutil  # noqa: F841


if __name__ == "__main__":
    sys.exit(main())
