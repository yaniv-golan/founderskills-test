# Phase B: invoke the runner (shared SKILL.md include)

This block is included verbatim by every founder-skills SKILL.md after its
skill-specific Phase A and Phase B step list.

---

## Phase B: invoke the runner

Always attempt to invoke the runner via Bash. **Do not probe Bash availability
first** — just try. If Bash is unavailable (Cowork sub-agent dispatch), the
Bash invocation fails; that's the signal to switch to handoff mode.

### Default Local mode (CLI, Cowork main session) — batch invocation

For most skills, run all of Phase B in one runner call:

```bash
python3 "$FOUNDER_SKILLS_ROOT/scripts/phase_b_runner.py" \
  --manifest "$WORK_DIR/RUN_MANIFEST.json" \
  --work-dir "$WORK_DIR" \
  --scripts "$SCRIPTS" \
  --run-result "$WORK_DIR/RUN_RESULT.json"
```

Read `$WORK_DIR/RUN_RESULT.json`. Surface produced artifacts to the user.

- `phase_b_status: success` — present the final artifacts (typically
  `report.json` and `report.html`) to the user.
- `phase_b_status: halted_for_user` — follow the halt protocol below.
- `phase_b_status: partial` or `failed` — surface the failed step's
  stderr to the user. Do not fabricate the missing artifacts.

### Per-step Local mode (opt-in)

If this skill prescribes per-step invocation (currently only
`financial-model-review` for the burn-multiple sanity check), run each
step individually so you can react to its output before the next:

```bash
python3 "$FOUNDER_SKILLS_ROOT/scripts/phase_b_runner.py" \
  --manifest "$WORK_DIR/RUN_MANIFEST.json" \
  --work-dir "$WORK_DIR" \
  --scripts "$SCRIPTS" \
  --step <step_id>
```

The skill's "Phase B steps" section documents which steps have
mid-flow reasoning hooks.

### Handoff mode (Cowork sub-agent dispatch)

If the Bash invocation errors with "tool not available" (or equivalent),
your final message must contain exactly:

```
**REVIEW INCOMPLETE — Phase B required**

Phase A finished. The caller MUST run phase_b_runner.py against the
manifest below or this review is incomplete.

  Manifest: <ABS_WORK_DIR>/RUN_MANIFEST.json

Caller invocation (in a Bash-capable context):
  python3 <FOUNDER_SKILLS_ROOT>/scripts/phase_b_runner.py \
    --manifest <ABS_WORK_DIR>/RUN_MANIFEST.json \
    --work-dir <ABS_WORK_DIR> \
    --scripts <ABS_SCRIPTS_PATH> \
    --run-result <ABS_WORK_DIR>/RUN_RESULT.json

If the runner exits with phase_b_status: halted_for_user, surface the
user_action_required message to the user (or write the requested
artifact yourself if halt_data.consumer == "agent"), then re-invoke
the runner with --resume-after <halted_after>.
```

Substitute the actual absolute paths. Do not narrate, do not summarize,
do not invent script output. The caller will run Phase B.

### Halt protocol (both modes)

When `RUN_RESULT.json` shows `phase_b_status: halted_for_user`:

1. Read `halted_after`, `user_action_required`, and `halt_data`.
2. Identify the consumer:
   - `halt_data.consumer == "human"` — surface the produced artifact
     (e.g., `review.html`) to the user with the message. Wait for them
     to upload the file named in `halt_data.expected_upload_artifact`.
     Save the upload to `$WORK_DIR/<expected_upload_artifact>`.
   - `halt_data.consumer == "agent"` — read upstream artifacts already
     produced by Phase B; write the artifact named in
     `halt_data.expected_artifact` to `$WORK_DIR` via the Write tool.
3. Re-invoke the runner with `--resume-after <halted_after>`.
4. Repeat halt handling until `phase_b_status: success`.
