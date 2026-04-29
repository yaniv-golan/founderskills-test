# RUN_MANIFEST.json schema (v1)

The handoff contract between Phase A (sub-agent-safe data collection) and
Phase B (Bash-required computation + composition) for founder-skills.

**Machine-readable form**: [`run_manifest_schema.json`](./run_manifest_schema.json) (JSON Schema draft-07).

---

## Top-level fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `schema_version` | int | yes | Currently `1`. Runner validates. |
| `skill` | string | yes | Skill identifier (e.g., `"deck-review"`). |
| `plugin_version` | string | yes | Plugin version that wrote this manifest. |
| `run_id` | string | yes | Unique per run; matches founder-context conventions. |
| `phase_a_complete` | bool | yes | If `false`, runner refuses to run Phase B and surfaces the reason via the side-blocks below. |
| `phase_a_artifacts[]` | array | yes | Each entry: `{path, required}`. Paths relative to `$WORK_DIR`. |
| `phase_a_missing[]` | array | yes | Strings naming artifacts the sub-agent expected but did not produce. Empty when `phase_a_complete: true`. |
| `step_0_required` | object | optional | Present when `phase_a_complete: false` due to missing pre-extraction (FMR xlsx case). See below. |
| `cross_skill_import_required` | object | optional | Present when `phase_a_complete: false` because a cross-skill import wasn't supplied in dispatch. See below. |
| `phase_b_pending` | bool | yes | Always `true` in v0.4.0. |
| `phase_b_steps[]` | array | yes | Per-step descriptors (see below). |

## Step descriptor

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Unique within manifest. Lower-snake-case (`^[a-z][a-z0-9_]*$`). |
| `step_type` | enum | optional, default `"subprocess"` | One of `subprocess`, `rename`, `noop-halt`. |
| `cmd[]` | array | required for `subprocess` | Argv. Placeholders expanded by runner: `$WORK_DIR`, `$SCRIPTS`, `$SHARED_SCRIPTS`, `$FOUNDER_SKILLS_ROOT`. |
| `stdin_from` | string | optional, `subprocess` only | Relative-to-`$WORK_DIR` path of file piped to subprocess stdin. |
| `from_path`, `to_path` | string, string | required for `rename` | Relative-to-`$WORK_DIR`. Runner does `os.replace(from, to)`. |
| `halt` | bool | optional, default `false` | If `true` and step succeeds, runner exits with status `halted_for_user`. |
| `halt_message` | string | optional | Free-text instruction for the caller to surface to the consumer. |
| `halt_data` | object | optional | Structured halt info. Recognized fields: `consumer` (`"human"` or `"agent"`), `expected_upload_artifact` (human consumer), `expected_artifact` (agent consumer). |
| `produces` | string | optional | Expected output artifact path (relative to `$WORK_DIR`). Diagnostic only — runner does not enforce. |
| `depends_on[]` | array | optional, default `[]` | Step IDs that must complete before this one runs. Cycle-checked. |
| `timeout_seconds` | int | optional, default `300` | Per-step subprocess timeout. |

## Step types

### `subprocess`

Runs `cmd[]` as a child process. If `stdin_from` is set, the named file's
contents are piped to the subprocess's stdin. Captures stdout, stderr,
exit code, duration. Non-zero exit = failure.

### `rename`

Filesystem move via `os.replace($WORK_DIR/from_path, $WORK_DIR/to_path)`.
Used for the `corrected_inputs.json → inputs.json` and similar patterns
in FMR. No subprocess.

### `noop-halt`

No command, no filesystem op. Used purely as a halt-point marker. Set
`halt: true` and the runner exits with `halted_for_user` after marking
the step as succeeded. Used when the consumer needs to write an artifact
based on prior step outputs (e.g., FMR's `commentary.json` after
`visualize` produces `report.html`).

## Halt protocol

When a step has `halt: true` and runs successfully, the runner exits
immediately with `phase_b_status: halted_for_user`. The caller:

1. Reads `RUN_RESULT.json`. Sees `halted_after: <step_id>`,
   `user_action_required: <halt_message>`, `halt_data: {consumer, ...}`.
2. Acts:
   - **`consumer: "human"`**: surfaces produced artifact (e.g., `review.html`)
     to the user with the message. Waits for upload. Saves the upload to
     `$WORK_DIR/<halt_data.expected_upload_artifact>`.
   - **`consumer: "agent"`**: caller (or in Local mode the agent itself)
     reads upstream artifacts already produced, writes the named artifact
     (`halt_data.expected_artifact`) to `$WORK_DIR` via Write.
3. Re-invokes runner with `--resume-after <step_id>`. Runner picks up at
   the next step in dependency order.

## Side-blocks for `phase_a_complete: false`

### `step_0_required` (currently only FMR with xlsx input)

```json
"step_0_required": {
  "reason": "Financial model file provided but not yet extracted",
  "cmd": [
    "python3",
    "$FOUNDER_SKILLS_ROOT/skills/financial-model-review/scripts/extract_model.py",
    "--file", "<source_xlsx>",
    "-o", "$WORK_DIR/model_data.json",
    "--pretty"
  ],
  "placeholders": {
    "<source_xlsx>": "Absolute path to the .xlsx or .csv file the user provided"
  },
  "next_step": "After extraction completes, re-dispatch financial-model-review against this $WORK_DIR"
}
```

### `cross_skill_import_required`

```json
"cross_skill_import_required": {
  "import_id": "competitive_positioning_landscape",
  "reason": "Deck review imports competitor landscape for cross-validation",
  "find_command": [
    "python3", "$SHARED_SCRIPTS/find_artifact.py",
    "--skill", "competitive-positioning",
    "--artifact", "landscape.json",
    "--slug", "<slug>"
  ],
  "placeholders": {
    "<slug>": "Company slug from founder context"
  },
  "next_step": "Run find_artifact and pass the resolved path in the dispatch prompt as 'competitive_positioning_landscape = <absolute_path>'"
}
```

## Worked example: deck-review

```json
{
  "schema_version": 1,
  "skill": "deck-review",
  "plugin_version": "0.4.0",
  "run_id": "2026-04-29-1234",
  "phase_a_complete": true,
  "phase_a_artifacts": [
    {"path": "deck_inventory.json", "required": true},
    {"path": "stage_profile.json", "required": true},
    {"path": "slide_reviews.json", "required": true},
    {"path": "checklist_input.json", "required": true}
  ],
  "phase_a_missing": [],
  "phase_b_pending": true,
  "phase_b_steps": [
    {
      "id": "validate_checklist",
      "step_type": "subprocess",
      "cmd": ["python3", "$SCRIPTS/checklist.py", "-o", "$WORK_DIR/checklist.json", "--pretty"],
      "stdin_from": "checklist_input.json",
      "produces": "checklist.json"
    },
    {
      "id": "compose",
      "step_type": "subprocess",
      "cmd": ["python3", "$SCRIPTS/compose_report.py", "--dir", "$WORK_DIR", "-o", "$WORK_DIR/report.json", "--pretty"],
      "produces": "report.json",
      "depends_on": ["validate_checklist"]
    },
    {
      "id": "visualize",
      "step_type": "subprocess",
      "cmd": ["python3", "$SCRIPTS/visualize.py", "--dir", "$WORK_DIR", "-o", "$WORK_DIR/report.html"],
      "produces": "report.html",
      "depends_on": ["compose"]
    }
  ]
}
```
