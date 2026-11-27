# Security design

## Threat model

Inputs come from three places: the add-on options file (operator-controlled but
still untrusted), third-party tariff APIs, and Home Assistant sensor states.
Outputs are numeric setpoints written to Home Assistant entities.

## Controls

| Area | Control |
|---|---|
| Config parsing | Strict key allow-list; finite & bounded numerics; safe YAML (`yaml.safe_load`), no object construction |
| Network egress | Tariff endpoints hard-coded and HTTPS-only; the Home Assistant URL is restricted to local/supervisor hosts so the Supervisor token can never be sent to an external host; TLS verification never disabled |
| Response handling | `Content-Length` + `max_response_bytes` caps; timeouts; JSON only; type checks on every field |
| Credentials | API keys read from environment variables; Home Assistant token from `SUPERVISOR_TOKEN`/`HA_TOKEN`; redacted from all logged errors; never written to a plan |
| Writes | Requires `enabled: true`, `--apply`, and (when configured) `--confirm`; entity-ID regex, explicit allow-list, domain allow-list, finite values, circuit breaker; dry-run by default |
| Web UI | Local mode binds loopback; add-on mode binds the container interface (required by Supervisor ingress) with a private/loopback source-IP filter; read-only GET/HEAD; HTML escaping; no file serving; strict CSP; no request-header logging |
| Code execution | No `eval`, `exec`, `pickle`, `shell=True`, `os.system`, dynamic imports from user data |
| Dependencies | Small fully pinned runtime set (PyYAML `==6.0.3`, cryptography `==50.0.2`); SBOM generated from requirements files by the audit script |

## Reporting a vulnerability

Open a private issue with reproduction steps. Do not include live API tokens.

## Disclosure and release process (CRA readiness)

- **SBOM:** `audit/sbom.cdx.json` is generated from the installed environment by
  `audit/run_audits.py` on every release build.
- **Coordinated disclosure:** reporters get a 90-day window before public disclosure.
  Acknowledge within 3 working days; track findings in a private advisory.
- **Security updates:** security fixes are released independently of feature
  updates and remain available free of charge for the supported release line.
- **Release integrity:** `audit/release.py` writes `SHA256SUMS` for the shipped
  artefacts and signs it with GPG when `VOLTWISE_RELEASE_KEY` is configured.
  Releases without a signing key are clearly marked unsigned.
- **Incident response:** follow `docs/INCIDENT_RESPONSE.md`; Article 14 reporting
  (24h early warning / 72h notification to ENISA) is owned by the release maintainer,
  with `audit/AUDIT_REPORT.md` preserved as evidence.
