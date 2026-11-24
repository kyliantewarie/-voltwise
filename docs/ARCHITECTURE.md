# Architecture

```
config.json / options.json
        │  (strict validation, hard caps)
        ▼
  VoltWiseConfig ──► TariffProvider ──► PricePoint[n]
        │                                │
        │                                ▼
        └──► ForecastProvider ──► Forecasts(prices, solar, consumption)
                                        │
                                        ▼
                              schedule_ev()  ── EV power + EV SOC series
                                        │
                                        ▼
                      _battery_power_schedule()  (backward DP over energy)
                                        │
                                        ▼
                              _simulate() ──► Plan
                                        │
                          audit_plan() (independent re-simulation)
                             │                    │
                        passed              failed
                             │                    │
                             ▼                    ▼
                          Plan              FALLBACK Plan (0 kW)
                             │
                    plan_commands() ──► HomeAssistantClient (allow-listed writes)
                             │
                    plan.json ──► read-only ingress web UI
```

## Optimisation formulation

State: battery energy `E` discretised into `soc_levels` points between the hard
min and max. Action: battery AC power `b` discretised into `action_steps`
fractions of the charge/discharge limits. Transition:

```
E(t+1) = E(t) + b·dt·η_charge     (b > 0)
E(t+1) = E(t) + b·dt/η_discharge  (b < 0)
grid(t) = load(t) - solar(t) + ev(t) + b(t)
cost(t) = max(0, grid)·buy·dt - max(0, -grid)·feed_in·dt + |b|·dt·degradation
```

Backward DP minimises `sum(cost)` subject to terminal SOC ≥ reserve. The
resulting schedule is then re-simulated by an auditor that shares no code with
the optimiser's transition function.
