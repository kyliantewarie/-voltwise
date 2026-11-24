# VoltWise improvement programme

Source: critical review of 2026-11-20. Status updated after each batch.

## P0 — physical / financial risk
- [x] 1. Actuator watchdog + plan expiry + auto-zero automation
- [x] 2. Transactional actuation (script call or rollback)
- [x] 3. Closed-loop verification (sensor freshness, measured-vs-planned)
- [x] 4. Ramp-rate / C-rate limits
- [x] 5. Adaptive reserve based on forecast uncertainty

## P0 — correctness of claimed value
- [x] 6. Explicit, auditable baseline specification in plan JSON
- [x] 7. SOC-weighted degradation model
- [x] 8. Forecast uncertainty / stress test
- [x] 9. Solver time budget + adaptive resolution
- [x] 10. Structured tariff model (buy/sell fees)

## P1 — Home Assistant fit
- [x] 11. HACS custom integration + standalone Docker image
- [x] 12. Inverter adapter layer (generic/MQTT/Modbus)
- [x] 13. Least-privilege token option

## P1 — engineering / release
- [x] 14. CI: pytest, ruff, mypy, audit, docker build
- [x] 15. Single-source version
- [x] 16. Build-context duplication cleanup (audited sync + checks)
- [x] 17. CRA: generated SBOM, disclosure policy, signed releases documented
- [x] 18. Structured logs + metrics
- [x] 19. Real HA contract test (not mocks)

## P1 — commercial
- [x] 20. Licensing / entitlement / feature gating
- [x] 21. Onboarding wizard + entity discovery
- [x] 22. i18n (nl, de)
- [x] 23. Diagnostics bundle + installer/update-channel documentation
