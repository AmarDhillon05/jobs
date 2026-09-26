# BLOCKERS

Living log of every blocker hit during implementation, per PRD §18.

Statuses: `open` · `deferred` · `revisiting` · `resolved` · `accepted-limitation`

Severity: `critical` (blocks a hard completion gate) · `major` · `minor`

---

## Summary

| ID | Component | Severity | Status |
| --- | --- | --- | --- |
| [BLK-001](#blk-001---outbound-egress-policy-blocks-all-third-party-careersats-hosts) | Live scraper validation | major | accepted-limitation |
| [BLK-002](#blk-002---provider-fixtures-could-not-be-captured-from-live-responses) | Provider fixtures | major | accepted-limitation |

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

## BLK-002 - Provider fixtures could not be captured from live responses

- **Timestamp / stage:** 2026-09-26, Phase D (provider coverage)
- **Requirement affected:** PRD §7 ("include fixture-based tests using saved
  representative responses"), §35 (no unverified claims)
- **Component:** `tests/fixtures/*`
- **Observed failure:** a downstream consequence of BLK-001. Fixtures are normally
  captured by recording one real response per provider. Every ATS host is
  403-blocked at the egress proxy, so nothing could be recorded.
- **Expected behaviour:** `curl <provider api> > tests/fixtures/<provider>/...`,
  giving byte-exact captures of live payloads.
- **What was done instead:** each fixture was authored to the provider's
  documented/public response shape, and each adapter's module docstring states
  the endpoint and the exact shape its fixture encodes, so a reviewer can diff
  the assumption against a real response in one command. Adapters read fields
  tolerantly (several accepted spellings, objects-or-strings, wrapped-or-bare
  arrays), so a single renamed key degrades one column instead of failing a
  company. Confidence is highest for Greenhouse / Lever / Ashby / SmartRecruiters
  / Workday, whose shapes are widely documented and stable, and is explicitly
  lowest for **Rippling** (1 registry entry), which is noted in its docstring.
- **Attempts made:**
  1. Direct capture from the four main provider APIs - 403 at CONNECT.
  2. Looked for the payloads inside the seed repositories: they publish
     *normalized* listings, not raw ATS responses, so they cannot substitute.
  3. Wrote `scripts/validate_companies.py`, which performs the real fetch through
     the same adapters and rewrites `support_status` + `last_validated`. Running
     it on a normal network both validates the slugs and surfaces any fixture
     that has drifted from reality.
- **Evidence / logs:** see BLK-001's proxy output.
- **Current hypothesis:** shapes are correct for the five major providers;
  Rippling carries real residual risk.
- **Next actions (for the user):** `make validate-companies` then
  `make coverage-report`. Any adapter whose real payload differs will show up
  immediately as `research-needed` with the parse error attached.
- **Severity:** major (bounded: affects 1 company materially; blocks no gate)
- **Status:** accepted-limitation

---
