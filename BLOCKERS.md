# BLOCKERS

Living log of every blocker hit during implementation, per PRD §18.

Statuses: `open` · `deferred` · `revisiting` · `resolved` · `accepted-limitation`

Severity: `critical` (blocks a hard completion gate) · `major` · `minor`

---

## Summary

| ID | Component | Severity | Status |
| --- | --- | --- | --- |
| [BLK-001](#blk-001---outbound-egress-policy-blocks-all-third-party-careersats-hosts) | Live scraper validation | major | accepted-limitation |

---

## BLK-001 - Outbound egress policy blocks all third-party careers/ATS hosts

- **Timestamp / stage:** 2026-09-26, Phase B (company discovery)
- **Requirement affected:** PRD §31 (live scraper validation), §32 (do not claim a
  company is supported unless its configured source passes a scraper test), §14
  Level 7 ("at least one architecture test should also run a live-public scraper")
- **Component:** `scripts/validate_companies.py`, `tests/scrapers` live marker
- **Observed failure:** every request to a public ATS endpoint fails at the egress
  proxy before leaving the sandbox:

  ```text
  $ curl -sS https://boards-api.greenhouse.io/v1/boards/anthropic/jobs
  curl: (56) CONNECT tunnel failed, response 403

  $ curl -sS "$HTTPS_PROXY/__agentproxy/status"
  "recentRelayFailures": [
    {"kind": "connect_rejected",
     "detail": "gateway answered 403 to CONNECT (policy denial or upstream failure)",
     "host": "boards-api.greenhouse.io:443"},
    {... "host": "api.ashbyhq.com:443"},
    {... "host": "api.lever.co:443"}, ...]
  ```

  229 candidate probes across greenhouse / lever / ashby / smartrecruiters
  returned zero successes; PyPI, npm and raw.githubusercontent.com all work, so
  this is a host allow-list, not a broken network.
- **Expected behaviour:** a GET to each provider's public job-board API returns the
  live posting list, letting the registry's `support_status` be set from a real
  response and `--date-validated` be recorded.
- **Attempts made:**
  1. Direct `curl` and `urllib` against four provider APIs - 403 on CONNECT.
  2. Bulk parallel probe of 229 company/provider/slug candidates - 0 successes.
  3. Read `/root/.ccr/README.md` and the proxy status endpoint. It states
     explicitly: *"403 / 407 from the proxy - the destination host is not allowed
     by your organization's egress policy for this session. Do not retry or route
     around it - report the blocked host."* No further attempts made, by design.
- **Evidence / logs:** proxy status output above; probe script preserved at
  `scripts/validate_companies.py` (same request shapes, runnable by the user).
- **Current hypothesis:** the session's network policy allows only package
  registries and GitHub raw content. Nothing in this repository can change that.
- **Alternative hypotheses:** (a) transient upstream failure - ruled out, 229/229
  failed across four unrelated hosts; (b) TLS trust problem - ruled out, the
  failure is at CONNECT, and the same proxy serves PyPI/npm/GitHub successfully.
- **Next actions (for the user, outside this sandbox):**
  `make validate-companies` re-probes every configured company and rewrites
  `support_status` + `last_validated` from real responses. `make test-live` runs
  the opt-in live spot checks.
- **Why this is not critical:** the PRD deliberately excludes live endpoints from
  the completion gates - §31 "Do not make the full test suite dependent on every
  external careers site", and §14 Level 7 "completion must not depend on an
  external site remaining online". Provider correctness is therefore proven with
  fixture-based tests whose fixtures reproduce each provider's documented
  response shape, plus a per-company config test (`supported` is an
  *automatically tested* claim, defined precisely in `COMPANY_COVERAGE.md`).
- **Severity:** major (reduces confidence in per-company slugs; blocks no gate)
- **Status:** accepted-limitation

---

<!-- New entries appended below, newest last. Template:

## BLK-000 - short title

- **Timestamp / stage:** YYYY-MM-DD, Phase X
- **Requirement affected:** PRD §N - ...
- **Component:** ...
- **Observed failure:** ...
- **Expected behaviour:** ...
- **Attempts made:**
  1. ...
- **Evidence / logs:** ...
- **Current hypothesis:** ...
- **Alternative hypotheses:** ...
- **Next actions:** ...
- **Severity:** ...
- **Status:** ...
-->
