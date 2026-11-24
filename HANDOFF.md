# VoltWise — handoff (status 2026-11-24)

## Where things live

| Item | Location |
|---|---|
| Source + git repo | `C:\Users\kylia\Desktop\herdr projecten\voltwise` (branch `main`, clean) |
| GitHub | https://github.com/kyliantewarie/voltwise (public, Apache-2.0) |
| Release | https://github.com/kyliantewarie/voltwise/releases/tag/v0.1.0-beta (prerelease, 3 signed assets) |
| Gumroad product | Published ("VoltWise Pro Kit") |
| Gumroad ZIP | `dist\voltwise-pro-kit.zip` (56 files, includes licence key + docs + screenshots) |
| Gumroad copy | `dist\GUMROAD_LISTING.md` |
| Screenshots | `dist\screenshots\` (desktop, mobile, 600×600 thumbnail) |
| Full audit history | `audit\FINAL_REPORT.md` |
| Publishing process | `PUBLISHING.md` |
| Contribution rules | `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `ATTRIBUTION.md` |

## Keys (outside the repository — do not commit)

| Item | Path |
|---|---|
| GPG release public key | `docs\release-key.asc` (fingerprint `D56BEC61E9AD6CD260475E3BB6A3D1EFBE45C548`) |
| GPG private key | `C:\Users\kylia\.voltwise\gnupg` (GPG home) |
| GPG passphrase | `C:\Users\kylia\.voltwise\release-passphrase.txt` |
| GPG private backup | `C:\Users\kylia\.voltwise\voltwise-release-PRIVATE.asc` |
| Licence signing private key | `C:\Users\kylia\.voltwise\licence-private.pem` |
| Licence mint script | `audit\mint_licence.py` |

## Last verified state

- GitHub CI: **success** on `main` (lint, mypy, pip-audit, bandit, tests, full audit, release artefacts, Docker build)
- Local: all tests pass, ruff/mypy clean, `audit/run_audits.py` → **0 findings**
- Release signed and verified with `gpg --verify SHA256SUMS.asc`
- 18+ adversarial audit campaigns resolved; no open findings

## Useful commands

```powershell
# Full local verification
python -m pytest -q
python audit/run_audits.py
python audit/release.py

# See the UI (needs a plan JSON)
$env:VOLTWISE_PLAN_PATH = "$PWD\audit\demo-plan.json"
python -m voltwise.webui        # http://127.0.0.1:8099

# Hardware-free simulation
python -m simulator.runner --cycles 6

# Mint a replacement Gumroad licence key
python audit/mint_licence.py --subject 'VoltWise Pro Kit - Gumroad' --features core,pro --days 3650
```

## Deliberately deferred (wait for user feedback)

- GitHub repository topics (needs repo-admin permission in the web UI)
- HACS default listing and Home Assistant add-on store submission
- PyPI publication
- Real inverter commissioning and CRA/legal sign-off

The product is explicitly a **beta**: functionally simulated, not hardware-commissioned. Keep that framing until real users report results.
