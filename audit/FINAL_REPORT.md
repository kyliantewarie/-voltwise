# VoltWise improvement & audit summary

Generated after the full 23-point improvement programme and four adversarial campaigns.

## Implemented improvements

See `IMPLEMENTATION_PLAN.md` for the item-by-item checklist (all 23 complete).

Highlights:

- Actor-level safety: watchdog expiry + HA automation, transactional actuation with rollback,
  sensor freshness, closed-loop deviation detection, ramp limits, adaptive reserve.
- Economics: SOC-weighted degradation, tariff/site fees with deterministic precedence,
  forecast stress testing, explicit baseline method, no negative savings claims.
- Performance: hard solver ceiling; worst legal config cycle reduced from ~35 s to ~6.4 s.
- Distribution: add-on, HACS integration, standalone Docker image, generic/MQTT/Modbus adapters.
- Operations: CI with ruff/mypy/pytest/audit/Docker build, single-source version, generated
  SBOM, structured logs and metrics, disclosure/signing process.
- Commercial: offline HMAC licensing with feature gating, onboarding discovery, nl/de
  translations, sanitised diagnostics bundles.

## Adversarial campaigns

| Campaign | Confirmed issues | Status |
|---|---|---|
| Optimiser physics | 6 (EV unit bug, baselines, curtailment, NaN audit, fallback) | Fixed |
| Security/inputs | 8 (SSRF, recursion, overflow, log injection, dead confirmation, symlink, YAML, datetime) | Fixed |
| Runtime/packaging | 7 (UI crash, OSError, breaker, bind, slowloris, translations, schema) | Fixed |
| High-intensity fuzz | 2 (mixed timezone, reserve savings) + impossible-site rejection | Fixed |
| New safety/economics | 7 classes (auditor bypass, ramp savings, hang, stress, SOC timing, reserve, fees) | Fixed |
| Licensing/HA writes | 5 (licence bypass, watchdog ordering, rollback, safe-stop gate, timestamp) | Fixed |
| Packaging/CI | 8 (schema types, null defaults, shipped defaults, HACS, translations, audit gaps, drift) | Fixed |
| Regression fuzz v2 | 2 (non-mapping config, HA deep JSON) | Fixed |
| Final cross-cutting attack | 3 (stale SOC timestamp, watchdog coverage, deep plan JSON) | Fixed |
| Closing verification review | 4 (HA version type, fee precedence, HA error handling, stray `base`) | Fixed |
| Subsequent critical review | 12 (licence trust model, live tariff smoke, HIL, watchdog adapter coverage, atomic metrics, signing, config flow, fee duplication, savings transparency, SAST, Docker CI, solver budget) | Fixed or mitigated |
| Licensing/distribution attack | 11 (require bypass, flag bypass, import crash, hwid, expiry, malleability, key types) | Fixed |
| Packaging v3 attack | 6 (stale checksums, SBOM deps, pyproject dep, doc drift, translations) | Fixed |
| Fuzz v3 | 1 (non-finite previous HA state broke rollback) | Fixed |
| Economics attack | 2 (extreme finite price overflow, solver ceiling overrun) | Fixed |
| Publish-readiness batch | Live tariff provider via HA, Nord Pool endpoint fix, telemetry sensors, incident runbook, dependency audit, licence test isolation, publishing/hardware docs | Implemented |
| HA tariff/telemetry attack | 10 (malformed sensor payloads, alignment bypass, row splicing, Nord Pool real API, telemetry script/helpers, currency injection, overflow, test isolation, NaN forecasts) | Fixed |
| Live API end-to-end check | 2 (Cloudflare blocked default User-Agent; full-day payload window selection) | Fixed; live provider now returns real prices |
| Hardware simulator | Built fake HA REST API + battery/EV/grid physics; then adversarial review found 11 false-confidence defects (MQTT/Modbus ignored, watchdog helpers, stale SOC, dropped config, sub-hourly, ramp units, BMS taper, service metrics, patch race, EV path, dt=0 crash) | All fixed; 16 simulator tests pass |
| Clean-room verification | Full CI command sequence locally (ruff/mypy/pip-audit/bandit/pytest/audit/release/config), isolated `pip install .` + CLI run, 9 adversarial simulation scenarios | All pass; only Docker build remains CI-only |
| Release prep | Git installed, repo initialised, tag `v0.1.0-beta`, Dependabot config, `.gitattributes`, GPG signing pipeline verified with throwaway key | Done |
| Publication | Pushed to `github.com/kyliantewarie/voltwise`; first CI run exposed a Linux-only test failure (platform-dependent `RecursionError` depth) | Fixed with explicit `json_safe.ensure_json_depth`; CI run #9 green on Ubuntu incl. Docker build; stale Dependabot PRs closed |

## Verification evidence

- `python -m pytest -q` → all tests pass (plus 4 opt-in live/HIL tests skipped without credentials).
- `python -m ruff check voltwise tests audit` → clean.
- `python -m mypy voltwise --ignore-missing-imports` → clean.
- `python audit/run_audits.py` → 0 findings (unit, functional fuzz, tamper, static, packaging).
- Attacker reproducers re-run after fixes: all report graceful rejection or corrected behaviour.
- Concurrent plan writes: 12 processes, 0 failures, 200/200 valid reads.
- Worst-case solver cycle: 6.35 s (was ~35 s).

## Residual / accepted limitations

1. Premium features are always gated, but `licensing.require` for the core feature still
   lives in operator-editable options. Offline licensing on customer-owned hardware is not
   absolute DRM; sell support and updates rather than relying on the cryptographic boundary.
2. Physically impossible sites (load exceeding grid + battery + solar over the horizon) are
   rejected at input audit rather than being "fixed" by software.
3. Real inverter/BMS commissioning, firmware limits and warranty terms remain the
   installer's responsibility; see `docs/SAFETY.md`.
4. Tariff API endpoints can change; validate a small sample before production use.
5. A Docker daemon was unavailable for an actual image build; Dockerfile build contexts and
   CLI flags were verified statically. CI performs the real build.
6. Hardware commissioning and live tariff credentials must be supplied by the installer;
   the product ships contract tests and a documented checklist (`docs/HARDWARE.md`).
