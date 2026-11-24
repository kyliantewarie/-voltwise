# Publishing checklist

## Repository state

Published at `https://github.com/kyliantewarie/-voltwise` on branch `main` with
tag `v0.1.0-beta`. GitHub Actions CI is green on Ubuntu, including the Docker
image build that cannot run locally.

GPG signing was verified with a throwaway key (`audit/release.py` wrote and
verified `SHA256SUMS.asc`). A real release key is still required before tagging
a signed release.

## Release artefacts
- [ ] `python -m pytest -q` passes (opt-in live/HIL tests may be skipped).
- [ ] `python -m pytest tests/test_simulator.py tests/test_simulator_scenarios.py -q` passes.
- [ ] Clean-room install: `python -m venv /tmp/vw && pip install . && python -m voltwise --config config.example.json --dry-run`.
- [ ] `python -m ruff check voltwise tests audit` passes.
- [ ] `python -m mypy voltwise --ignore-missing-imports` passes.
- [ ] `python -m bandit -q -r voltwise --skip B104` passes.
- [ ] `python -m pip_audit --requirement requirements.txt --requirement requirements-dev.txt` passes.
- [ ] `python audit/run_audits.py` reports 0 findings.
- [ ] `python audit/release.py` with `VOLTWISE_RELEASE_KEY` set; verify `SHA256SUMS.asc`.
- [ ] Version matches in `voltwise/_version.py`, `pyproject.toml`, `addon/config.yaml`,
      `custom_components/voltwise/manifest.json`.
- [ ] `addon/voltwise` matches `voltwise/` (audit drift check).

## Documentation
- [ ] README installation paths (add-on, HACS, standalone Docker) verified.
- [ ] `docs/SAFETY.md`, `docs/SECURITY.md`, `docs/INCIDENT_RESPONSE.md`,
      `docs/COMMERCIAL.md` current.
- [ ] Supported inverter list and commissioning steps in `docs/HARDWARE.md`.
- [ ] Savings described as a modelled estimate, never a billing guarantee.

## Known limitations (must stay in release notes)
- Offline licensing is not tamper-proof; premium gating is an honour system against a
  determined local attacker.
- No inverter has been hardware-validated by the maintainers; installers must commission.
- Tariff APIs can change; prefer `provider=home_assistant` with the official HA
  Nord Pool integration.
- CRA process is technical only; no EU representative or legal sign-off.
- Docker image build is verified in CI, not on maintainer machines.
