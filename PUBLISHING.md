# Publishing checklist

## Repository state

Published at `https://github.com/kyliantewarie/voltwise` on branch `main` with
tag `v0.1.0-beta`. GitHub Actions CI is green on Ubuntu, including the Docker
image build that cannot run locally.

> **Tag creation is a manual release step.** CI and the audit scripts never
> create tags or releases. A maintainer creates and pushes the version tag only
> after the release checklist passes; a fresh clone may therefore have no tags
> even though the documentation refers to `v0.1.0-beta`. Treat the tag name as
> the intended naming convention until that manual step has been performed.

## Release signing key

Releases are signed with a dedicated Ed25519 key:

- **Identity:** `VoltWise Release <348753426+kyliantewarie@users.noreply.github.com>`
- **Fingerprint:** `D56BEC61E9AD6CD260475E3BB6A3D1EFBE45C548`
- **Expires:** 2028-11-23
- **Public key:** [`docs/release-key.asc`](docs/release-key.asc)

The private key is held outside this repository by the maintainer. To sign a release:

```powershell
$env:GNUPGHOME = "$env:USERPROFILE\.voltwise\gnupg"
$env:VOLTWISE_RELEASE_KEY = "D56BEC61E9AD6CD260475E3BB6A3D1EFBE45C548"
$env:VOLTWISE_GPG_PASSPHRASE_FILE = "$env:USERPROFILE\.voltwise\release-passphrase.txt"
python audit/release.py
gpg --verify SHA256SUMS.asc
```

Attach `SHA256SUMS`, `SHA256SUMS.asc` and `docs/release-key.asc` to the GitHub
release. Verify a downloaded release with:

```powershell
gpg --import release-key.asc
gpg --verify SHA256SUMS.asc
```

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
