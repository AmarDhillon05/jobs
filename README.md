# Internship Job Monitor

Continuously monitors ~100-150 high-value companies for newly posted software
engineering and adjacent technical internships, and alerts you within roughly one
polling interval (default: 10 minutes) by email and push notification.

> **This repository has never been deployed to a real AWS account.** Every AWS
> behaviour claimed here was validated against LocalStack, Moto, or botocore
> stubs. See `ARCHITECTURE.md` for the exact real-vs-emulated boundary and
> `VALIDATION_REPORT.md` for evidence.

Status: **under construction** - this README is completed in the final
documentation phase. See `CLAUDE.md` for the full product requirements.

## Quick start

```bash
make setup     # python venv + node toolchains
make test      # backend test levels 1-4 and 6
make verify    # the hard gate: everything, from clean
```

## Documents

| File | Contents |
| --- | --- |
| `CLAUDE.md` | Product requirements & agent execution spec (the spec for this build) |
| `ARCHITECTURE.md` | Chosen design, alternatives, tradeoffs, emulation gaps, cost |
| `TESTING.md` | Test pyramid, what is real vs mocked vs emulated |
| `COMPANY_COVERAGE.md` | Company/provider coverage report |
| `BLOCKERS.md` | Every blocker hit, and its resolution |
| `VALIDATION_REPORT.md` | Requirement -> status -> evidence matrix |
