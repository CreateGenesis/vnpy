# vn.py Workspace Guide

Read `../AGENTS.md` and `../SPEC.md` before changing this repository.

- vn.py is the sole owner of gateways, broker submissions and cancellations, reconciliation, positions, hard risk, lifecycle, pause, and emergency stop.
- Rust and Agents may provide observations, research, candidate packages, and bounded intents only; vn.py independently validates and may reject every request.
- Preserve the Simplified Chinese operator experience, loopback authentication, write-only secrets, and absence of manual order/risk override surfaces.
- Run focused Python tests with `python -m pytest <test-path> -q` and focused UI tests from `web/demo-ui` with `npm test -- <pattern>`.
- Do not run the full Python, frontend, or Playwright suites unless the user requests a release or full regression.
