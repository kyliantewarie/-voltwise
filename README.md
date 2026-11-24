# VoltWise

A Home Assistant add-on that schedules a home battery and (optionally) an EV
charger against day-ahead electricity prices. It minimises the **total site
cost** — grid purchases minus export revenue, plus a battery-degradation cost —
while enforcing hard physical and connection limits.

## Status

| Item | Value |
|---|---|
| Version | 0.1.0 |
| License | Apache-2.0 |
| Runtime | Python 3.11+ (PyYAML for config, cryptography for licence checks) |
| Actuation default | **OFF** (dry-run must be explicitly disabled) |

## Why this exists

Research into the Home Assistant add-on market (see `../Home Assistant Market
Research.md`) identified dynamic-tariff battery optimisation as the strongest
software opportunity: many fragmented community projects, high-value European
households, and no polished product. VoltWise implements the core engine with
a safe, auditable architecture.

## How it works

1. **Tariff provider** supplies exactly *n* day-ahead prices (manual, Nord Pool,
   Tibber or Octopus), verified to cover the requested time window. Provider
   feed-in prices are honoured when present.
2. **Live state** optionally reads the battery SOC from a Home Assistant sensor
   (`battery.soc_entity`); otherwise the configured starting SOC is used.
3. **Forecast provider** supplies solar production and household consumption.
4. **EV scheduler** places the required charging energy into the cheapest slots
   before the deadline, limited by site import capacity and EV capacity.
5. **Battery optimiser** solves a dynamic program over discretised battery
   energy, including solar curtailment when the export limit would otherwise be
   exceeded, and valuing energy left in the battery at the end of the horizon.
6. **Independent safety audit** re-simulates the plan from raw inputs and checks
   every hard constraint. Any hard violation produces a **zero-power fallback**.
7. **Actuation** (only if enabled twice) writes the next slot's targets to
   allow-listed Home Assistant entities. If prices, forecasts, live SOC or the
   safety audit fail, a **safe stop** writes zero power instead of leaving stale
   setpoints active.

## Quick start

```powershell
python -m pip install -r requirements.txt
python -m voltwise --config config.example.json --dry-run --output plan.json
```

Expected output includes a status (`OPTIMAL`, `DEGRADED` or `FALLBACK`), the
modelled cost, the no-optimisation baseline and the savings.

To validate configuration and inputs without optimising:

```powershell
python -m voltwise --config config.example.json --validate-only
```

To run continuously with the read-only web UI:

```powershell
python -m voltwise.service --config config.example.json --interval-seconds 900
```

## Home Assistant add-on

Copy `addon/` into an add-on repository and build with the Supervisor. The
add-on exposes an ingress panel at port 8099 showing the latest plan.
Actuation is disabled unless `actuation.enabled` is true **and** the service is
started with `--apply` (see `addon/run.sh`).

Each write target must be one of:
`number.voltwise_battery_power`, `input_number.voltwise_target_soc`,
`number.voltwise_ev_charge_power` (entity IDs are configurable but strictly
validated). In Home Assistant, wire these helpers to your inverter/EV charger
through your existing integration (e.g. a `number` entity exposed by an
ESPHome or Modbus integration) — VoltWise never talks to hardware directly.

## Interpreting savings

`baseline_cost` is a **modelled estimate** (`baseline_method` and
`baseline_is_estimate` are in the plan JSON), not a utility bill. Do not use the
demo's savings percentage as a marketing claim without a measured, documented
baseline.

## Installation options

- **Add-on (HA OS):** copy `addon/` into your add-on repository.
- **HACS integration (Container/Core):** copy `custom_components/voltwise/` into
  `config/custom_components/` and add the integration from Settings → Devices &
  Services (a config flow is provided). Actuation still comes from the
  standalone container image built with the root `Dockerfile`.
- **Standalone Docker:** `docker build -t voltwise .`, mount your config at
  `/config/voltwise.json`.

## Required watchdog setup

Copy `addon/watchdog_automation.yaml` into Home Assistant. It forces zero
setpoints when the `input_datetime.voltwise_plan_expiry` helper has passed.
VoltWise writes that expiry every cycle, and writes a past timestamp on every
fail-safe path. Without this automation a crashed add-on can leave stale
setpoints active. If you rename entities or use an MQTT/Modbus adapter, generate
a matching automation with
`python -m voltwise --config config.json --watchdog-automation watchdog.yaml`.
The add-on logs a warning whenever custom actuation settings are detected.

## Testing without hardware

`simulator/` contains a fake Home Assistant REST API plus simulated battery, EV
charger and grid meter. Run `python -m simulator.runner --cycles 6` for an
end-to-end demonstration or `python -m pytest tests/test_simulator.py` for the
functional checks (watchdog, ramp limits, BMS cut-offs, rollback, circuit
breaker, closed-loop feedback). It cannot replace real electrical commissioning.

## Supported hardware

VoltWise writes HA helper entities, not devices. Commissioning steps and the
sign-convention checklist are in `docs/HARDWARE.md`. Release process and known
limitations are in `PUBLISHING.md`.

## Commercial options

See `docs/COMMERCIAL.md` for editions, licence keys, installer/multi-site
workflow and the signed update channel.

## Safety and security

See `docs/SAFETY.md` and `docs/SECURITY.md`. In short: bounded inputs, hard
constraints re-checked after optimisation, fail-closed fallback, HTTPS-only
 tariff endpoints, token redaction, allow-listed writes, circuit breaker, and
a loopback-only read-only web UI.

## Verifying a release

Release archives are accompanied by `SHA256SUMS` and `SHA256SUMS.asc`, signed
with the key in [`docs/release-key.asc`](docs/release-key.asc)
(fingerprint `D56BEC61E9AD6CD260475E3BB6A3D1EFBE45C548`):

```powershell
gpg --import release-key.asc
gpg --verify SHA256SUMS.asc
```

## Audits

Run `python audit/run_audits.py` to execute unit tests, functional/safety
fuzzing, a static security scan and SBOM generation. Results are written to
`audit/AUDIT_REPORT.md`. `python audit/release.py` writes `SHA256SUMS` (and a GPG
signature when `VOLTWISE_RELEASE_KEY` is set).

Live tariff smoke tests are opt-in via `VOLTWISE_LIVE_TARIFF_TESTS=1`; the
hardware-in-the-loop contract test needs `VOLTWISE_HIL_HA_URL` and `HA_TOKEN`.
Both are skipped by default so normal CI stays offline.
