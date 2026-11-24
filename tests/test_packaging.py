"""Add-on build-context tests (these would have caught the H2 packaging defect)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "audit"))

from sync_addon import fingerprints, sync  # noqa: E402


def test_addon_build_context_contains_all_copy_sources():
    sync()
    addon = ROOT / "addon"
    dockerfile = (addon / "Dockerfile").read_text(encoding="utf-8")
    for line in dockerfile.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].upper() == "COPY":
            source = parts[1]
            if not source.startswith("/"):
                assert (addon / source).exists(), f"missing from build context: {source}"


def test_vendored_sources_match_root_exactly():
    sync()
    hashes = fingerprints()
    for name, digest in hashes.items():
        if not name.endswith("@addon"):
            assert hashes[name + "@addon"] == digest, f"stale vendored copy: {name}"


def test_watchdog_automation_is_shipped():
    automation = ROOT / "addon" / "watchdog_automation.yaml"
    assert automation.is_file()
    text = automation.read_text(encoding="utf-8")
    assert "input_datetime.voltwise_plan_expiry" in text
    assert "number.set_value" in text


def test_addon_schema_exposes_runtime_options():
    text = (ROOT / "addon" / "config.yaml").read_text(encoding="utf-8")
    assert "list(" not in text
    for key in ("soc_entity: 'str?'", "energy_value_per_kwh: 'float?'",
                "- 'float?'"):
        assert key in text
    assert "image:" not in text
