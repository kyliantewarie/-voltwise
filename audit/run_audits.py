#!/usr/bin/env python3
"""Repeatable audit pipeline for VoltWise.

Runs: unit tests -> functional/safety fuzzing -> static security scan -> SBOM.
Exits non-zero if any finding is raised. The generated report lives at
audit/AUDIT_REPORT.md.
"""

from __future__ import annotations

import ast
import json
import platform
import random
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import make_config, make_config_dict, make_forecasts  # noqa: E402

from voltwise import __version__  # noqa: E402
from voltwise.config import VoltWiseConfig  # noqa: E402
from voltwise.optimizer import optimize  # noqa: E402
from voltwise.safety import audit_forecasts, audit_plan  # noqa: E402

PACKAGE = ROOT / "voltwise"
REPORT = ROOT / "audit" / "AUDIT_REPORT.md"
SBOM = ROOT / "audit" / "sbom.cdx.json"

FORBIDDEN_CALLS = {
    "eval", "exec", "system", "popen", "loads",  # loads is allowed only for json; checked separately
}
FORBIDDEN_ATTRS = {
    "verify",  # verify=False in requests-style calls
}
ALLOWED_LOADS = {"json"}


class Finding:
    def __init__(self, category: str, severity: str, location: str, message: str):
        self.category = category
        self.severity = severity
        self.location = location
        self.message = message

    def __str__(self) -> str:
        return f"- **[{self.severity}]** `{self.location}` — {self.message}"


def run_tests() -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output.strip()


def static_security_scan() -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        file_text = path.read_text(encoding="utf-8")
        tree = ast.parse(file_text, filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else (
                    func.attr if isinstance(func, ast.Attribute) else ""
                )
                owner = ""
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    owner = func.value.id
                if name in {"eval", "exec", "system", "popen", "mktemp"}:
                    findings.append(Finding("static", "HARD", f"{rel}:{node.lineno}",
                                            f"forbidden call {name}()"))
                if owner == "hashlib" and name in {"md5", "sha1"}:
                    findings.append(Finding("static", "HARD", f"{rel}:{node.lineno}",
                                            f"forbidden call {name}()"))
                if name == "load" and owner == "yaml":
                    findings.append(Finding("static", "HARD", f"{rel}:{node.lineno}",
                                            "yaml.load() used; require yaml.safe_load()"))
                if name == "loads" and owner not in ALLOWED_LOADS:
                    findings.append(Finding("static", "HARD", f"{rel}:{node.lineno}",
                                            f"{owner}.loads() is not allowed"))
                if name == "urlopen":
                    keywords = {kw.arg for kw in node.keywords}
                    if "timeout" not in keywords:
                        findings.append(Finding("static", "HARD", f"{rel}:{node.lineno}",
                                                "urlopen() without explicit timeout"))
                for kw in node.keywords:
                    if kw.arg in {"shell", "verify", "check_same_thread"} and isinstance(kw.value, ast.Constant):
                        if kw.value.value is True or kw.value.value is False:
                            findings.append(Finding("static", "HARD", f"{rel}:{node.lineno}",
                                                    f"unsafe literal keyword {kw.arg}={kw.value.value}"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if "0.0.0.0" in text and path.name == "webui.py":
                    guarded = all(token in file_text for token in ("container_mode", "ipaddress", "403"))
                    if not guarded:
                        findings.append(Finding("static", "HARD", f"{rel}:{node.lineno}",
                                                "0.0.0.0 bind requires container_mode + source-IP filter"))
                if text.startswith(("sk-", "ghp_", "xoxb-")):
                    findings.append(Finding("static", "HARD", f"{rel}:{node.lineno}",
                                            "possible hard-coded credential"))
    return findings


def functional_fuzz(iterations: int = 250) -> tuple[list[Finding], dict]:
    findings: list[Finding] = []
    stats = {"plans": 0, "optimal": 0, "degraded": 0, "fallback": 0, "violations": 0}
    random.seed(20261120)
    for run in range(iterations):
        n = random.choice([4, 6, 8, 12])
        prices = [round(random.uniform(-0.3, 1.0), 3) for _ in range(n)]
        solar = [round(random.uniform(0, 4), 2) for _ in range(n)]
        load = [round(random.uniform(0.1, 2.5), 2) for _ in range(n)]
        data = make_config_dict(
            n_slots=n, prices=prices, solar=solar, consumption=load, soc_levels=51,
            battery={
                "capacity_kwh": round(random.uniform(5, 20), 1),
                "initial_soc_pct": round(random.uniform(30, 80), 1),
                "soc_min_pct": 5.0, "soc_max_pct": 95.0,
                "reserve_soc_pct": round(random.uniform(15, 25), 1),
                "max_charge_kw": round(random.uniform(1, 6), 1),
                "max_discharge_kw": round(random.uniform(1, 6), 1),
            },
            site={
                "max_grid_import_kw": round(random.uniform(4, 12), 1),
                "max_grid_export_kw": round(random.uniform(0, 10), 1),
                "feed_in_factor": round(random.uniform(0, 0.6), 2),
            },
        )
        if run % 2 == 0:
            data["ev"] = {
                "capacity_kwh": round(random.uniform(30, 90), 1),
                "initial_soc_pct": round(random.uniform(10, 60), 1),
                "target_soc_pct": round(random.uniform(70, 95), 1),
                "deadline_slot": random.randrange(n),
                "max_charge_kw": round(random.uniform(3, 16), 1),
                "charge_efficiency": 0.95,
            }
        try:
            config = VoltWiseConfig.from_dict(data)
            forecasts = make_forecasts(config)
            if not audit_forecasts(config, forecasts).passed:
                # Random generation can produce physically impossible sites;
                # those are correctly rejected before optimisation.
                stats["rejected_inputs"] = stats.get("rejected_inputs", 0) + 1
                continue
            plan = optimize(config, forecasts)
            stats["plans"] += 1
            stats[plan.status.lower()] = stats.get(plan.status.lower(), 0) + 1
            if plan.status == "FALLBACK":
                fallback_audit = audit_plan(plan, config, forecasts)
                if not fallback_audit.passed:
                    findings.append(Finding("fuzz", "HARD", f"case {run}", "fallback plan failed audit"))
                continue
            report = audit_plan(plan, config, forecasts)
            if not report.passed:
                stats["violations"] += 1
                findings.append(Finding("fuzz", "HARD", f"case {run}",
                                        f"unsafe plan: {[v.code for v in report.hard_violations]}"))
            if plan.total_cost > plan.baseline_cost + 1e-6:
                findings.append(Finding("fuzz", "HARD", f"case {run}",
                                        "optimised cost exceeds no-optimisation baseline"))
            # Determinism check on a sample.
            if run % 25 == 0:
                again = optimize(config, forecasts)
                if again.to_dict()["slots"] != plan.to_dict()["slots"]:
                    findings.append(Finding("fuzz", "HARD", f"case {run}", "non-deterministic plan"))
        except Exception as exc:  # noqa: BLE001 - fuzz must surface any crash
            findings.append(Finding("fuzz", "HARD", f"case {run}",
                                    f"{type(exc).__name__}: {exc}"))
    return findings, stats


def tamper_fuzz(iterations: int = 60) -> list[Finding]:
    """Random single-field tampering must always be detected by the auditor."""
    from dataclasses import replace

    findings: list[Finding] = []
    random.seed(7)
    for run in range(iterations):
        n = 6
        config = make_config(n_slots=n, prices=[0.01, 0.02, 0.05, 0.40, 0.60, 0.10], soc_levels=51)
        forecasts = make_forecasts(config)
        plan = optimize(config, forecasts)
        if plan.status == "FALLBACK":
            continue
        index = random.randrange(n)
        slot = plan.slots[index]
        field = random.choice(["battery_kw", "grid_import_kw", "battery_soc_pct", "cost"])
        original = getattr(slot, field)
        mutated = replace(slot, **{field: original + 0.75})
        tampered = replace(plan, slots=tuple(
            [mutated, *plan.slots[1:]] if index == 0 else
            [*plan.slots[:index], mutated, *plan.slots[index + 1:]]
        ))
        report = audit_plan(tampered, config, forecasts)
        if report.passed:
            findings.append(Finding("tamper-fuzz", "HARD", f"case {run}",
                                    f"tampering with {field} went undetected"))
    return findings


def packaging_audit() -> list[Finding]:
    """Verify the shipped add-on files are parseable and internally consistent."""
    findings: list[Finding] = []
    from sync_addon import fingerprints, sync

    # Detect stale vendored files before overwriting them.
    hashes = fingerprints()
    sync()
    for name, digest in list(hashes.items()):
        if not name.endswith("@addon") and hashes.get(name + "@addon") != digest:
            findings.append(Finding("packaging", "HARD", name, "add-on copy differs from source"))

    version_sources = {
        "voltwise/_version.py": (PACKAGE / "_version.py").read_text(encoding="utf-8"),
        "pyproject.toml": (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        "addon/config.yaml": (ROOT / "addon" / "config.yaml").read_text(encoding="utf-8"),
    }
    if len({__version__ in text for text in version_sources.values()}) != 1:
        findings.append(Finding("packaging", "HARD", "version",
                                f"version {__version__} missing from a release file"))

    addon = ROOT / "addon"
    config_path = addon / "config.yaml"
    for required in ("config.yaml", "Dockerfile", "run.sh", "build.yaml", "translations/en.yaml"):
        if not (addon / required).is_file():
            findings.append(Finding("packaging", "HARD", required, "missing add-on file"))

    compile_result = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", str(PACKAGE)],
        capture_output=True, text=True, timeout=120,
    )
    if compile_result.returncode != 0:
        findings.append(Finding("packaging", "HARD", "compileall",
                                (compile_result.stderr or "").strip()[:500]))

    dockerfile = addon / "Dockerfile"
    if dockerfile.is_file():
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.upper().startswith("COPY "):
                source = stripped.split()[1]
                if not source.startswith("/") and not (addon / source).exists():
                    findings.append(Finding("packaging", "HARD", "Dockerfile",
                                            f"COPY source {source!r} missing from add-on build context"))

    if config_path.is_file():
        try:
            import yaml

            data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("root is not a mapping")
            for key in ("name", "version", "slug", "arch", "options", "schema", "ingress_port"):
                if key not in data:
                    findings.append(Finding("packaging", "HARD", "config.yaml", f"missing key {key}"))
            schema_text = config_path.read_text(encoding="utf-8")
            for accepted in ("soc_entity", "energy_value_per_kwh", "manual_feed_in_prices"):
                if accepted not in schema_text:
                    findings.append(Finding("packaging", "HARD", "config.yaml",
                                            f"schema does not expose {accepted}"))
            if "soc_entity: 'str?'" not in schema_text or "energy_value_per_kwh: 'float?'" not in schema_text:
                findings.append(Finding("packaging", "HARD", "config.yaml",
                                        "optional schema keys must use the '?' suffix"))
            if "image:" in schema_text:
                findings.append(Finding("packaging", "HARD", "config.yaml",
                                        "image key prevents local Dockerfile build"))

            def leaves(node, prefix=""):
                result = {}
                if isinstance(node, dict):
                    for key, value in node.items():
                        result.update(leaves(value, f"{prefix}.{key}" if prefix else key))
                elif isinstance(node, list) and node and all(isinstance(x, (str, int, float)) for x in node):
                    for index, value in enumerate(node):
                        result[f"{prefix}[{index}]"] = str(value)
                else:
                    result[prefix] = node
                return result

            options = data.get("options", {})
            option_leaves = leaves(options)
            for key, value in option_leaves.items():
                if value is None:
                    findings.append(Finding("packaging", "HARD", "config.yaml",
                                            f"option {key} has a null default (omit it instead)"))

            def schema_leaves(node, prefix=""):
                result = {}
                if isinstance(node, dict):
                    for key, value in node.items():
                        result.update(schema_leaves(value, f"{prefix}.{key}" if prefix else key))
                elif isinstance(node, list):
                    for value in node:
                        result.update(schema_leaves(value, prefix + "[]"))
                else:
                    result[prefix] = str(node)
                return result

            for key, type_name in schema_leaves(data.get("schema", {})).items():
                normalised = key.replace("[]", "[0]")
                if normalised.endswith("?") or type_name.endswith("?"):
                    continue
                if normalised not in option_leaves:
                    findings.append(Finding("packaging", "HARD", "config.yaml",
                                            f"required schema key {key} has no option default"))

            translations = addon / "translations" / "en.yaml"
            if translations.is_file():
                translated = yaml.safe_load(translations.read_text(encoding="utf-8"))
                configuration = translated.get("configuration", {}) if isinstance(translated, dict) else {}
                if not isinstance(configuration, dict):
                    findings.append(Finding("packaging", "HARD", "en.yaml",
                                            "configuration must be a mapping"))
                else:
                    for key, value in configuration.items():
                        if not isinstance(value, dict) or "name" not in value or "description" not in value:
                            findings.append(Finding("packaging", "HARD", "en.yaml",
                                                    f"translation for {key} needs name+description mappings"))
                    translated_keys = set()
                    for key, value in configuration.items():
                        translated_keys.add(key)
                        fields = value.get("fields", {}) if isinstance(value, dict) else {}
                        if isinstance(fields, dict):
                            translated_keys.update(f"{key}.{field}" for field in fields)
                    configured_keys = {
                        key.replace("[]", "") for key in schema_leaves(data.get("schema", {}))
                    } - {""}
                    for section_key, section_value in data.get("schema", {}).items():
                        if isinstance(section_value, dict):
                            configured_keys.add(section_key)
                    for missing in sorted(configured_keys - translated_keys):
                        findings.append(Finding("packaging", "HARD", "en.yaml",
                                                f"missing translation for schema key {missing}"))
                    for orphan in sorted(translated_keys - configured_keys):
                        findings.append(Finding("packaging", "HARD", "en.yaml",
                                                f"orphan translation key {orphan}"))
            # The shipped defaults must validate without being rewritten.
            VoltWiseConfig.from_dict(data.get("options", {}))
        except Exception as exc:  # noqa: BLE001 - packaging must surface any error
            findings.append(Finding("packaging", "HARD", "config.yaml",
                                    f"{type(exc).__name__}: {exc}"))

    run_script = addon / "run.sh"
    if run_script.is_file():
        lines = [
            line for line in run_script.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        ]
        text = "\n".join(lines)
        guard = "bashio::config.true 'actuation.enabled'"
        if guard not in text:
            findings.append(Finding("packaging", "HARD", "run.sh",
                                    "actuation must be guarded by the option flag"))
        elif text.count("--apply") != 1 or text.index("--apply") < text.index(guard):
            findings.append(Finding("packaging", "HARD", "run.sh",
                                    "--apply must appear exactly once, after the guard"))
    return findings


def generate_sbom() -> dict:
    """Generate a CycloneDX SBOM from the actual installed environment."""
    from importlib import metadata

    components: list[dict] = []
    runtime: set[str] = set()
    dev: set[str] = set()
    for filename, default in (
        ("requirements.txt", True),
        ("requirements-dev.txt", False),
    ):
        path = ROOT / filename
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "-r")):
                continue
            name = line.split("==")[0].split(">=")[0].split("<=")[0].strip()
            if name:
                (runtime if default else dev).add(name.lower())

    for name in sorted(runtime | dev):
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            version = "unknown"
        try:
            raw = metadata.metadata(name).get("License") or ""
            license_name = raw.split("\n")[0][:64] or "see-package-metadata"
        except metadata.PackageNotFoundError:
            license_name = "unknown (not installed)"
        components.append({
            "type": "library", "name": name, "version": version,
            "licenses": [{"license": {"name": license_name}}],
            "scope": "required" if name in runtime else "dev",
        })
    components.append({
        "type": "library", "name": "python", "version": platform.python_version(),
        "licenses": [{"license": {"name": "PSF-2.0"}}], "scope": "required",
    })
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:3e671687-395b-41f5-a30f-a58921a69b79",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(UTC).isoformat(),
            "component": {
                "type": "application", "name": "voltwise", "version": __version__,
                "licenses": [{"license": {"id": "Apache-2.0"}}],
            },
            "tools": [{"vendor": "OpenCode", "name": "voltwise-audit"}],
        },
        "components": components,
    }


def main() -> int:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    tests_ok, test_output = run_tests()
    static = static_security_scan()
    packaging = packaging_audit()
    fuzz, stats = functional_fuzz()
    tamper = tamper_fuzz()
    sbom = generate_sbom()
    SBOM.write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")

    findings = static + packaging + fuzz + tamper
    if not tests_ok:
        findings.append(Finding("tests", "HARD", "pytest", "unit test suite failed"))

    lines = [
        "# VoltWise audit report",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        f"Python: {platform.python_version()} | VoltWise: {__version__}",
        "",
        "## Summary",
        "",
        f"- Unit tests: **{'PASS' if tests_ok else 'FAIL'}**",
        f"- Functional fuzz cases: **{stats['plans']}** plans "
        f"(optimal={stats.get('optimal', 0)}, degraded={stats.get('degraded', 0)}, "
        f"fallback={stats.get('fallback', 0)}, "
        f"infeasible inputs rejected={stats.get('rejected_inputs', 0)})",
        f"- Unsafe plans found: **{stats['violations']}**",
        f"- Static security findings: **{len(static)}**",
        f"- Packaging findings: **{len(packaging)}**",
        f"- Tamper-detection findings: **{len(tamper)}**",
        f"- **Total findings: {len(findings)}**",
        "",
        "## Findings",
        "",
    ]
    lines += [str(f) for f in findings] or ["None."]
    lines += ["", "## Test output", "", "```text", test_output[-4000:], "```", ""]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:18]))
    print(f"Full report: {REPORT}")
    print(f"SBOM: {SBOM}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
