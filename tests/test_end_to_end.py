"""End-to-end CLI tests using the shipped example configuration."""

from __future__ import annotations

import json
from pathlib import Path

from voltwise.cli import main

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config.example.json"


def test_validate_only(tmp_path):
    assert main(["--config", str(EXAMPLE), "--validate-only"]) == 0


def test_full_plan_written_to_json(tmp_path):
    output = tmp_path / "plan.json"
    assert main(["--config", str(EXAMPLE), "--dry-run", "--output", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] in {"OPTIMAL", "DEGRADED"}
    assert len(payload["slots"]) == 8
    assert payload["savings"] >= 0.0
    assert payload["total_cost"] <= payload["baseline_cost"] + 1e-6


def test_missing_config_exits_cleanly():
    assert main(["--config", str(Path("does-not-exist.json"))]) == 2


def test_apply_and_dry_run_are_mutually_exclusive():
    assert main(["--config", str(EXAMPLE), "--apply", "--dry-run"]) == 2


def test_invalid_start_exits_cleanly():
    assert main(["--config", str(EXAMPLE), "--start", "not-a-date"]) == 2
