# Security Policy

## Scope

`tachometer` collects local profiling data and serves a lightweight dashboard.
It must never become a place to store live secrets, host-specific private data,
or broadly exposed control surfaces. Profiling summaries and dashboard output
may reveal local repo state, resource usage, and operational timing, so the
dashboard is intended to stay on `localhost` by default unless remote exposure
is explicitly and intentionally enabled.

## Reporting

Report security issues privately to the repository owner instead of opening a
public issue with exploit details.

## Handling Rules

- Keep secrets, tokens, private keys, and personal data out of tracked profile
  fixtures, screenshots, and issue reports.
- Treat `.tachometer/` outputs as local-only operational data unless a tracked
  fixture is deliberately sanitized for tests.
- Do not expose the dashboard on a public network interface without an explicit
  trust boundary and authentication plan.
- Treat the `run-all` dashboard action as a privileged local operation; do not
  describe it as a safe anonymous remote endpoint in documentation or examples.
