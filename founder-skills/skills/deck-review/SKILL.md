---
name: deck-review
disable-model-invocation: true
description: "Scores and strengthens startup pitch decks (pre-seed through Series A) against 35 investor-grade criteria grounded in Sequoia, DocSend, YC, a16z, and Carta data."
compatibility: Requires Python 3.10+ and uv for script execution.
metadata:
  author: lool-ventures
  version: "0.4.0"
exports:
  - "checklist.json -> financial-model-review, ic-sim, fundraise-readiness"
---

# Deck Review Skill

Help startup founders strengthen their pitch decks before sending them to investors. Produce a structured, scored review with specific, actionable recommendations grounded in current best practices from Sequoia, DocSend, YC, a16z, and Carta data. The tone is founder-first: a candid coaching session, not a VC evaluation.

## Input Formats

Accept any format: PDF, PowerPoint (PPTX), markdown, or text descriptions of slides.

## Architecture (v0.4.0)

This skill follows the Phase 0 / Phase A / Phase B handoff architecture so it works in both Cowork sub-agent dispatch (where sub-agents have no Bash) and Cowork main-session / CLI (where Bash is available):

- **Phase 0 — Setup** (caller's main session in handoff mode; inline in Local mode): create `$REVIEW_DIR`, run `founder_context.py`, resolve cross-skill imports.
- **Phase A — Data collection** (sub-agent in handoff mode; inline in Local mode): read deck, produce four JSON artifacts via the Write tool, emit `RUN_MANIFEST.json`. **No scripts in Phase A.**
- **Phase B — Computation + composition** (caller in handoff mode; inline in Local mode): runs the manifest's steps (`checklist.py` → `compose_report.py` → `visualize.py`) via `phase_b_runner.py`.

## Available Scripts

All scripts are at `${CLAUDE_PLUGIN_ROOT}/skills/deck-review/scripts/`:

- **`checklist.py`** — Validates and aggregates the 35-criteria scoring (Phase B)
- **`compose_report.py`** — Assembles artifacts into final report; `--strict` exits 1 on high/medium warnings (Phase B)
- **`visualize.py`** — Generates self-contained HTML with SVG charts (Phase B)

Shared scripts at `${CLAUDE_PLUGIN_ROOT}/scripts/`:

- **`founder_context.py`** — Per-company context (init/read/merge/validate) (Phase 0)
- **`find_artifact.py`** — Cross-skill artifact resolution (Phase 0)
- **`phase_b_runner.py`** — Phase B pipeline runner

## Available References

Read as needed from `${CLAUDE_PLUGIN_ROOT}/skills/deck-review/references/`:

- **`deck-best-practices.md`** — Slide frameworks, stage guidelines, design rules, AI-company requirements
- **`checklist-criteria.md`** — All 35 criteria with pass/fail/warn thresholds
- **`artifact-schemas.md`** — JSON schemas for all artifacts

Shared schema at `${CLAUDE_PLUGIN_ROOT}/skills/_shared/`:

- **`run_manifest_schema.md`** — RUN_MANIFEST.json contract

## Phase A artifacts

| Artifact | Required | Producer | Notes |
|---|---|---|---|
| `deck_inventory.json` | yes | agent (Write) | Slide-by-slide extraction |
| `stage_profile.json` | yes | agent (Write) | Stage detection + AI flag |
| `slide_reviews.json` | yes | agent (Write) | Per-slide critique |
| `checklist_input.json` | yes | agent (Write) | 35-criteria scoring as heredoc payload for `checklist.py` |
| `competitive_positioning_landscape.json` | optional | cross-skill import | Caller resolves and supplies path in handoff mode; agent runs `find_artifact.py` in Local mode |

Keep the founder informed with brief, plain-language updates at each step. Never mention file names, scripts, or JSON. After each analytical step, share a one-sentence finding before moving on.

---

## Phase 0: Setup

### Local mode (CLI, Cowork main session)

```bash
SCRIPTS="$CLAUDE_PLUGIN_ROOT/skills/deck-review/scripts"
REFS="$CLAUDE_PLUGIN_ROOT/skills/deck-review/references"
SHARED_SCRIPTS="$CLAUDE_PLUGIN_ROOT/scripts"
FOUNDER_SKILLS_ROOT="$CLAUDE_PLUGIN_ROOT"
if ls "$(pwd)"/mnt/*/ >/dev/null 2>&1; then
  ARTIFACTS_ROOT="$(ls -d "$(pwd)"/mnt/*/ | head -1)artifacts"
elif ls "$(pwd)"/sessions/*/mnt/*/ >/dev/null 2>&1; then
  ARTIFACTS_ROOT="$(ls -d "$(pwd)"/sessions/*/mnt/*/ | head -1)artifacts"
else
  ARTIFACTS_ROOT="$(pwd)/artifacts"
fi
```

If `CLAUDE_PLUGIN_ROOT` is empty, fall back: `Glob` for `**/founder-skills/skills/deck-review/scripts/checklist.py`, strip to get `SCRIPTS`, derive `REFS`, `SHARED_SCRIPTS`, `FOUNDER_SKILLS_ROOT`.

**If `ARTIFACTS_ROOT` resolves to `$(pwd)/artifacts` but no `artifacts/` directory exists at `$(pwd)`:** The workspace may not be mounted yet. Use `Glob` with pattern `**/artifacts/founder_context.json` to locate existing artifacts, and derive `ARTIFACTS_ROOT` from the result. If nothing is found, `mkdir -p "$ARTIFACTS_ROOT"` and proceed.

#### Step 0.1: Read or Create Founder Context

```bash
python3 "$SHARED_SCRIPTS/founder_context.py" read --artifacts-root "$ARTIFACTS_ROOT" --pretty
```

**Exit 0 (found):** Use the company slug and pre-filled fields. Proceed to Step 0.2.

**Exit 1 (not found):** First run — not an error. Use `AskUserQuestion` to ask for company name, stage, sector, and geography. Then create:

```bash
python3 "$SHARED_SCRIPTS/founder_context.py" init \
  --company-name "Acme Corp" --stage seed --sector "B2B SaaS" \
  --geography "US" --artifacts-root "$ARTIFACTS_ROOT"
```

**Exit 2 (multiple):** Present the list, ask which company, re-read with `--slug`.

#### Step 0.2: Set up the review directory

```bash
WORK_DIR="$ARTIFACTS_ROOT/deck-review-${SLUG}"
mkdir -p "$WORK_DIR"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
```

Every artifact written to `$WORK_DIR` must include `"metadata": {"run_id": "$RUN_ID"}` at the top level. `compose_report.py` checks that all artifact run IDs match — a mismatch triggers a `STALE_ARTIFACT` high-severity warning, blocking under `--strict`.

If `WORK_DIR` already contains artifacts from a previous run, remove them before starting:

    rm -f "$WORK_DIR"/{deck_inventory,stage_profile,slide_reviews,checklist_input,checklist,report,RUN_MANIFEST,RUN_RESULT}.json "$WORK_DIR"/report.{html,md}

In Cowork, file deletion may require explicit permission. If cleanup fails with "Operation not permitted", request delete permission and retry before proceeding.

### Handoff mode (Cowork sub-agent dispatch)

The **caller** (user's main session) runs Phase 0 *before* dispatching this sub-agent. Sub-agents in Cowork have no Bash, so the steps above cannot run inside Phase A.

The caller passes the following in the dispatch prompt, which the sub-agent reads at Phase A entry:

- `WORK_DIR` — absolute path
- `RUN_ID` — string per founder-context conventions
- Founder context — either inline JSON or a path to `$WORK_DIR/founder_context.json`
- Cross-skill imports — absolute paths to artifacts the consumer may need (e.g., `competitive_positioning_landscape = /abs/path/to/landscape.json`)

If the dispatch prompt is missing required Phase 0 outputs, the sub-agent's Phase A emits `RUN_MANIFEST.json` with `phase_a_complete: false` and either `phase_a_missing` or `cross_skill_import_required` populated, then stops.

---

## Phase A: data collection (judgment-only, no scripts)

### Step A1: Ingest Deck → `deck_inventory.json`

**Ingestion pitfalls — common issues that degrade review quality:**

1. **PDF image-only slides:** Some PDFs embed slides as images with no extractable text. If Read returns blank or garbled content, note `input_quality: "image_only"` in `deck_inventory.json` and base the review on visual description + OCR-level best effort. Flag reduced confidence in coaching commentary.
2. **PPTX speaker notes vs. slide content:** Speaker notes often contain the real narrative; slide text is abbreviated. Extract both — notes go into `content_summary`, slide text into `headline`. Do not discard notes.
3. **Multi-file submissions:** Founder sends v1 + v2, or deck + appendix as separate files. Ask which is the primary deck before proceeding. Do not merge or review both simultaneously.
4. **Partial decks:** Deck has fewer than 5 slides or is clearly a subset. Proceed but set `confidence: "low"` in stage_profile and note the limitation. Missing-slides detection still runs normally.
5. **Wrong file type:** File named `.pdf` but is actually a Word doc or image. If Read fails, try alternate format before asking the founder for a re-upload.

Read the provided deck. For each slide, extract: headline, content summary, visuals description, word count estimate. Record metadata: company name, total slides, format, any claimed stage or raise amount.

Write the result to `$WORK_DIR/deck_inventory.json` via the Write tool. Consult `references/artifact-schemas.md` for the schema.

### Step A2: Detect Stage → `stage_profile.json`

Determine pre-seed/seed/series-a from signals in the deck. Read `references/deck-best-practices.md` for stage-specific frameworks. Record: detected stage, confidence, evidence, whether AI company, expected slide framework, stage benchmarks.

**Stage signals:** Pre-seed: no revenue, LOIs/waitlist, prototype, <$2.5M ask. Seed: early ARR, paying customers, <$6M ask. Series A: $1M+ ARR, cohort data, repeatable GTM, $10M+ ask. Later-stage: set detected_stage to `"series_b"` or `"growth"` — use the Gate below. Do not ask outside the gate.

**AI company detection signals:** The company is AI-first if ANY of: (1) core product uses ML/AI for its primary value proposition, (2) inference or training costs appear in COGS or margins, (3) deck mentions foundation models, fine-tuning, or AI infrastructure as product components, (4) retention or engagement metrics reference AI-specific patterns (usage retention vs. seat retention). Set `is_ai_company: true` and record the evidence in `ai_evidence`. When in doubt, flag it — the gate will let the founder correct.

Write the result to `$WORK_DIR/stage_profile.json` via the Write tool.

### Gate: Confirm Stage and Scope

**MANDATORY STOP — TWO SEPARATE STEPS. DO NOT COMBINE THEM.**

**Step A: Output a chat message** with the stage detection results. Use a formatted summary. This is a normal assistant message — NOT an AskUserQuestion call. Example:

```
Based on the deck, here's what I'm seeing:

- **Detected stage:** Seed
- **Confidence:** High
- **Key evidence:** $4.2M ARR, 3 paying enterprise customers, $5M raise ask
- **AI company:** Yes — inference costs in COGS
- **Expected framework:** Sequoia seed (12-15 slides)
- **Slides in deck:** 14
```

**If `detected_stage` is `pre_seed`, `seed`, or `series_a`:**

**Step B: AFTER the chat message, call `AskUserQuestion`** with ONLY a short question. The question field is plain text — NO markdown, NO tables, NO bullet points.

Question: `Does this stage detection look right?`
Options: `Looks right` / `Different stage` / `Not sure — proceed anyway`

**If `detected_stage` is `"series_b"` or `"growth"`:** include a note in Step A: "This skill is calibrated only for pre-seed through Series A, so this deck is currently out of scope."

Then call `AskUserQuestion` with:
Question: `This looks out of scope for this skill. What should I do?`
Options: `Stop review` / `Different stage` / `Proceed anyway (best-effort)`

**CRITICAL: The AskUserQuestion question must be ONE SHORT SENTENCE. Put ALL details in the chat message (Step A), not in the question.**

This two-step pattern (chat message then AskUserQuestion) is required because AskUserQuestion renders as plain text. Detailed content goes in the chat message; only the gate question goes in AskUserQuestion.

**If the founder selects "Looks right":** Proceed to Step A4 with the detected stage.

**If the founder selects "Different stage":** Ask which stage they want to use (pre-seed / seed / series-a / series-b / growth). Then rebuild `stage_profile.json` for the corrected stage (do not re-read the deck or re-detect signals — the deck evidence is unchanged): re-read `references/deck-best-practices.md` for the new stage's framework and benchmarks, rebuild the artifact, and repeat the gate.

For the rebuilt `stage_profile.json`:
- `detected_stage`: the founder's corrected value
- `confidence`: `"high"` (founder confirmed directly)
- `evidence`: include original detection signals plus `"Founder corrected stage from X to Y"`
- `is_ai_company`, `ai_evidence`: unchanged from original detection unless the founder also corrected those inputs
- `expected_framework`, `stage_benchmarks`: rebuild from `deck-best-practices.md` only if the corrected stage is `pre_seed`, `seed`, or `series_a`

**If the corrected stage is still `"series_b"` or `"growth"`:** stop after the gate. Do not continue. Do **not** use Series A as a proxy for later-stage companies.

**If the founder selects "Stop review":** stop the skill here.

**If "Proceed anyway (best-effort)":** Use Series A criteria as the closest available proxy. Set `confidence: "low"` in `stage_profile.json` and add `"Founder chose best-effort review for out-of-scope stage"` to evidence. Include a prominent disclaimer in the final coaching commentary that scoring criteria are calibrated for pre-seed through Series A and may not reflect later-stage investor expectations.

**If "Not sure — proceed anyway":** Use the detected stage but note the uncertainty in `stage_profile.json` under `confidence: "low"`. Mention in the final coaching commentary that the founder should confirm their stage positioning.

### Step A4: Review Each Slide → `slide_reviews.json`

Compare each slide against the stage-specific framework and non-negotiable principles. For each slide: identify strengths, weaknesses, and specific recommendations. Map to expected framework. Flag missing expected slides.

**Critical:** Every critique must cite a specific best-practice principle. No vague feedback.

Write the result to `$WORK_DIR/slide_reviews.json` via the Write tool.

### Step A5: Score Checklist → `checklist_input.json`

Evaluate all 35 criteria from `references/checklist-criteria.md`. For non-AI companies, mark AI category items as `not_applicable`.

Write the agent-judgment payload to `$WORK_DIR/checklist_input.json` via the Write tool. The schema is the same heredoc payload `checklist.py` previously consumed:

```json
{
  "items": [
    {"id": "purpose_clear", "status": "pass", "evidence": "Sequoia: single declarative sentence", "notes": "Clear one-liner with quantified outcome"},
    ...all 35 items...
  ]
}
```

`checklist.py` runs in Phase B and reads this file via stdin (the runner pipes it).

**Evidence quality rules:**
- Every `fail` and `warn` MUST cite a specific best-practice principle or benchmark (e.g., "Sequoia: single declarative sentence" or "DocSend: median 11 slides for seed").
- Every `pass` MUST note what was checked — not just "pass" with empty evidence.
- `not_applicable` items MUST include a reason (e.g., "Not an AI company — AI category gated").
- Empty `evidence` on any non-pass item = quality gate failure. Fix before writing the manifest.

### Step A-final: Verify and write `RUN_MANIFEST.json`

Verify each required Phase A artifact exists at `$WORK_DIR` (use Glob). Then write `$WORK_DIR/RUN_MANIFEST.json`:

```json
{
  "schema_version": 1,
  "skill": "deck-review",
  "plugin_version": "0.4.0",
  "run_id": "<RUN_ID>",
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

If any required artifact is missing, write the manifest with `phase_a_complete: false` and `phase_a_missing` populated, then stop.

---

## Phase B: invoke the runner

(See `${CLAUDE_PLUGIN_ROOT}/skills/_shared/phase_b_template.md` for the full template — included by reference here.)

**Default invocation (batch, Local mode):**

```bash
python3 "$FOUNDER_SKILLS_ROOT/scripts/phase_b_runner.py" \
  --manifest "$WORK_DIR/RUN_MANIFEST.json" \
  --work-dir "$WORK_DIR" \
  --scripts "$SCRIPTS" \
  --run-result "$WORK_DIR/RUN_RESULT.json"
```

Read `$WORK_DIR/RUN_RESULT.json`:

- `phase_b_status: success` — proceed to Step Final.
- `phase_b_status: partial` or `failed` — surface the failed step's stderr to the user. Do not fabricate the missing pieces.

**If the Bash invocation fails** (Cowork sub-agent dispatch — Bash filtered): final message must be exactly:

```
**REVIEW INCOMPLETE — Phase B required**

Phase A finished. The caller MUST run phase_b_runner.py against the
manifest below or this review is incomplete.

  Manifest: <ABS_WORK_DIR>/RUN_MANIFEST.json

Caller invocation:
  python3 <FOUNDER_SKILLS_ROOT>/scripts/phase_b_runner.py \
    --manifest <ABS_WORK_DIR>/RUN_MANIFEST.json \
    --work-dir <ABS_WORK_DIR> \
    --scripts <ABS_SCRIPTS_PATH> \
    --run-result <ABS_WORK_DIR>/RUN_RESULT.json
```

Substitute the actual absolute paths.

---

## Step Final: Deliver Artifacts

After Phase B succeeds:

1. Read `report_markdown` from `$WORK_DIR/report.json`, write it to `$WORK_DIR/report.md`, and display it to the user in full.
2. Present the file paths: `report.md`, `report.html` (if generated).
3. Add coaching commentary.
4. Optionally copy to workspace root: `{Company}_Deck_Review.md`, `.html`, `.json`.

## Gotchas

- **"Looks polished" bias:** A well-designed deck is not a strong deck. Score content, narrative, and evidence independently of visual quality. The checklist separates design (5 items) from content (8 items) for this reason.
- **Template / AI-generated copy:** If multiple slides use generic phrasing ("revolutionize," "disrupt," "world-class team") with no specifics, flag this in coaching commentary as a credibility risk — investors notice formulaic decks. This is not a checklist item but affects overall narrative assessment.
- **Benchmarks are medians, not gates:** A $3M seed round in a $1B TAM market is not automatically wrong — context matters. Use benchmarks from `deck-best-practices.md` as reference points, not hard pass/fail thresholds. The coaching commentary should explain deviations rather than penalize them.
- **Founder provided text, not a file:** When the founder describes slides in conversation rather than uploading a file, adapt: write `deck_inventory.json` from the conversation, set `input_format: "text"`, and note reduced confidence in visual/design assessments. Design category items become `not_applicable` unless the founder shares screenshots.
- **Cross-skill context:** If `founder_context.py` returned prior market-sizing or financial-model-review runs, mention relevant findings in coaching commentary (e.g., "Your market sizing calculated $X TAM — your deck claims $Y"). Do not hard-fail on discrepancies; flag them for the founder.

## Scoring

- Each of 35 items: pass / fail / warn / not_applicable
- `score_pct` = pass / (total - not_applicable) x 100
- Overall: "strong" (>=85%), "solid" (>=70%), "needs_work" (>=50%), "major_revision" (<50%)
