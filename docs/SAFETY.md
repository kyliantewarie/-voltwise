# Safety design

VoltWise influences physical equipment. Safety is therefore enforced at four
independent layers; a failure in one layer must not be able to cause unsafe
actuation.

## Layer 1 — Input bounds (`voltwise/config.py`)

Every operator-supplied value is type-checked, finite-checked and bounded:
capacity ≤ 200 kWh, power ≤ 100 kW, 1–168 slots, 15–60 minute intervals,
efficiencies in [0.5, 1], SOC in [0, 100] with `min < max ≤ 100`, reserve
between min and max, EV target ≥ EV initial. Unknown keys are rejected.

## Layer 2 — Hard constraints in the optimiser (`voltwise/optimizer.py`)

The dynamic program only considers powers that satisfy, for every slot:

- `-P_discharge ≤ battery_kw ≤ P_charge` (hardware limits)
- `-grid_export_limit ≤ net_load + battery_kw ≤ grid_import_limit`
- `max(SOC_min, reserve) ≤ SOC_t ≤ SOC_max` at every step (the reserve is a
  discharge floor, not just an end-of-horizon target; if the battery starts
  below reserve, the current SOC is the temporary floor)
- final SOC ≥ reserve SOC
- EV SOC ≤ 100% and EV power ≥ 0

Charging and discharging are represented by a single signed variable, so
simultaneous charge/discharge is structurally impossible.

## Layer 3 — Independent post-hoc audit (`voltwise/safety.py`)

`audit_plan()` re-simulates the plan from the original forecasts. It verifies
power balance, cost recomputation, all hardware/grid limits, SOC trajectory,
EV trajectory, deadline and reserve. A single **hard** violation discards the
plan and returns `make_fallback_plan()`, which requests 0 kW from every
actuator and preserves the current SOC.

## Layer 4 — Actuation limits (`voltwise/ha_client.py`, `voltwise/cli.py`)

- Writes require **both** `actuation.enabled: true` and the CLI `--apply` flag.
- Only four allow-listed entity IDs in four allow-listed domains are writable.
- Values must be finite; a circuit breaker opens after repeated failures.
- Dry-run is the default and performs no network writes at all.

## Residual risks (require on-site commissioning)

1. VoltWise writes *setpoints*, not safety interlocks. The inverter/BMS must
   retain its own hard limits and must be configured to ignore out-of-range
   commands.
2. Forecast errors can produce sub-optimal but still safe schedules; the hard
   constraints are evaluated against the forecast, not against measured
   reality. Re-optimise frequently (default 15 min).
3. Cell-level balancing, temperature derating and warranty terms are BMS
   responsibilities and are intentionally outside VoltWise's model.
4. Live tariff API endpoints can change; validate a small sample before
   enabling a non-manual provider in production.
