#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Tests for validate_extraction.py — anti-hallucination gate.

Run:  pytest founder-skills/tests/test_validate_extraction.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FMR_SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "skills", "financial-model-review", "scripts")


def _run(
    inputs: dict[str, Any] | None,
    model_data: dict[str, Any] | None,
    extra_args: list[str] | None = None,
) -> tuple[int, dict[str, Any], str]:
    """Write temp files, run validate_extraction.py, return (exit_code, json, stderr)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        inputs_path = os.path.join(tmpdir, "inputs.json")
        model_data_path = os.path.join(tmpdir, "model_data.json")

        if inputs is not None:
            with open(inputs_path, "w") as f:
                json.dump(inputs, f)
        if model_data is not None:
            with open(model_data_path, "w") as f:
                json.dump(model_data, f)

        cmd = [
            sys.executable,
            os.path.join(FMR_SCRIPTS_DIR, "validate_extraction.py"),
            "--inputs", inputs_path,
            "--model-data", model_data_path,
            "--pretty",
        ]
        if extra_args:
            cmd.extend(extra_args)
        result = subprocess.run(cmd, capture_output=True, text=True)
        try:
            data = json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError:
            data = {}
        return result.returncode, data, result.stderr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MODEL_DATA: dict[str, Any] = {
    "sheets": [
        {
            "name": "P&L",
            "headers": ["Line Item", "Jan 2025", "Feb 2025", "Mar 2025"],
            "rows": [
                ["Revenue", 50000, 55000, 60000],
                ["Payroll - R&D", 120000, 120000, 120000],
                ["Payroll - S&M", 80000, 80000, 80000],
                ["Cash Balance", 1000000, 920000, 840000],
            ],
            "detected_type": "pnl",
            "periodicity": "monthly",
            "row_count": 4,
            "col_count": 4,
            "pre_header_rows": [["Acme Corp", None, None, None]],
        }
    ],
    "source_format": "xlsx",
    "source_file": "acme-model.xlsx",
    "periodicity_summary": "monthly",
}

_INPUTS: dict[str, Any] = {
    "company": {
        "company_name": "Acme Corp",
        "slug": "acme-corp",
        "stage": "seed",
        "sector": "B2B SaaS",
        "geography": "US",
        "model_format": "spreadsheet",
    },
    "revenue": {
        "mrr": {"value": 60000, "as_of": "2025-03"},
        "customers": 100,
    },
    "cash": {
        "current_balance": 840000,
        "monthly_net_burn": 80000,
    },
    "expenses": {
        "headcount": [
            {"role": "R&D", "count": 5, "salary_annual": 120000},
            {"role": "S&M", "count": 3, "salary_annual": 80000},
        ],
    },
}


# ---------------------------------------------------------------------------
# Skip tests
# ---------------------------------------------------------------------------


class TestSkipConditions:
    def test_skip_missing_model_data_file(self) -> None:
        """Skip when model_data file doesn't exist."""
        rc, data, _ = _run(_INPUTS, None)
        assert rc == 0
        assert data["status"] == "skip"

    def test_skip_stub_model_data(self) -> None:
        """Skip when model_data is a stub."""
        stub = {"skipped": True, "reason": "no file provided"}
        rc, data, _ = _run(_INPUTS, stub)
        assert rc == 0
        assert data["status"] == "skip"

    def test_skip_conversational_format(self) -> None:
        """Skip when model_format is conversational."""
        inputs = {**_INPUTS, "company": {**_INPUTS["company"], "model_format": "conversational"}}
        rc, data, _ = _run(inputs, _MODEL_DATA)
        assert rc == 0
        assert data["status"] == "skip"
        assert "conversational" in data["summary"]["skip_reason"]

    def test_skip_deck_format(self) -> None:
        """Skip when model_format is deck."""
        inputs = {**_INPUTS, "company": {**_INPUTS["company"], "model_format": "deck"}}
        rc, data, _ = _run(inputs, _MODEL_DATA)
        assert rc == 0
        assert data["status"] == "skip"

    def test_skip_empty_sheets(self) -> None:
        """Skip when model_data has no sheets."""
        rc, data, _ = _run(_INPUTS, {"sheets": []})
        assert rc == 0
        assert data["status"] == "skip"


# ---------------------------------------------------------------------------
# Pass tests
# ---------------------------------------------------------------------------


class TestPassConditions:
    def test_all_values_traceable(self) -> None:
        """Pass when all inputs values appear in model_data."""
        rc, data, _ = _run(_INPUTS, _MODEL_DATA)
        assert rc == 0
        assert data["status"] == "pass"
        assert data["summary"]["warn"] == 0
        assert len(data["correction_hints"]) == 0

    def test_output_flag(self) -> None:
        """The -o flag writes to file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            inputs_path = os.path.join(tmpdir, "inputs.json")
            model_data_path = os.path.join(tmpdir, "model_data.json")
            out_path = os.path.join(tmpdir, "result.json")
            with open(inputs_path, "w") as f:
                json.dump(_INPUTS, f)
            with open(model_data_path, "w") as f:
                json.dump(_MODEL_DATA, f)
            result = subprocess.run(
                [
                    sys.executable,
                    os.path.join(FMR_SCRIPTS_DIR, "validate_extraction.py"),
                    "--inputs", inputs_path,
                    "--model-data", model_data_path,
                    "-o", out_path,
                ],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0
            receipt = json.loads(result.stdout)
            assert receipt["ok"] is True
            with open(out_path) as f:
                written = json.load(f)
            assert written["status"] == "pass"


# ---------------------------------------------------------------------------
# Warn tests
# ---------------------------------------------------------------------------


class TestWarnConditions:
    def test_company_name_mismatch(self) -> None:
        """Warn when company name doesn't match model_data."""
        inputs = {**_INPUTS, "company": {**_INPUTS["company"], "company_name": "ZetaCorp Industries"}}
        rc, data, _ = _run(inputs, _MODEL_DATA)
        assert rc == 0
        checks_by_id = {c["id"]: c for c in data["checks"]}
        assert checks_by_id["COMPANY_NAME"]["status"] == "warn"
        assert "candidates" in checks_by_id["COMPANY_NAME"]
        assert any("company name" in h.lower() or "Company name" in h for h in data["correction_hints"])

    def test_salary_untraceable(self) -> None:
        """Warn when salary values aren't found in model_data."""
        inputs = json.loads(json.dumps(_INPUTS))
        inputs["expenses"]["headcount"] = [
            {"role": "Engineer", "count": 5, "salary_annual": 250000},
        ]
        rc, data, _ = _run(inputs, _MODEL_DATA)
        assert rc == 0
        checks_by_id = {c["id"]: c for c in data["checks"]}
        assert checks_by_id["SALARY_TRACEABILITY"]["status"] == "warn"
        assert any("salary" in h.lower() or "Salary" in h for h in data["correction_hints"])

    def test_revenue_untraceable(self) -> None:
        """Warn when revenue values aren't found in model_data."""
        inputs = json.loads(json.dumps(_INPUTS))
        inputs["revenue"]["mrr"]["value"] = 777777
        rc, data, _ = _run(inputs, _MODEL_DATA)
        assert rc == 0
        checks_by_id = {c["id"]: c for c in data["checks"]}
        assert checks_by_id["REVENUE_TRACEABILITY"]["status"] == "warn"
        assert any("revenue" in h.lower() or "Revenue" in h for h in data["correction_hints"])

    def test_cash_not_found(self) -> None:
        """Warn when cash balance isn't found in model_data."""
        inputs = json.loads(json.dumps(_INPUTS))
        inputs["cash"]["current_balance"] = 555555
        rc, data, _ = _run(inputs, _MODEL_DATA)
        assert rc == 0
        checks_by_id = {c["id"]: c for c in data["checks"]}
        assert checks_by_id["CASH_BALANCE"]["status"] == "warn"

    def test_scale_plausibility_low_cash(self) -> None:
        """Warn when cash balance is implausibly low for stage."""
        inputs = json.loads(json.dumps(_INPUTS))
        inputs["cash"]["current_balance"] = 4000  # $4K — model in thousands?
        rc, data, _ = _run(inputs, _MODEL_DATA)
        assert rc == 0
        checks_by_id = {c["id"]: c for c in data["checks"]}
        assert checks_by_id["SCALE_PLAUSIBILITY"]["status"] == "warn"
        assert any("thousand" in h.lower() or "million" in h.lower() for h in data["correction_hints"])

    def test_scale_plausibility_indicator_in_header(self) -> None:
        """Warn when model has ($000) scale indicator."""
        model = json.loads(json.dumps(_MODEL_DATA))
        model["sheets"][0]["headers"][0] = "Line Item ($000)"
        rc, data, _ = _run(_INPUTS, model)
        assert rc == 0
        checks_by_id = {c["id"]: c for c in data["checks"]}
        assert checks_by_id["SCALE_PLAUSIBILITY"]["status"] == "warn"
        assert any("($000)" in s for s in checks_by_id["SCALE_PLAUSIBILITY"].get("signals", []))

    def test_scale_plausibility_pass(self) -> None:
        """Pass when values are plausible for stage."""
        rc, data, _ = _run(_INPUTS, _MODEL_DATA)
        assert rc == 0
        checks_by_id = {c["id"]: c for c in data["checks"]}
        assert checks_by_id["SCALE_PLAUSIBILITY"]["status"] == "pass"


# ---------------------------------------------------------------------------
# Pre-header tests
# ---------------------------------------------------------------------------


class TestPreHeaderRows:
    def test_company_name_in_pre_header(self) -> None:
        """Company name found via pre_header_rows passes."""
        # Model data has "Acme Corp" only in pre_header_rows
        model = json.loads(json.dumps(_MODEL_DATA))
        # Remove "Acme Corp" from data rows
        model["sheets"][0]["rows"] = [
            ["Revenue", 50000, 55000, 60000],
            ["Payroll", 120000, 120000, 120000],
            ["Cash Balance", 840000, 920000, 1000000],
        ]
        # Company name is in pre_header_rows
        model["sheets"][0]["pre_header_rows"] = [["Acme Corp", None, None, None]]

        inputs = json.loads(json.dumps(_INPUTS))
        inputs["cash"]["current_balance"] = 840000
        inputs["expenses"]["headcount"] = [
            {"role": "Payroll", "count": 5, "salary_annual": 120000},
        ]
        rc, data, _ = _run(inputs, model)
        assert rc == 0
        checks_by_id = {c["id"]: c for c in data["checks"]}
        assert checks_by_id["COMPANY_NAME"]["status"] == "pass"


# ---------------------------------------------------------------------------
# Periodicity tests
# ---------------------------------------------------------------------------


class TestPeriodicity:
    def test_quarterly_salary_traceable(self) -> None:
        """Quarterly salary × 3 found when periodicity is quarterly."""
        model = {
            "sheets": [
                {
                    "name": "P&L",
                    "headers": ["Line Item", "Q1 2025", "Q2 2025"],
                    "rows": [
                        ["Revenue", 150000, 165000],
                        ["R&D Payroll", 360000, 360000],
                        ["Cash Balance", 1000000, 850000],
                    ],
                    "detected_type": "pnl",
                    "periodicity": "quarterly",
                    "row_count": 3,
                    "col_count": 3,
                    "pre_header_rows": [["TestCo", None, None]],
                }
            ],
            "source_format": "xlsx",
            "source_file": "test.xlsx",
            "periodicity_summary": "quarterly",
        }
        inputs = {
            "company": {
                "company_name": "TestCo",
                "slug": "testco",
                "stage": "seed",
                "sector": "SaaS",
                "geography": "US",
                "model_format": "spreadsheet",
            },
            "revenue": {"mrr": {"value": 55000, "as_of": "2025-06"}, "customers": 50},
            "cash": {"current_balance": 850000, "monthly_net_burn": 50000},
            "expenses": {
                "headcount": [
                    {"role": "R&D", "count": 5, "salary_annual": 120000},
                ],
            },
        }
        rc, data, _ = _run(inputs, model)
        assert rc == 0
        checks_by_id = {c["id"]: c for c in data["checks"]}
        # salary_annual=120000, monthly=10000, quarterly=30000
        # Model has 360000 quarterly. 120000 * 3 = 360000. Should be traceable.
        assert checks_by_id["SALARY_TRACEABILITY"]["status"] == "pass"


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_missing_pre_header_rows_key(self) -> None:
        """Missing pre_header_rows key treated as empty array."""
        model = json.loads(json.dumps(_MODEL_DATA))
        del model["sheets"][0]["pre_header_rows"]
        # Company name won't be found in pre_header (it's gone) but it is
        # in the row data as "Acme Corp" doesn't appear there either,
        # so check won't crash
        rc, data, _ = _run(_INPUTS, model)
        assert rc == 0
        # Should not crash — that's the key assertion
        assert data["status"] in ("pass", "warn")
