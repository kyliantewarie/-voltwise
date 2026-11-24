## Summary

<!-- What does this change and why? -->

## Safety impact

<!-- Does it touch the optimiser, actuation, watchdog or fail-safe paths? -->

- [ ] No safety-critical code changed
- [ ] Safety-critical code changed, with regression tests added

## Checklist

- [ ] `ruff`, `mypy`, `bandit`, `pytest` and `run_audits.py` pass locally
- [ ] `addon/voltwise/` synced with `python audit/sync_addon.py` if package code changed
- [ ] Config keys added to `addon/config.yaml` and all translations
- [ ] No secrets, tokens or private keys included
- [ ] Documentation updated if behaviour changed
