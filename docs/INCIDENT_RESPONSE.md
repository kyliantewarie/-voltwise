# Incident response runbook (EU CRA readiness)

## Roles
- **Release owner:** signs releases, owns the signing key and the ENISA reporting account.
- **Security contact:** published in `SECURITY.md`; receives private advisories.

## Detection
- `pip-audit` runs in CI on every commit and before each release.
- GitHub Dependabot / security alerts are enabled on the repository.
- Users report issues privately via the `SECURITY.md` contact.

## Triage (within 3 working days)
1. Acknowledge the report and assign a severity (critical/high/medium/low).
2. Reproduce against the latest release; record the affected versions.
3. If the issue is actively exploited or a severe incident, start the Article 14 clock immediately.

## CRA Article 14 timeline
- **24 hours:** early warning to ENISA via the single reporting platform.
- **72 hours:** full notification with affected products, versions and impact.
- **14 days:** final report with root cause and remediation.

## Remediation
1. Prepare a security-only patch release; do not bundle unrelated features.
2. Run `python audit/run_audits.py` and `python audit/release.py` with a signing key.
3. Publish the signed `SHA256SUMS`, advisory and fix simultaneously.
4. Keep security updates free for all supported release lines.

## Evidence to preserve
- `audit/AUDIT_REPORT.md`, `audit/sbom.cdx.json`, `SHA256SUMS`, the advisory thread and
  the commit hash of the fix.

## Limitations
This project has no EU representative and no legal counsel; the runbook is a technical
process, not legal advice. Engage qualified counsel before selling into the EU.
