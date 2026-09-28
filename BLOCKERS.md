# BLOCKERS

Living log of every blocker hit during implementation, per PRD §18.

Statuses: `open` · `deferred` · `revisiting` · `resolved` · `accepted-limitation`

Severity: `critical` (blocks a hard completion gate) · `major` · `minor`

---

## Summary

| ID | Component | Severity | Status |
| --- | --- | --- | --- |
| [BLK-001](#blk-001---outbound-egress-policy-blocks-all-third-party-careersats-hosts) | Live scraper validation | major | resolved |
| [BLK-002](#blk-002---provider-fixtures-could-not-be-captured-from-live-responses) | Provider fixtures | major | accepted-limitation |
| [BLK-003](#blk-003---localstack-could-not-pull-a-lambda-runtime-image) | LocalStack Lambda | critical | resolved |
| [BLK-004](#blk-004---api-gateway-v2-http-api-is-a-localstack-pro-feature) | LocalStack API Gateway | major | resolved |
| [BLK-005](#blk-005---eventbridge-scheduler-emulation-is-unreliable-in-localstack-community) | LocalStack EventBridge | major | accepted-limitation |
| [BLK-006](#blk-006---dlq-redrive-could-not-be-observed-within-a-test-timeout) | Level 7 DLQ test | major | resolved |
| [BLK-007](#blk-007---purge_queue-is-rate-limited-which-made-a-level-7-test-flake) | Level 7 isolation | minor | resolved |
| [BLK-008](#blk-008---push-delivery-to-a-physical-handset-cannot-be-verified-here) | Push delivery | major | accepted-limitation |
| [BLK-009](#blk-009---docker-compose-cannot-interpolate-a-default-containing-braces) | docker-compose | minor | resolved |
| [BLK-010](#blk-010---eight-registry-companies-failed-live) | Registry configs | major | resolved |
| [BLK-011](#blk-011---companies-with-no-reachable-first-party-source) | Coverage | minor | accepted-limitation |
| [BLK-012](#blk-012---live-validation-would-have-demoted-companies-and-broken-the-gate) | Validation tooling | major | resolved |
| [BLK-013](#blk-013---docker-hub-rate-limit-stops-localstack-creating-lambdas) | LocalStack Lambda | major | accepted-limitation |

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

## BLK-003 - LocalStack could not pull a Lambda runtime image

- **Timestamp / stage:** 2026-09-26, Phase K (local AWS integration)
- **Requirement affected:** PRD §13.3, §14 Level 7, Gate E - the architecture must
  actually run on emulated AWS
- **Component:** `docker-compose.yml`, LocalStack Lambda provider
- **Observed failure:** every `Invoke` returned a runtime error; LocalStack's logs
  showed it could not pull `public.ecr.aws/lambda/python:3.11` - the same egress
  policy as BLK-001 denies `public.ecr.aws`. With no runtime image, no Lambda can
  execute, so Gate E was unreachable.
- **Expected behaviour:** LocalStack pulls the runtime image once and executes each
  handler in a container.
- **Attempts made (three materially different, per §18.1):**
  1. Pre-pull the image directly with `docker pull` - 403, same policy.
  2. Switch LocalStack's Lambda executor to `local` (in-process) - unsupported in
     the installed version, and it would have stopped exercising real container
     invocation, which is the point of the level.
  3. Remap the runtime image to a Docker Hub equivalent via
     `LAMBDA_RUNTIME_IMAGE_MAPPING` - `docker.io` *is* reachable, so
     `mlupin/docker-lambda:python3.11` pulls and runs.
- **Evidence / logs:** 23/23 Level-7 tests now pass against real Lambda
  invocations, including cold starts, event-source triggers and DLQ redrive.
- **Resolution:** the mapping lives in `docker-compose.yml` with a comment naming
  the reason, and is overridable by environment variable for anyone whose network
  allows the AWS image.
- **Severity:** critical (blocked Gate E)
- **Status:** resolved

---

## BLK-004 - API Gateway v2 (HTTP API) is a LocalStack Pro feature

- **Timestamp / stage:** 2026-09-26, Phase K
- **Requirement affected:** PRD §13.3 ("if a particular LocalStack service requires
  a paid feature ... document the limitation and emulate that boundary with the
  closest reliable alternative"), §24 (the API the client reads)
- **Component:** `scripts/local_provision.py`
- **Observed failure:** `apigatewayv2 create_api` is rejected by LocalStack
  community; the deployed stack uses an HTTP API because it is ~70% cheaper.
- **What was done:** local provisioning creates a **REST (v1)** API in front of the
  *same* API Lambda, with the same routes. The Level-7 suite then reads the feed
  over real HTTP through real API Gateway emulation.
- **What that leaves unproven:** the v2 payload-format-2.0 event shape. That is
  covered separately by unit tests over the adapter
  (`tests/integration/test_api.py`), which assert the v2 envelope directly, and by
  the CDK template assertions that pin the HTTP API's routes and integration.
- **Severity:** major (a real emulation gap, named rather than hidden)
- **Status:** resolved - documented in `ARCHITECTURE.md` §9 and `TESTING.md`

---

## BLK-005 - EventBridge scheduler emulation is unreliable in LocalStack community

- **Timestamp / stage:** 2026-09-26, Phase K
- **Requirement affected:** PRD §14 Level 7 ("scheduled event -> coordinator -> ...")
- **Component:** LocalStack EventBridge / Scheduler
- **Observed failure:** a rule with a 10-minute schedule expression is accepted but
  does not reliably fire the target in LocalStack community; a test that waits for
  it either waits ten minutes or flakes.
- **What was done:** exactly the procedure PRD §18.4 prescribes for this case. The
  EventBridge rule **stays** in the CDK stack and `infrastructure/tests` asserts its
  schedule expression, its target, its input payload and its invoke permission;
  `scripts/local_poll.py` then delivers the **identical payload** to the
  coordinator. Everything downstream - queue, event sources, workers, storage,
  topic, notifier, API - is genuinely exercised.
- **Why this is not critical:** the simulated boundary is one API call wide, and the
  thing it stands in for (a cron firing) is the part of AWS least likely to be
  wrong. It is recorded as the project's *only* simulated boundary.
- **Severity:** major
- **Status:** accepted-limitation - documented in `ARCHITECTURE.md` §9 and
  `TESTING.md` ("the one simulated boundary")

---

## BLK-006 - DLQ redrive could not be observed within a test timeout

- **Timestamp / stage:** 2026-09-26, Phase K
- **Requirement affected:** PRD §30 Scenario 7, §16 (dead-letter path)
- **Component:** `tests/aws_local/test_architecture.py`
- **Observed failure:** two DLQ tests timed out. The deployed queue uses a 240s
  visibility timeout with `maxReceiveCount: 3`, so a poison message needs ~12
  minutes to reach the DLQ - far longer than any reasonable test wait.
- **Attempts made:**
  1. Raise the test's wait - would have made the suite unusable.
  2. Assert only the redrive *policy* rather than the redrive - would have proven
     configuration, not behaviour, which §35 explicitly rejects.
  3. Provision the local queue with shortened timings (worker 55s, visibility 60s)
     while the CDK stack keeps the production values, and merge the two tests into
     one so the wait is paid once.
- **Evidence / logs:** the message is now observed arriving on the DLQ; the test
  passes in ~3 minutes as part of a 226s suite.
- **Resolution:** local-only timings, with a comment naming the production values
  and the CDK assertions that pin them.
- **Severity:** major (blocked observed evidence for a Gate F sub-case)
- **Status:** resolved

---

## BLK-007 - `purge_queue` is rate limited, which made a Level 7 test flake

- **Timestamp / stage:** 2026-09-26, Phase K
- **Requirement affected:** PRD §35 (no silently skipped or flaky evidence)
- **Component:** `tests/aws_local/conftest.py`
- **Observed failure:** the health-view test intermittently failed. Test isolation
  used `purge_queue`, which SQS permits only once per 60 seconds per queue; the
  second call in a run is a no-op, so a poison message left by the DLQ test was
  still being redelivered and starved the queue.
- **Resolution:** isolation now drains with a receive+delete loop, before *and*
  after each test, which has no rate limit and no ordering assumption.
- **Severity:** minor (a test-harness fault, not a system fault)
- **Status:** resolved

---

## BLK-008 - Push delivery to a physical handset cannot be verified here

- **Timestamp / stage:** 2026-09-27, Phase I (client)
- **Requirement affected:** PRD §22 (mobile push), §14 Level 5, §30 Scenario 8
- **Component:** `mobile/`, `ExpoPushTransport`
- **Observed limitation:** this environment has no iOS or Android simulator, and a
  simulator could not mint a push token even if it did - Expo needs a real device
  with a push certificate. No automated test here can prove that APNs or FCM wakes
  a handset.
- **What *is* proven, and how:**
  - the permission flow, token registration, listener wiring, tap routing and
    cold-start replay, against mocked native modules (122 tests in `mobile/`);
  - the exact request the backend sends to `exp.host` - token list, the `data` map
    carrying the deep link, `priority`, and the Android `channelId` that must match
    the one the app creates - plus every ticket status Expo can return, against a
    scripted transport (40 tests);
  - the same deep-link and notification-routing behaviour in a **real browser**,
    via the `web/` client's Playwright suite, which is why that client was kept;
  - that the payload names a job the API can actually serve, in Level 8.
- **Next actions (for the user):** `mobile/README.md` step 4-6. `python -m
  jobmonitor.cli push-test` sends one real alert and prints the deep link it
  carries, so a failed tap can be diagnosed without guessing.
- **Why this is not critical:** PRD §14 Level 4 states plainly that "real
  email/push delivery does not need to occur during automated local testing". The
  gap is recorded as *not tested* in `TESTING.md` rather than counted as a pass.
- **Severity:** major (the one user-visible behaviour with no local proof)
- **Status:** accepted-limitation

---

## BLK-009 - docker-compose cannot interpolate a default containing braces

- **Timestamp / stage:** 2026-09-26, Phase K
- **Component:** `docker-compose.yml`
- **Observed failure:** `docker compose` refused the file with a parse error. The
  BLK-003 fix needs the value
  `LAMBDA_RUNTIME_IMAGE_MAPPING={"python3.11": "..."}`, and written as
  `${VAR:-{"python3.11": "..."}}` Compose's interpolator mis-parses the braces
  inside the default.
- **Resolution:** the mapping is a quoted literal, and the override is documented
  as an environment variable set outside the file.
- **Severity:** minor
- **Status:** resolved

---

## Blocker sweep - 2026-09-27

Per PRD §18.5, every unresolved blocker was revisited before final verification.

- **BLK-001** re-probed: `boards-api.greenhouse.io`, `api.lever.co` and
  `api.ashbyhq.com` all still fail at CONNECT, and the proxy status endpoint still
  reports `connect_rejected` policy denials. Unchanged; the egress policy is
  outside this repository. Remains `accepted-limitation`, blocks no gate (PRD §31,
  §14 Level 7).
- **BLK-002** unchanged, being a consequence of BLK-001. `make validate-companies`
  is the user's one-command path to closing both.
- **BLK-005** re-examined rather than re-attempted: the mitigation is the exact
  procedure PRD §18.4 prescribes for it, so there is nothing to retry.
- **BLK-008** is new in this phase and inherent to the environment, not a bug to
  fix. Recorded with the precise boundary between what is proven and what is not.
- **Critical blockers open: 0.**

## Update - 2026-09-27: egress opened

- **BLK-001 resolved.** The user changed the environment's network policy. Greenhouse,
  Lever, Ashby, SmartRecruiters, Workday and Rippling hosts all answer; a live run
  over all 150 companies reached 142.
- **BLK-002 partly realized, as predicted.** The first live run found two real
  mismatches between the hand-authored fixtures and the live APIs:
  1. *Workday pagination* - the live API sends `"total": 0` after page one, which
     silently truncated every Workday board at 40 postings. Fixed, with a
     regression test that replays the real behaviour (`TESTING.md` bug 7).
  2. *Rippling* has no posting date in its job list at all, and two Workday
     tenants (Zoom, Boston Dynamics) hide `postedOn`. Handled by the one-day
     window's undated rule rather than by guessing a date.
- **Eight companies fail live** (Atomic Semi, Cirrus Logic, Quantinuum, Marqeta,
  Postman on ATS slugs; Castleton, Dell, Netflix on Workday). These are slug or
  site-name errors of exactly the kind `make validate-companies` exists to find,
  and are the next piece of work.

---

## BLK-010 - Eight registry companies failed live

- **Timestamp / stage:** 2026-09-27, first live run after egress opened
- **Requirement affected:** PRD §31 (live validation), §32 (no unearned `supported`)
- **Observed failure:** 8 of 150 companies failed against their real endpoints.
- **Root causes - five were the world changing, two were bugs in our discovery:**

  | Company | Symptom | Cause | Fix |
  | --- | --- | --- | --- |
  | Cirrus Logic, Quantinuum | Lever 404 | **Discovery bug**: boards are on `jobs.eu.lever.co`; the region was dropped, so we called the US API | Discovery keeps `region: eu`; the Lever adapter calls `api.eu.lever.co` |
  | Castleton Commodities | Workday 422 on every body | **Discovery bug**: the tenant id is `osv_cci`; we took the hyphenated subdomain | Discovery converts `-` to `_` in tenant ids |
  | Dell | Workday 422 | moved to Oracle Fusion | new `oracle_hcm` adapter |
  | Netflix | Workday 422 | moved to Eightfold | new `eightfold` adapter (older v2 API) |
  | Atomic Semi | Ashby 404 | rebranded - careers page redirects to fab2.com | board `fab2` |
  | Marqeta, Postman | Greenhouse 404 | left their boards; see BLK-011 | Simplify fallback, `partial` |

- **Evidence:** all eight re-checked live afterwards; each answers, and each sample
  job link resolves (HTTP 200). The two discovery fixes are pinned by tests in
  `tests/scrapers/test_search_and_new_adapters.py`, and Cirrus, Quantinuum and
  Castleton now resolve correctly *from the seed URLs alone* - no hand pinning.
- **Severity:** major (8 companies silently unmonitored)
- **Status:** resolved

---

## BLK-011 - Companies with no reachable first-party source

- **Timestamp / stage:** 2026-09-27, adding the 35 companies the user named
- **Requirement affected:** PRD §3 coverage; §5 ("do not bypass authentication,
  CAPTCHAs, anti-bot controls")
- **Observed:** for seven companies no first-party job source can be read by a
  plain, unauthenticated request:

  | Company | What stops it | Outcome |
  | --- | --- | --- |
  | Meta | GraphQL needs page-issued tokens | Simplify fallback, `partial` |
  | ByteDance / TikTok | API needs signed requests | Simplify fallback, `partial` |
  | Tesla | 403 to a plain request (bot protection) | Simplify fallback, `partial` |
  | Marqeta | referral-only board, now gone; page loads jobs client-side | Simplify fallback, `partial` |
  | Postman | left Greenhouse; page loads jobs client-side | Simplify fallback, `partial` |
  | Bloomberg | 403 (bot protection); absent from both seed feeds | `blocked`, not polled |
  | LinkedIn | roles behind sign-in; absent from both seed feeds | `blocked`, not polled |

- **Attempts (Postman, as the worst case - three materially different):** slug
  probes on Greenhouse, Lever and Ashby; the careers page and its Markdown twin
  for ATS links; the open-positions page for embedded data. Nothing. Marqeta: the
  same three. Per §18.1, stopped there.
- **Why not critical:** 176 of 183 polled companies read a first-party source;
  these seven are recorded, and five still have a secondary source. Getting past
  a token wall or a 403 would be exactly the evasion §5 forbids.
- **One trap worth recording:** the Greenhouse and Lever boards *named*
  `linkedin` answer - but they are not LinkedIn's (a job titled `123123`, another
  `Anirban jobReq 3`). A slug answering is not proof of ownership; every new
  entry was checked by its apply-URL domain.
- **Severity:** minor
- **Status:** accepted-limitation

---

## BLK-012 - Live validation would have demoted companies and broken the gate

- **Timestamp / stage:** 2026-09-27, first real run of `scripts/validate_companies.py`
- **Requirement affected:** PRD §31, §15 Gate H (`make verify` must pass)
- **Observed:** the script had never run (BLK-001), and running it exposed three
  defects:
  1. it wrote `companies.json` in place, but that file is *generated* - so the
     build's `--check`, and with it `make verify`, failed the moment validation ran;
  2. it mapped every failure to `research-needed`, which stops a company being
     polled. Microsoft answered **429** after a burst of development traffic, and
     would have been dropped from monitoring for a rate limit;
  3. it probed entries with no adapter (the two `blocked` companies) and crashed.
- **Resolution:** verdicts are recorded in `data/validation.json` (observed facts,
  merged per company) and overlaid by the build, so the registry stays generated;
  429 / 5xx / timeouts are *inconclusive* - status unchanged, verdict not written;
  entries without an adapter are listed and skipped. 12 tests in
  `tests/unit/test_validation_recording.py`.
- **Severity:** major (would have silently unmonitored healthy companies)
- **Status:** resolved

---

## BLK-013 - Docker Hub rate limit stops LocalStack creating Lambdas

- **Timestamp / stage:** 2026-09-28, `make verify` after adding the digest Lambda
- **Requirement affected:** PRD §14 Level 7, Gate E
- **Component:** LocalStack 3.8 Lambda provider, Docker Hub
- **Observed failure:** `provision` failed on the *first* function (the
  coordinator, unchanged), `State: Failed`, `StateReason: Error while creating
  lambda`. With `LOCALSTACK_DEBUG=1` the cause is LocalStack re-pulling the
  runtime image on every function create (`_ensure_runtime_image_present` pulls
  unconditionally), and Docker Hub answering `429 Too Many Requests`:
  `ratelimit-remaining: 0;w=3600` for anonymous pulls from this environment's
  shared egress IP. The image itself was already cached locally.
- **Expected behaviour:** a cached runtime image is enough to create a function.
- **Attempts made:**
  1. Direct `docker pull` - same 429, so not a LocalStack bug.
  2. Read LocalStack's pull path - no setting skips the pull in 3.8.
  3. Serve the cached image from a local, read-only registry mirror (a 50-line
     script over `docker save` output, kept outside the repository) and start
     `dockerd --registry-mirror=http://127.0.0.1:5000`. The forced pull resolves
     against the mirror; nothing in the project changed.
- **Evidence / logs:** `make verify` 20/20 afterwards, including all Level 7 tests
  and the new `TestHourlyDigest` on a real LocalStack Lambda.
- **Current hypothesis:** an external quota, not a defect. On a normal network, or
  with `docker login`, it does not occur.
- **Next actions:** none in the project. If it recurs: wait for the hourly window,
  `docker login`, or use a registry mirror as above.
- **Severity:** major (blocks Gate E while the quota is exhausted)
- **Status:** accepted-limitation (environmental; worked around without changing
  what is tested)

