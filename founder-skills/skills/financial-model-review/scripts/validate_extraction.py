#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Anti-hallucination gate: cross-reference model_data.json against inputs.json.

Usage:
    python validate_extraction.py --inputs inputs.json --model-data model_data.json --pretty
    python validate_extraction.py --inputs inputs.json --model-data model_data.json -o extraction_validation.json

Checks that agent-produced inputs.json values are traceable to the raw
extraction in model_data.json.  High-confidence checks only — skips when
ambiguous.

Output: {"status": "pass"|"warn", "checks": [...], "summary": {...}, "correction_hints": [...]}
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_output(data: str, output_path: str | None) -> None:
    if output_path:
        abs_path = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(data)
        receipt = {"ok": True, "path": abs_path, "bytes": len(data.encode("utf-8"))}
        sys.stdout.write(json.dumps(receipt, separators=(",", ":")) + "\n")
    else:
        sys.stdout.write(data)


def _normalize(s: str) -> str:
    """Lowercase, strip, collapse whitespace."""
    return " ".join(s.lower().split())


def _fuzzy_match(a: str, b: str) -> bool:
    """Check if two company names are similar enough.

    Returns True if one is a substring of the other (after normalization)
    or if the first word matches.
    """
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    # First-word match (e.g. "Acme" vs "Acme Corp Ltd")
    wa, wb = na.split()[0], nb.split()[0]
    return wa == wb


def _close_enough(a: float, b: float, tolerance: float = 0.05) -> bool:
    """Check if two numbers are within *tolerance* of each other."""
    if b == 0:
        return a == 0
    return abs(a - b) / abs(b) <= tolerance


def _all_numeric_values(model_data: dict[str, Any]) -> list[float]:
    """Collect all numeric cell values from model_data sheets."""
    nums: list[float] = []
    for sheet in model_data.get("sheets", []):
        for row in sheet.get("rows", []):
            for cell in row:
                if isinstance(cell, (int, float)) and not isinstance(cell, bool) and math.isfinite(cell):
                    nums.append(float(cell))
    return nums


def _all_string_values(model_data: dict[str, Any], *, include_pre_header: bool = True) -> list[str]:
    """Collect all non-empty string cell values from model_data sheets."""
    strings: list[str] = []
    for sheet in model_data.get("sheets", []):
        for row in sheet.get("rows", []):
            for cell in row:
                if isinstance(cell, str) and cell.strip():
                    strings.append(cell.strip())
        for h in sheet.get("headers", []):
            if isinstance(h, str) and h.strip():
                strings.append(h.strip())
        if include_pre_header:
            for row in sheet.get("pre_header_rows", []):
                for cell in row:
                    if isinstance(cell, str) and cell.strip():
                        strings.append(cell.strip())
    return strings


def _periodicity_multiplier(model_data: dict[str, Any]) -> int:
    """Return the multiplier to convert a period value to monthly.

    quarterly -> 3, annual -> 12, else 1.
    """
    ps = model_data.get("periodicity_summary", "monthly")
    if ps == "quarterly":
        return 3
    if ps == "annual":
        return 12
    return 1


def _find_numeric_in_model(
    target: float,
    model_data: dict[str, Any],
    tolerance: float = 0.05,
    *,
    periodicity_aware: bool = False,
) -> bool:
    """Check if *target* (or a periodicity-scaled variant) appears in model_data."""
    if target == 0:
        return True  # Zero is trivially traceable
    nums = _all_numeric_values(model_data)
    mult = _periodicity_multiplier(model_data) if periodicity_aware else 1

    for n in nums:
        if _close_enough(target, n, tolerance):
            return True
        # Periodicity-aware: monthly value * mult ≈ source value
        if periodicity_aware and mult > 1:
            if _close_enough(target * mult, n, tolerance):
                return True
            if _close_enough(target, n / mult, tolerance):
                return True
            # Also try: target ≈ n * mult (source is monthly, inputs annualized)
            if _close_enough(target, n * mult, tolerance):
                return True
    return False


def _find_cell_ref(
    target: float,
    model_data: dict[str, Any],
    tolerance: float = 0.05,
    *,
    periodicity_aware: bool = False,
) -> dict[str, str] | None:
    """Find the cell reference for a value in model_data cell_refs.

    Returns {"ref": "Sheet!B5", "confidence": "best_match"} or None.
    Uses the list-based cell_refs structure where each entry has
    {row_index, label, cols: {col_header: "B5"}}.
    Mirrors the periodicity scaling logic from _find_numeric_in_model.
    """
    if target == 0:
        return None  # Zero is trivially traceable, no meaningful ref
    mult = _periodicity_multiplier(model_data) if periodicity_aware else 1

    for sheet in model_data.get("sheets", []):
        cell_refs = sheet.get("cell_refs", [])
        if not isinstance(cell_refs, list):
            continue
        rows = sheet.get("rows", [])
        headers = sheet.get("headers", [])
        for ref_entry in cell_refs:
            row_idx = ref_entry.get("row_index", -1)
            if row_idx < 0 or row_idx >= len(rows):
                continue
            row = rows[row_idx]
            cols = ref_entry.get("cols", {})
            for j, val in enumerate(row[1:], 1):
                if not (isinstance(val, (int, float)) and not isinstance(val, bool)):
                    continue
                fval = float(val)
                col_header = headers[j] if j < len(headers) else ""
                coord = cols.get(col_header, "")
                if not coord:
                    continue
                # Direct match
                if _close_enough(target, fval, tolerance):
                    return {"ref": f"{sheet['name']}!{coord}", "confidence": "best_match"}
                # Periodicity-aware matches
                if periodicity_aware and mult > 1:
                    if _close_enough(target * mult, fval, tolerance):
                        return {"ref": f"{sheet['name']}!{coord}", "confidence": "best_match"}
                    if _close_enough(target, fval / mult, tolerance):
                        return {"ref": f"{sheet['name']}!{coord}", "confidence": "best_match"}
                    if _close_enough(target, fval * mult, tolerance):
                        return {"ref": f"{sheet['name']}!{coord}", "confidence": "best_match"}
    return None


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _check_company_name(inputs: dict[str, Any], model_data: dict[str, Any]) -> dict[str, Any]:
    """COMPANY_NAME: fuzzy-match company name against model_data strings."""
    company_name = inputs.get("company", {}).get("company_name", "")
    if not company_name:
        return {
            "id": "COMPANY_NAME",
            "status": "skip",
            "message": "No company name in inputs",
        }

    strings = _all_string_values(model_data)
    candidates: list[str] = []
    for s in strings:
        if _fuzzy_match(company_name, s):
            return {
                "id": "COMPANY_NAME",
                "status": "pass",
                "message": f"Company name '{company_name}' found in model data",
            }
        # Collect potential candidates (strings that look like names — not too long)
        if len(s) > 2 and len(s) < 60 and not any(c.isdigit() for c in s):
            candidates.append(s)

    # De-duplicate candidates, keep first 5
    seen: set[str] = set()
    unique: list[str] = []
    for c in candidates:
        nc = _normalize(c)
        if nc not in seen:
            seen.add(nc)
            unique.append(c)
        if len(unique) >= 5:
            break

    return {
        "id": "COMPANY_NAME",
        "status": "warn",
        "message": f"Company name '{company_name}' not found in model data",
        "candidates": unique,
    }


def _check_salary_traceability(inputs: dict[str, Any], model_data: dict[str, Any]) -> dict[str, Any]:
    """SALARY_TRACEABILITY: check that headcount salary values appear in model_data."""
    headcount = inputs.get("expenses", {}).get("headcount", [])
    if not headcount:
        return {
            "id": "SALARY_TRACEABILITY",
            "status": "skip",
            "message": "No headcount data in inputs",
        }

    untraceable: list[dict[str, Any]] = []
    for entry in headcount:
        salary = entry.get("salary_annual")
        if salary is None or salary == 0:
            continue
        if not _find_numeric_in_model(float(salary), model_data, periodicity_aware=True):
            untraceable.append(
                {
                    "role": entry.get("role", "unknown"),
                    "salary_annual": salary,
                }
            )

    if untraceable:
        return {
            "id": "SALARY_TRACEABILITY",
            "status": "warn",
            "message": f"{len(untraceable)} salary value(s) not traceable to model data",
            "untraceable": untraceable,
        }
    # Collect cell refs for traceable salaries
    source_refs: dict[str, Any] = {}
    for entry in headcount:
        salary = entry.get("salary_annual")
        if salary is not None and salary > 0:
            ref = _find_cell_ref(float(salary), model_data, periodicity_aware=True)
            if ref:
                source_refs[entry.get("role", "unknown")] = ref
    result: dict[str, Any] = {
        "id": "SALARY_TRACEABILITY",
        "status": "pass",
        "message": "All salary values traceable to model data",
    }
    if source_refs:
        result["source_refs"] = source_refs
    return result


def _check_revenue_traceability(inputs: dict[str, Any], model_data: dict[str, Any]) -> dict[str, Any]:
    """REVENUE_TRACEABILITY: check MRR/ARR/monthly totals against model_data."""
    revenue = inputs.get("revenue", {})
    mrr_val = revenue.get("mrr", {}).get("value") if isinstance(revenue.get("mrr"), dict) else None
    arr_val = revenue.get("arr", {}).get("value") if isinstance(revenue.get("arr"), dict) else None
    monthly_total = revenue.get("monthly_total")

    # Collect the latest monthly entry total as well
    monthly_entries = revenue.get("monthly", [])
    latest_monthly = None
    if monthly_entries and isinstance(monthly_entries, list):
        last = monthly_entries[-1]
        if isinstance(last, dict):
            latest_monthly = last.get("total")

    targets = []
    if mrr_val is not None and mrr_val > 0:
        targets.append(("MRR", float(mrr_val)))
    if arr_val is not None and arr_val > 0:
        targets.append(("ARR", float(arr_val)))
    if monthly_total is not None and monthly_total > 0:
        targets.append(("monthly_total", float(monthly_total)))
    if latest_monthly is not None and latest_monthly > 0:
        targets.append(("latest_monthly", float(latest_monthly)))

    if not targets:
        return {
            "id": "REVENUE_TRACEABILITY",
            "status": "skip",
            "message": "No revenue values in inputs",
        }

    untraceable: list[dict[str, Any]] = []
    for label, val in targets:
        if not _find_numeric_in_model(val, model_data, periodicity_aware=True):
            untraceable.append({"field": label, "value": val})

    if untraceable:
        return {
            "id": "REVENUE_TRACEABILITY",
            "status": "warn",
            "message": f"{len(untraceable)} revenue value(s) not traceable to model data",
            "untraceable": untraceable,
        }
    # Collect cell refs for traceable revenue values
    source_refs: dict[str, Any] = {}
    for label, val in targets:
        ref = _find_cell_ref(val, model_data, periodicity_aware=True)
        if ref:
            source_refs[label] = ref
    result: dict[str, Any] = {
        "id": "REVENUE_TRACEABILITY",
        "status": "pass",
        "message": "Revenue values traceable to model data",
    }
    if source_refs:
        result["source_refs"] = source_refs
    return result


def _check_cash_balance(inputs: dict[str, Any], model_data: dict[str, Any]) -> dict[str, Any]:
    """CASH_BALANCE: check that cash balance (stock metric) appears in model_data."""
    cash_val = inputs.get("cash", {}).get("current_balance")
    if cash_val is None or cash_val == 0:
        return {
            "id": "CASH_BALANCE",
            "status": "skip",
            "message": "No cash balance in inputs",
        }

    # Cash balance is a stock metric — no periodicity scaling
    if _find_numeric_in_model(float(cash_val), model_data, periodicity_aware=False):
        ref = _find_cell_ref(float(cash_val), model_data, periodicity_aware=False)
        result: dict[str, Any] = {
            "id": "CASH_BALANCE",
            "status": "pass",
            "message": f"Cash balance {cash_val} found in model data",
        }
        if ref:
            result["source_refs"] = {"current_balance": ref}
        return result
    return {
        "id": "CASH_BALANCE",
        "status": "warn",
        "message": f"Cash balance {cash_val} not found in model data",
    }


def _detect_scale_indicator(model_data: dict[str, Any]) -> str | None:
    """Look for explicit scale indicators like ($000), (in thousands), etc."""
    patterns = ["($000)", "(in thousands)", "($k)", "(in $k)", "(000s)", "($m)", "(in millions)", "(in $m)"]
    for sheet in model_data.get("sheets", []):
        for h in sheet.get("headers", []):
            if isinstance(h, str):
                for p in patterns:
                    if p in h.lower():
                        return p
        for row in sheet.get("pre_header_rows", []):
            for cell in row:
                if isinstance(cell, str):
                    for p in patterns:
                        if p in cell.lower():
                            return p
        # Check first 3 data rows too
        for row in sheet.get("rows", [])[:3]:
            for cell in row:
                if isinstance(cell, str):
                    for p in patterns:
                        if p in cell.lower():
                            return p
    return None


# Stage-based plausibility ranges for monthly values (USD)
_STAGE_RANGES: dict[str, dict[str, tuple[float, float]]] = {
    "pre-seed": {"cash_balance": (10_000, 50_000_000), "monthly_burn": (1_000, 500_000)},
    "seed": {"cash_balance": (50_000, 100_000_000), "monthly_burn": (5_000, 2_000_000)},
    "series-a": {"cash_balance": (500_000, 500_000_000), "monthly_burn": (50_000, 10_000_000)},
    "series-b": {"cash_balance": (1_000_000, 1_000_000_000), "monthly_burn": (100_000, 50_000_000)},
}


def _check_scale_plausibility(inputs: dict[str, Any], model_data: dict[str, Any]) -> dict[str, Any]:
    """SCALE_PLAUSIBILITY: detect when monetary values look like they're still in thousands/millions."""
    stage = inputs.get("company", {}).get("stage", "seed")
    cash = inputs.get("cash", {}).get("current_balance")
    burn = inputs.get("cash", {}).get("monthly_net_burn")
    headcount_entries = inputs.get("expenses", {}).get("headcount", [])
    total_headcount = sum(h.get("count", 0) for h in headcount_entries if isinstance(h, dict))

    signals: list[str] = []

    # Check for explicit scale indicator in model
    scale_indicator = _detect_scale_indicator(model_data)
    if scale_indicator:
        signals.append(f"Model contains scale indicator: {scale_indicator}")

    # Cash balance sanity
    ranges = _STAGE_RANGES.get(stage, _STAGE_RANGES["seed"])
    if cash is not None and cash > 0:
        low, _high = ranges["cash_balance"]
        if cash < low:
            signals.append(f"Cash balance ${cash:,.0f} implausibly low for {stage} stage (min ~${low:,.0f})")

    # Burn sanity — if we have headcount, burn should cover at least basic salaries
    if burn is not None and burn > 0 and total_headcount > 0:
        # Minimum plausible monthly burn: $2K/person (even for low-cost geographies)
        min_burn = total_headcount * 2_000
        if burn < min_burn:
            signals.append(
                f"Monthly burn ${burn:,.0f} too low for {total_headcount} employees "
                f"(implies ${burn / total_headcount:,.0f}/person/month)"
            )

    # Salary sanity — check if any salary is implausibly low
    for h in headcount_entries:
        salary = h.get("salary_annual", 0)
        if salary and salary > 0 and salary < 10_000:
            signals.append(
                f"Annual salary ${salary:,.0f} for '{h.get('role', '?')}' implausibly low — "
                f"model may be in thousands (${salary * 1000:,.0f}?)"
            )

    if signals:
        return {
            "id": "SCALE_PLAUSIBILITY",
            "status": "warn",
            "message": "Values may not be converted from model denomination (thousands/millions)",
            "signals": signals,
        }
    return {
        "id": "SCALE_PLAUSIBILITY",
        "status": "pass",
        "message": "Monetary values appear plausible for stated stage",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _should_skip(model_data: dict[str, Any] | None, inputs: dict[str, Any]) -> str | None:
    """Return a skip reason string, or None if checks should run."""
    if model_data is None:
        return "model_data file missing or unreadable"
    sheets = model_data.get("sheets", [])
    if not sheets:
        return "model_data has no sheets"
    # Stub check
    if model_data.get("skipped"):
        return "model_data is a stub"
    # Conversational / deck — no extraction to validate
    model_format = inputs.get("company", {}).get("model_format", "")
    if model_format in ("conversational", "deck"):
        return f"model_format is '{model_format}' — no extraction to validate"
    return None


def validate(inputs: dict[str, Any], model_data: dict[str, Any] | None) -> dict[str, Any]:
    """Run all extraction validation checks."""
    skip_reason = _should_skip(model_data, inputs)
    if skip_reason:
        return {
            "status": "skip",
            "checks": [],
            "summary": {"skip_reason": skip_reason},
            "correction_hints": [],
        }

    assert model_data is not None
    checks = [
        _check_company_name(inputs, model_data),
        _check_salary_traceability(inputs, model_data),
        _check_revenue_traceability(inputs, model_data),
        _check_cash_balance(inputs, model_data),
        _check_scale_plausibility(inputs, model_data),
    ]

    warnings = [c for c in checks if c["status"] == "warn"]
    status = "warn" if warnings else "pass"

    correction_hints: list[str] = []
    for w in warnings:
        cid = w["id"]
        if cid == "COMPANY_NAME" and w.get("candidates"):
            correction_hints.append(f"Company name mismatch — candidates from model: {', '.join(w['candidates'][:3])}")
        elif cid == "SALARY_TRACEABILITY":
            roles = [u["role"] for u in w.get("untraceable", [])]
            correction_hints.append(
                f"Salary values for [{', '.join(roles)}] not found in spreadsheet — may be hallucinated"
            )
        elif cid == "REVENUE_TRACEABILITY":
            fields = [u["field"] for u in w.get("untraceable", [])]
            correction_hints.append(
                f"Revenue values [{', '.join(fields)}] not found in spreadsheet — verify against source"
            )
        elif cid == "CASH_BALANCE":
            correction_hints.append(f"Cash balance {w.get('message', '')} — verify against source")
        elif cid == "SCALE_PLAUSIBILITY":
            sigs = w.get("signals", [])
            correction_hints.append(
                "Model may be denominated in thousands/millions — multiply monetary values by 1000 or 1000000. "
                + "; ".join(sigs[:3])
            )

    return {
        "status": status,
        "checks": checks,
        "summary": {
            "total": len(checks),
            "pass": sum(1 for c in checks if c["status"] == "pass"),
            "warn": sum(1 for c in checks if c["status"] == "warn"),
            "skip": sum(1 for c in checks if c["status"] == "skip"),
        },
        "correction_hints": correction_hints,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate extraction: cross-reference model_data vs inputs")
    parser.add_argument("--inputs", required=True, help="Path to inputs.json")
    parser.add_argument("--model-data", required=True, help="Path to model_data.json")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument("-o", "--output", help="Write output to file instead of stdout")
    args = parser.parse_args()

    # Read inputs
    try:
        with open(args.inputs) as f:
            inputs = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error reading inputs: {e}", file=sys.stderr)
        sys.exit(1)

    # Read model_data (missing is OK — returns skip)
    model_data: dict[str, Any] | None = None
    try:
        with open(args.model_data) as f:
            model_data = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass  # Will be handled as skip

    result = validate(inputs, model_data)
    indent = 2 if args.pretty else None
    out = json.dumps(result, indent=indent) + "\n"
    _write_output(out, args.output)


if __name__ == "__main__":
    main()
