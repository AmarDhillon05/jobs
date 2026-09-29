# Validation report

Requirement → status → evidence, per PRD §37. Written after re-reading the PRD
section by section.

**Statuses:** `implemented` · `tested` · `passed` · `blocked` · `N/A`

Everything marked `tested` or `passed` has a named test or a command whose output
was observed. Nothing here rests on inspection of the code alone. Where something
was *not* verified, it says so and says why — that is the point of the document.

---

## Verification run

```text
$ make clean && docker compose down -v && make verify
  20/20 stage(s) passed, 0 skipped, total 300s
  gates with passing evidence: A, B, C, D, E, E/F, F, H
  RESULT: PASS
```

Run from a **genuinely clean tree**: caches, `cdk.out`, `build/` and the LocalStack
container and volumes all removed first. That matters — Gate E requires the local
cloud to boot *from scratch*, and an earlier run passed that stage in 0.2s only
because a container was already up. It now takes ~5s because it is really created.

The clean run also earned its keep by finding a bug in the gate runner itself:
`infra-lint` built its file list by globbing `cdk.out` when the *stage list* was
constructed, which on a clean tree is empty — so `cfn-lint` was handed no files and
linted stdin instead, reporting a meaningless failure. It now invokes the make
target, which synthesizes first and expands the glob at run time.

Per-stage logs and `summary.json` in `.verify/`. Stage-by-stage:

| Stage | Gate | Result | Evidence |
| --- | --- | --- | --- |
| `lint` | H | PASS | ruff, all checks passed |
| `format` | H | PASS | 93 files already formatted |
| `types` | H | PASS | mypy, no issues in 48 source files |
| `registry` | B | PASS | `companies.json` regenerates identically |
| `unit` | B | PASS | 354 passed |
| `scrapers` | A | PASS | 1,128 passed, 22 skipped (each names its reason) |
| `integration` | B | PASS | 324 passed |
| `e2e-local` | F | PASS | 40 passed — the eight §30 scenarios, twice over |
| `mobile` | C | PASS | 122 passed (jest-expo + RNTL) |
| `mobile-types` | C | PASS | `tsc --noEmit` clean |
| `mobile-bundle` | C | PASS | `expo export` — 701 modules, 1.78 MB Hermes bytecode |
| `app` | C | PASS | 77 passed (Vitest) |
| `app-build` | C | PASS | `tsc --noEmit && vite build` |
| `app-e2e` | C | PASS | 11 passed in real Chromium |
| `infra-synth` | D | PASS | `cdk synth` |
| `infra-test` | D | PASS | 54 template assertions |
| `infra-lint` | D | PASS | `cfn-lint`, clean |
| `localstack` | E | PASS | boots from `docker compose up` |
| `provision` | E | PASS | tables, queues, topic, 5 Lambdas, event sources, REST API |
| `e2e-aws` | E/F | PASS | 23 passed against emulated AWS, 228s |

Totals: **2,170 automated tests collected** (354 unit + 1,170 scraper + 324
integration + 40 acceptance + 23 architecture + 49 infrastructure + 122 Expo + 77
web + 11 Chromium). A full `make verify` runs **2,128** of them: 20 live-endpoint
tests are deselected and 22 skip, each printing its reason, and none of the 42 is
part of a hard gate. 23 need Docker; **none** needs network access or AWS
credentials.

---

## §1-2 Objective and target roles

| Requirement | Status | Evidence |
| --- | --- | --- |
| ~100-150 companies monitored | deviation (user-directed) | 183 were researched and validated; on 2026-09-28 the user cut polling to the 91 at or above an AWS SDE internship for resume value (92 moved to `excluded` in `data/company_universe.json`, restorable) + 2 blocked; `test_company_model.py::test_monitors_a_focused_but_substantial_set_of_companies` asserts 50-200 |
| 10-minute polling interval | implemented, tested | `poll_interval_minutes=10`; the EventBridge rule's `rate(10 minutes)` is asserted in `infrastructure/tests/test_stack.py` |
| Detect within one interval | implemented, tested | `first_seen` set on first sighting; §30 Scenario 1 in `tests/e2e` and `tests/aws_local` |
| Prefer official company / ATS endpoints | passed | 83/91 read a first-party source; 8 use the community feed, each with its reason in `COMPANY_COVERAGE.md` and BLK-011 |
| Normalize into one schema | tested | `Job` / `JobRecord`; `test_job_model.py`, and the contract suite asserts required fields per provider |
| Filter for relevant technical internships | tested | `tests/unit/test_filtering.py` + a 200-title real-world corpus |
| Persist discovered jobs | tested | `test_repository_contract.py`, run against in-memory *and* DynamoDB under Moto |
| Deduplicate notifications | passed | §30 Scenario 2, twice in `tests/e2e`, again on emulated AWS |
| Email notifications | tested | `test_notifications.py`; SES request shape under Moto |
| Mobile / app push notifications | tested | `test_expo_push.py` (40), `mobile/src/__tests__/notifications.test.ts`. **Delivery to a handset: not tested** — BLK-008 |
| A simple mobile app / installable client | passed | `mobile/` (Expo, 122 tests, bundles) and `web/` (PWA, 88 tests) |
| Direct links to the original posting | tested | preserved byte-for-byte through the pipeline; asserted in §30 Scenario 1 and in both clients' apply-action tests |
| Runnable locally | passed | `make local`, `make serve-api`, `make serve-mobile`; CLI `poll` needs no AWS at all |
| Testable locally | passed | `make test` needs no Docker and no network |
| AWS-ready via IaC | passed | `cdk synth` + 49 assertions + `cfn-lint` |
| No real AWS deployment | N/A — intentionally prohibited | Never run. `tests/conftest.py` forces fake credentials and clears `AWS_ENDPOINT_URL` unless a test opts in |
| Easy to extend with more companies | passed | one JSON entry; a per-company test is generated for it automatically |
| Track scraper health and failures | tested | `ScraperHealth` + `make local-health` + `cli health`, 17 CLI tests |
| Target roles list (§2) | tested | every listed role scores above the notify threshold; `test_filtering.py` parametrizes the whole §2 list |
| Irrelevant categories excluded | tested | same file; the corpus test names any real title that crosses a boundary |
| Prefer keeping a low-scoring job over discarding it | implemented, tested | keep threshold well below notify threshold; asserted for adjacent roles |

## §3-4 Company universe and mandatory seed sources

| Requirement | Status | Evidence |
| --- | --- | --- |
| Emphasis beyond Big Tech | passed | distinct industry labels across the registry; AI labs, quant/market-making, robotics, aerospace, semiconductors, devtools, databases, security and fintech all represented. Breakdown in `COMPANY_COVERAGE.md` |
| Priority tiers `high`/`medium`/`experimental` | implemented, tested | `Priority` enum; drives immediate-vs-grouped alerts, asserted in `test_notifications.py` |
| Start from SimplifyJobs/Summer2027-Internships | passed | `scripts/build_company_registry.py`; 3,177 employers extracted |
| Start from vanshb03/Summer2027-Internships | passed | same script; both feeds, including closed listings |
| Use closed listings too | passed | closed roles are retained for employer discovery; recorded in `source_discovered_from` |
| Extract unique names, de-duplicate aliases | tested | `tests/unit/test_discovery.py` — exact-normalized matching plus an explicit alias table |
| Identify official careers pages | passed | every registry entry has `careers_url` |
| Identify the ATS/provider | passed | provider detected from observed apply URLs; `test_discovery.py` |
| Prefer official endpoints over the feeds | passed | 8/91 read the community feed, because their own sites need tokens, block plain requests, or expose nothing (BLK-011) |
| Reach 100-150 worthwhile companies | deviation (user-directed) | 183 researched; 91 polled after the user's resume-value cut |
| `companies.json` with the documented schema | passed | `Company.from_item` round-trips every field; `make verify` regenerates the file identically |
| Support statuses | passed | `supported` 139, `partial` 11 — each a checkable claim, defined in `COMPANY_COVERAGE.md` |

## §5-7 Source discovery, scraper framework, scraper correctness

| Requirement | Status | Evidence |
| --- | --- | --- |
| Canonical source per company | passed | `provider` + `provider_config` per entry, with a per-company test |
| Preference order honoured | passed | structured JSON APIs first; HTML/JSON-LD only where no API exists; no browser automation was needed |
| Never bypass auth / CAPTCHA / anti-bot / rate limits | passed | 401/403 → `AccessBlocked`, never retried, marked `blocked`: `test_provider_contract.py` asserts it for every adapter |
| No unauthorized cloud accounts | passed | none created |
| Standard provider abstraction | tested | `JobSource` with `fetch_jobs` / `normalize` / `healthcheck` |
| Normalized `Job` model as specified | tested | `test_job_model.py` |
| Reusable adapters, not 183 scrapers | passed | 11 ATS adapters + 5 single-employer adapters + the fallback cover all 91 |
| Required fields validated | tested | contract suite, per provider: company, title, URL, stable id, location, date, description, source |
| URLs syntactically valid | tested | contract suite asserts absolute `https://` |
| Stable provider ids | tested | contract suite: identical fetches produce identical ids and hashes |
| Pagination handled, nothing skipped | tested | contract suite for the 2 paginating providers; the other 7 skip with the reason "returns a whole board in one response", and a separate test asserts exactly one request |
| Malformed records do not crash a source | tested | contract suite + §30 in `tests/e2e` |
| Source duplicates collapsed | tested | contract suite |
| Empty results ≠ failure | tested | contract suite; `ScraperStatus.EMPTY` is `ok` |
| Fixture-based tests per provider | tested | `tests/fixtures/` — the 9 newer providers' fixtures are **trimmed real captures**; the original 7 were authored from documented shapes (BLK-002), and live runs have since checked every one of those adapters against its real API |
| Links resolve to real pages | passed | live run 2026-09-27: a sample job link from every polled company fetched; all resolve (HTTP 200/202), except Citadel, Citadel Securities and Tesla, whose own sites answer a plain request with 403 while serving browsers normally |

## §8-9 Filtering and new-job detection

| Requirement | Status | Evidence |
| --- | --- | --- |
| Filtering separate from scraping | passed | `src/jobmonitor/filtering/` has no HTTP dependency |
| Auto-deploy on push (user request, 2026-09-29) | tested, not run | `.github/workflows/deploy.yml` + `GitHubDeployStack`; `infrastructure/tests/test_github_deploy.py` pins the trust policy, permissions and workflow gates; the CI test job was run end to end in a clean copy. A real GitHub-to-AWS deploy needs the user's one-time setup (README) |
| US-only (user request, 2026-09-29) | tested | `filtering/location.py`; `test_location.py` (82 real location strings from the live boards), `TestUsOnly` in Level 8 on both storage backends |
| Configurable relevance score 0-100 | tested | `test_filtering.py` |
| Configurable notification threshold | tested | `FilterSettings.notify_threshold`, driven from env |
| Permissive toward adjacent technical roles | tested | corpus test; three real bugs it caught are listed in `TESTING.md` |
| Distinguish existing / new / updated / removed | tested | `UpsertResult.is_new` / `is_updated`; `test_repository_contract.py`; §30 Scenario 2's edited-posting case |
| Identity `company + external_id`, hash fallback | tested | `tests/unit/test_identity.py` |
| All persisted fields present | tested | `test_repository_contract.py` asserts every field the PRD lists |
| `first_seen` authoritative | tested | asserted stable across 10 polls, in both repositories |
| An unchanged job never re-notifies | passed | §30 Scenario 2; and the sparse-index bug that broke this is in `TESTING.md`'s caught-bugs list |

## §10-13 Storage, architecture, no-AWS constraint, testing strategy

| Requirement | Status | Evidence |
| --- | --- | --- |
| Serverless-friendly store; DynamoDB default | passed | `ARCHITECTURE.md` §4 |
| Idempotent inserts | tested | single `UpdateItem` with `if_not_exists`; contract suite |
| Lookup by id, recent query, first/last seen, notification state | tested | contract suite |
| Scraper-health storage with TTL | tested | asserted in the CDK template; expiry itself is not emulated (named in §9 gaps) |
| Moto for unit-level AWS | passed | every persistence assertion runs twice |
| LocalStack for higher-level integration | passed | 23 Level-7 tests, real Lambda containers |
| Architecture documented with alternatives | passed | `ARCHITECTURE.md` §2 weighs one-Lambda, Step Functions and SQS fan-out |
| Lambda duration / concurrency / retries / cost evaluated | passed | `ARCHITECTURE.md` §2 and §10 |
| **No real AWS deployment** | N/A — intentionally prohibited | PRD §12. Nothing was created; `.claude/settings.json` additionally denies `cdk deploy`, `terraform apply` and the `aws` CLI |
| botocore `Stubber` for request shape | tested | `test_aws_request_shape.py` — asserts the exact DynamoDB requests |
| LocalStack service coverage | passed | Lambda, DynamoDB, SQS, SNS, API Gateway, CloudWatch Logs |
| Paid-feature limitations documented, not silently claimed | passed | API Gateway v2 is Pro → REST v1 locally (BLK-004); EventBridge scheduler simulated (BLK-005). Both in `ARCHITECTURE.md` §9 |
| CDK synth + assertions + cfn-lint | passed | `make infra-validate` |

## §14 Test pyramid

| Level | Status | Evidence |
| --- | --- | --- |
| 1 — pure units | passed | 354 |
| 2 — scrapers/providers, incl. `500,500,200` and `429+Retry-After` | passed | 1,170 collected; 1,128 run, 22 skipped with reasons, 20 live deselected |
| 3 — persistence | passed | in `tests/integration` (324), run against both repositories |
| 4 — notifications | passed | same suite; payloads, formatters, deep links, failed delivery, suppression |
| 5 — app/client | passed | 122 Expo + 77 web + 11 Chromium. Every item the PRD lists, including "tapping a notification routes to the correct job" and "duplicate events do not create duplicate visible items" |
| 6 — backend integration, all ten cases | passed | `test_pipeline.py`, `test_handlers.py`, and §30 Scenarios 4-7 |
| 7 — local AWS architecture | passed | 23, on emulated AWS |
| 8 — final acceptance/regression | passed | `make verify`, 20/20 |
| At least one architecture test runs a live public scraper | passed (live runs) | `test_deployed_worker_reaches_the_network` exists and proves the deployed worker makes real outbound calls; the *public ATS* half was refused by the egress policy until it was opened (BLK-001, resolved); every provider has since been run live. PRD §14 L7: "completion must not depend on an external site remaining online" |

## §15 Hard completion gates

| Gate | Result | Evidence |
| --- | --- | --- |
| **A** — scraper correctness | PASS | one contract battery over every adapter; a per-company config test for each of the 183; all 183 validated live; failure isolation proven in §30 Scenario 4 |
| **B** — core business logic | PASS | filtering, normalization, fingerprints, persistence, `first_seen`/`last_seen`, dedup, notification suppression — all tested, dedup and suppression twice over |
| **C** — app/client | PASS | both clients build and run; component, data-loading, notification and deep-link tests pass; the Expo bundle is produced by Metro + Hermes |
| **D** — infrastructure | PASS | synth, 49 assertions, cfn-lint; IAM least-privilege asserted per function; no unresolved references |
| **E** — local AWS architecture | PASS | LocalStack boots from scratch, resources provision, the full path succeeds, retry/failure isolation exercised, no AWS credentials needed |
| **F** — full system | PASS | new internship → discovered → normalized → relevant → stored → notified → exposed to the feed; re-run gives **0 duplicate notifications**; a deliberately failed scraper leaves the others running. Proven in process *and* on emulated AWS |
| **G** — final review | PASS | this document; 0 critical blockers open |
| **H** — git | PASS | lint, format, types and tests pass; working tree clean; `build/`, `cdk.out/`, `.verify/`, `node_modules/` ignored |

## §16-17 Failure isolation and retry policy

| Requirement | Status | Evidence |
| --- | --- | --- |
| One provider failure never aborts the run | passed | §30 Scenario 4, in process and on emulated AWS |
| Failure record with attempts / error type / timestamp | tested | `ScraperHealth`; asserted field by field |
| DLQ / failure queues | passed | observed on emulated AWS — the message really does arrive on the DLQ (BLK-006 is how) |
| Bounded retries, exponential backoff with jitter | tested | `test_http_client.py`; full jitter asserted with a seeded RNG |
| `Retry-After` respected and capped | tested | delta-seconds *and* HTTP-date forms; capped at 300s |
| Timeouts / resets / DNS / 429 / 5xx handled | tested | contract suite, all nine adapters |
| Do not hammer sites | passed | `Retry-After` always wins over our own backoff; asserted |

## §18 Blocker protocol

| Requirement | Status | Evidence |
| --- | --- | --- |
| `BLOCKERS.md` with the full entry template | passed | 9 entries, every field filled |
| Three-attempt rule | passed | BLK-003 records three materially different approaches before the one that worked; BLK-001 records three and then *stopped*, because the proxy README says not to route around it |
| Backtracking / fault-tree reasoning | passed | BLK-006's three options are the acceptance-criterion-backwards trace; the `content_hash`/`date_posted` bug in `TESTING.md` is the worked example |
| Alternative implementation branch | passed | BLK-005 is exactly the §18.4 case, handled exactly as §18.4 prescribes |
| Blocker sweep before completion | passed | dated sweep at the end of `BLOCKERS.md`; BLK-001 re-probed and still failing |
| No critical blocker left open | passed | **0 critical open.** The only critical one (BLK-003) is resolved |

## §19-21, §26-29 Process, config, cost, structure

| Requirement | Status | Evidence |
| --- | --- | --- |
| Frequent coherent commits, none deferred to the end | passed | 16 commits across phases A-N; the first was made in Phase A, before any feature code |
| History not rewritten to look tidy | passed | one amend, to fix an HTML-escaped author email |
| `make setup/test/test-app/infra-validate/local/e2e/verify` | passed | all present and documented in the README |
| `.env.example`, nothing secret committed | passed | every variable documented; no keys, tokens or credentials anywhere in the tree |
| Tests supply fake values | passed | `for_tests()` plus a conftest rail that forces fake AWS credentials |
| IaC framework chosen and justified | passed | CDK (Python), `ARCHITECTURE.md` §7 |
| Least-privilege IAM | tested | per-function policies asserted in the template tests |
| Cost target $0-10/month | passed | ≈ $4.70/month, itemised in `ARCHITECTURE.md` §10 |
| Repository structure | passed | as the PRD suggests, with the deviations listed in `ARCHITECTURE.md` §11 |

## §22-25 Notifications, client, API, observability

| Requirement | Status | Evidence |
| --- | --- | --- |
| Email required in the production design | passed | SES transport; request shape asserted under Moto |
| Local tests use a mock sink | passed | memory and console transports |
| PRD's email layout | tested | `test_notifications.py` asserts the example layout line by line |
| Immediate for high priority, grouped for the rest | tested | `Notifier.plan`; asserted both ways. With ntfy every job is its own alert (its buttons act on one link); high priority still rings louder |
| Per-job vs batched email (the PRD leaves it to the agent) | tested | user's choice, 2026-09-28: instant push + an hourly email digest that is skipped when empty; `test_digest.py` (22, incl. Moto), Level 8 Scenarios 1-2, LocalStack `TestHourlyDigest` |
| A practical push path | passed | ntfy (recommended; `test_ntfy_push.py`), Expo to the phone app, SNS dependency-free, Web Push to the PWA |
| Push provider abstracted for a fake transport | passed | `PushTransport` + `MemoryPushTransport`; six implementations behind one interface |
| Recent-jobs screen with the listed fields | tested | both clients |
| Job-detail screen with the listed fields | tested | both clients; Level 8 asserts the API actually serves every field the screen renders |
| "Open Application" opens the original page | tested | both clients; the URL is asserted unchanged, query string included |
| No AWS credentials reach the client | passed | the client only ever calls the API, never AWS; and `test_api.py` asserts a registered device's push token is not echoed back in the response |
| `GET /jobs`, `/jobs/{id}`, `/jobs/recent`, `POST /devices/register` | tested | `test_api.py` |
| Write paths protected | tested | constant-time shared token, **fails closed** when unconfigured; asserted |
| Every §25 metric tracked | tested | `PollSummary`; `test_pipeline.py` asserts each counter |
| Structured logs in the documented shape | tested | asserted key by key |
| A scraper-health view | passed | `cli health` — table and JSON, 17 CLI tests |

## §30 Acceptance scenarios

Each one runs in process against **both** storage implementations; the five that
can be are *also* run on emulated AWS through real Lambdas and real queues.

| Scenario | In process | On emulated AWS |
| --- | --- | --- |
| 1 — new job, end to end | PASS | PASS |
| 2 — same poll again: 0 duplicates, `first_seen` unmoved | PASS | PASS |
| 3 — 10 existing + 1 new → 1 alert, 11 stored | PASS | PASS |
| 4 — one scraper fails, the others continue | PASS | PASS |
| 5 — `500, 500, 200` → recovery | PASS | (unit-level; scripted transport) |
| 6 — `429` → controlled backoff | PASS | (unit-level; scripted transport) |
| 7 — worker fails → retry → DLQ | PASS (batch-failure contract) | PASS (message observed on the DLQ) |
| 8 — app notification path and deep link | PASS (payload → API join) | — (client half is in `mobile/` and `web/`) |

## §31-32 Live validation and coverage reporting

| Requirement | Status | Evidence |
| --- | --- | --- |
| Validate selected real public endpoints | passed | **every** polled company, 2026-09-27: 183/183 answered; verdicts in `data/validation.json` - 173 supported, 10 partial (the fallback feed), 0 failing |
| Record the date of live validation | passed | per company in `data/validation.json` and `companies.json` (`last_validated`); summarised in `COMPANY_COVERAGE.md` |
| Do not make the suite depend on external sites | passed | `-m live` is opt-in and gates nothing |
| Coverage report with provider breakdown | passed | `COMPANY_COVERAGE.md`, generated from `companies.json` |
| Do not claim `supported` without a passing test | passed | `supported` is defined as four automated checks; `test_registry_configs.py` generates one test per company |

## §33 Auto-apply

| Requirement | Status |
| --- | --- |
| Optional phase, not to be prioritized | N/A — **not built**, by design. No profile storage, no form filling, no submission. Monitoring's gates come first, and the PRD says so |

## §35 Prohibited shortcuts — self-audit

| Shortcut | Avoided? |
| --- | --- |
| Declaring done because files exist | Yes — `make verify` is the gate, and it reports PARTIAL if a stage is skipped |
| Unit tests alone | No — levels 1-8 all run |
| Mocks alone | No — Moto *and* LocalStack *and* real containers *and* a real browser |
| One scraper working | No — every adapter shares one contract battery, each of the 183 configs has its own test, and all were run live |
| IaC that merely exists | No — synth, 49 assertions, cfn-lint, and the same table schemas provisioned locally |
| LocalStack starting but the architecture not exercised | No — 23 tests drive the full path, including DLQ redrive |
| App rendering without notification tests | No — tap routing, cold-start replay and duplicate suppression are all tested |
| Fixtures working but fields unvalidated | No — the contract suite asserts the whole §7 field list |
| Commenting out or silently skipping failing tests | No — 22 skips, every one printing its reason, none in a hard gate |
| Assuming AWS will work | No — and where something genuinely is not proven (handset delivery, live ATS endpoints, TTL expiry, v2 event shape at runtime) it is written down as not proven |

---

## What is not verified

Stated plainly, because the rest of this document is a list of things that are.

1. **A push actually arriving on a handset.** No simulator here, and a simulator
   cannot mint a push token. Everything up to the request to `exp.host` is tested.
   BLK-008; steps for the user in `mobile/README.md`.
2. **That every endpoint answers *tomorrow*.** All 183 answered on 2026-09-27;
   sites move (8 had, BLK-010). `make validate-companies` re-checks in one command.
3. **Undated sources.** IBM, Arm (TalentBrew), Rippling and two Workday tenants
   expose no posting date, so the one-day window cannot judge their age; they are
   kept and the seen-before check stops repeat alerts.
4. **Rate limits under production polling.** Microsoft's Eightfold tenant answered
   429 after ~20-30 requests; its search terms were trimmed to ~22 requests a poll,
   but whether that clears its limit every 10 minutes is only provable by running.
5. **The EventBridge rule firing.** One API call wide, simulated per PRD §18.4.
   BLK-005.
6. **API Gateway v2's runtime event shape.** v2 is LocalStack Pro; REST v1 is used
   locally, and the v2 envelope is asserted in unit tests instead. BLK-004.
7. **DynamoDB TTL expiry.** Configured and asserted in the template; real expiry
   takes up to 48 h and is not emulated. Affects health-record cleanup only.
8. **Anything at all on real AWS.** Intentionally prohibited (PRD §12).
