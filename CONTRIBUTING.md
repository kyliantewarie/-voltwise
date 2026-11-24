# Contributing to VoltWise

Thanks for helping improve VoltWise. Please read `docs/SAFETY.md` before changing
the optimiser, actuation or watchdog code — this project controls physical
equipment.

## Getting set up

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

## Before opening a pull request

Run the full local pipeline:

```powershell
python -m ruff check voltwise tests audit simulator
python -m mypy voltwise --ignore-missing-imports
python -m bandit -q -r voltwise --skip B104
python -m pytest -q
python audit/run_audits.py
```

All steps must pass. CI repeats them on Ubuntu and also builds the Docker image.

## Testing without hardware

```powershell
python -m simulator.runner --cycles 6
python -m pytest tests/test_simulator.py tests/test_simulator_scenarios.py -q
```

The simulator covers actuation, watchdog, BMS limits, ramp limits, EV charging,
live-SOC feedback and sub-hourly intervals. It is a functional substitute, not
an electrical model — real commissioning is still required.

## Rules for changes

- Any change to solver constraints must keep `audit_plan` independent of the
  optimiser and add a regression test.
- Never remove a fail-closed path (fallback, safe stop, watchdog) without a
  replacement that is at least as strict.
- New configuration keys must appear in `addon/config.yaml`, all three
  translation files, and the round-trip config test.
- Keep `addon/voltwise/` in sync via `python audit/sync_addon.py`.
- Do not commit secrets, API keys, the GPG private key or the passphrase file.

## Reporting bugs

Use the issue template and include: HA version, installation method, config with
secrets removed, the plan JSON, and the relevant log lines. For security issues,
follow `docs/SECURITY.md` instead of opening a public issue.
