# Testing

What is tested, at which level, and — the part that usually goes unsaid — **what
is real, what is mocked, and what is emulated**.

Run everything with `make verify`. Run the fast part with `make test`.

---

## The pyramid

| Level | What | Where | Count | Command |
| --- | --- | --- | --- | --- |
| 1 | Pure units: normalization, filtering, identity, canonical URLs, scoring, retry/backoff maths, config | `tests/unit/` | 354 | `make test-unit` |
| 2 | Providers: parsing, required fields, pagination, malformed records, retries, rate limits, per-company configs | `tests/scrapers/` | 1,170 | `make test-scrapers` |
| 3 | Persistence: insert, reinsert, `first_seen` stability, `last_seen`, notification state, idempotency | `tests/integration/` | 324 total | `make test-integration` |
| 4 | Notifications: payloads, formatters, deep links, failed delivery, duplicate suppression, the Expo push transport | `tests/integration/` | (same 324) | `make test-integration` |
| 5a | Expo app: render, data loading, empty/error states, detail, apply action, push registration, tap routing | `mobile/src/__tests__/` | 122 | `make test-mobile` |
| 5b | Web client: the same list, in jsdom | `web/src/__tests__/` | 77 | `make test-web` |
| 5c | Web client in a real browser | `web/e2e/` | 11 | `make test-app-e2e` |
| 6 | Backend pipeline: the ten required cases, handlers, queue semantics, API, CLI | `tests/integration/` | (same 324) | `make test-integration` |
| 7 | The architecture on emulated AWS | `tests/aws_local/` | 23 | `make e2e-aws` |
| D | Infrastructure: synth, template assertions, cfn-lint | `infrastructure/tests/` | 49 | `make infra-validate` |
| 8 | The eight PRD §30 acceptance scenarios, end to end | `tests/e2e/` | 40 | `make e2e-local` |

Markers: `unit`, `scrapers`, `integration`, `aws_local`, `e2e`, `infra`, `live`.
`make test` excludes `aws_local` and `live`, so the default run needs no Docker and
no network.

## Real vs. mocked vs. emulated

This is the table to read if you are deciding how much to trust a claim.

| Thing | How it is tested | Real? |
| --- | --- | --- |
| Parsing, filtering, identity, scoring | Direct calls | **Real code, real logic** |
| HTTP retry / backoff / `Retry-After` | Injected scripted transport | Real client logic, **fake network** |
| Provider response shapes | Saved fixtures | Real parser, **hand-authored payloads** (see BLK-002) |
| Live ATS endpoints | `-m live`, opt-in | **Blocked in this environment** (BLK-001) |
| DynamoDB behaviour | Moto, and the same suite in-memory | **Real semantics**, emulated service |
| DynamoDB request shape | botocore `Stubber` | **Real requests**, asserted byte-for-byte |
| SQS / SNS / SES behaviour | Moto | Emulated service |
| DLQ redrive | Moto, and LocalStack | **Observed**, not assumed |
| Lambda execution | LocalStack, real containers | **Real invocation**, emulated service |
| SQS → Lambda event source | LocalStack | **Real trigger**, emulated service |
| API Gateway → Lambda → DynamoDB | LocalStack, over HTTP | Real path, **REST (v1) locally vs HTTP (v2) deployed** |
| EventBridge schedule | Payload injected | **Simulated boundary** — see below |
| IAM least privilege | Asserted on the synthesized template | **Not enforced** locally |
| CloudFormation validity | `cdk synth` + `cfn-lint` | **Real synthesis** |
| Client rendering / routing | jsdom + Testing Library (web), jest-expo + RNTL (app) | Real components |
| Web client deep links | Playwright, real Chromium | **Real browser** |
| Expo app native modules | `expo-notifications` / `expo-device` mocked | **Real app code, mocked native layer** |
| Expo app bundling | `expo export` — Metro + Hermes | **Real bundle**, 701 modules |
| Email delivery | Memory/console sinks; SES shape under Moto | **Never sent** |
| Web Push delivery | Payload tested, transport faked | **Never sent** |
| Expo push request | Scripted transport; request body and every ticket status asserted | **Real request shape**, no network |
| Expo push *delivery to a handset* | — | **Not tested** — needs a physical device; steps in `mobile/README.md` |
| Real AWS | — | **Never deployed** (PRD §12) |

### The one simulated boundary

LocalStack community's EventBridge-scheduler emulation is unreliable. Per PRD
§18.4, rather than abandon EventBridge compatibility:

- the EventBridge rule stays in the CDK stack, and `infrastructure/tests` asserts
  its schedule expression, target, input payload and invoke permission;
- `scripts/local_poll.py` delivers the **identical payload** to the coordinator;
- everything downstream of the coordinator — queue, event sources, workers,
  storage, topic, notifier, API — is genuinely exercised.

Nothing else in the architecture is stood in for. Full ledger in
[`ARCHITECTURE.md` §9](ARCHITECTURE.md#local-emulation-gaps).

## Ideas worth knowing about

**The transport is injected, so the network is scriptable.** `HttpClient` takes its
transport, its `sleep` and its RNG as constructor arguments. A `500, 500, 200`
recovery and a `429 + Retry-After: 2` are therefore ordinary unit tests with no
mocking library, no sockets and no real waiting — and the same battery runs against
all nine adapters.

**One contract, nine adapters.** `test_provider_contract.py` is parametrized over
every provider and asserts the whole PRD §7 list uniformly: required fields, valid
absolute URLs, stable ids, pagination with nothing skipped, malformed records
contained, source duplicates collapsed, empty results not a failure, 5xx recovery,
429 backoff, and 401/403 as blocked-and-never-retried. A tenth adapter cannot ship
with weaker guarantees than the other nine.

**One contract, two repositories.** Every persistence assertion runs twice — once
in-memory, once against DynamoDB under Moto. That is what licenses the fast
in-memory store as the default path: it is held to the same contract as the thing
that ships.

**`supported` is a tested claim, not an opinion.** `test_registry_configs.py`
generates a test **per company**: build the adapter from that company's own config,
check the request is an absolute HTTPS URL against the right API host and embeds
that company's identifiers (which catches a copy-pasted slug), and replay the
provider fixture through it. The precise definition is in
[`COMPANY_COVERAGE.md`](COMPANY_COVERAGE.md).

**The push to a phone is tested up to the last hop, and the gap is named.** The
Expo transport talks to the project's own `HttpClient`, so `tests/integration/
test_expo_push.py` scripts exactly what `exp.host` replies and asserts the request
Expo would have received — the token list, the `data` map carrying the deep link,
`priority`, the Android `channelId` that must match the one the app creates — plus
every ticket status Expo can return. `DeviceNotRegistered` retires the device
instead of being retried, because one uninstalled phone would otherwise wedge every
later alert behind it. What no test here can show is APNs or FCM waking a handset;
that is a physical-device step, written up in `mobile/README.md` and listed as
**not tested** in the table above rather than glossed as a pass.

**Real-world data as a regression corpus.** `tests/fixtures/filter_corpus.json`
holds 200 real internship titles sampled from the seed repositories with the
verdict the filter gives each. Tuning a weight names every real title that crossed
the keep/notify boundary, instead of changing behaviour silently. Regenerate
deliberately with `python scripts/build_filter_corpus.py --write`.

**The zero-dependency claim is enforced.** A test AST-walks every module in the
Lambda bundle and asserts each import root is stdlib, boto3, botocore or urllib3.
That property is what lets `cdk synth` run with no Docker — so it gets a test
rather than a comment.

**Tests cannot reach real AWS.** `tests/conftest.py` forces fake credentials for
every test and clears `AWS_ENDPOINT_URL` unless a test opts in via the `aws_local`
marker. This environment has ambient AWS credentials; that rail is why they are
harmless.

**The deterministic `fixture` provider.** The Level-7 suite needs a source that
cannot fail for reasons unrelated to the architecture, which PRD §14 expressly
permits. Its postings are configured inline, so a fixture company travels to the
worker inside the ordinary `ScrapeTask` message — no `companies.json` entry and no
test-only branch in the deployed handler. A test asserts no real registry entry
uses it, and `test_deployed_worker_reaches_the_network` separately proves the
deployed worker does make real outbound calls.

## Bugs these tests actually caught

Worth listing, because it is the best evidence the suite is load-bearing rather
than decorative.

1. **`content_hash` included `date_posted`.** Workday reports relative text
   ("Posted 5 Days Ago"), so the derived date moved daily and marked all 44 Workday
   jobs "updated" on every poll — 44 phantom events per 10 minutes. Found by the
   contract battery's "identical fetches produce identical hashes" test.
2. **The sparse index resurrected notified jobs.** `notification_pending =
   if_not_exists(...)` re-added the marker on the next sighting, putting
   already-notified jobs back on the pending queue — a re-alert every 10 minutes,
   forever. Found while writing the persistence contract.
3. **Substring matching invented internships.** "Internal Audit" and
   "International Operations" matched `intern`. Found by running the filter over
   10,852 real titles.
4. **A non-technical *team* discarded a technical *role*.** "Data Science Intern —
   Influencer Marketing AI" was dropped. Same real-data run.
5. **`student` was missing entirely**, dropping "Software Development Student".
   Same run.
6. **The Playwright route stub swallowed the app's own navigation.** A `**/jobs/*`
   glob matched the document request for `/jobs/<id>`, serving JSON instead of the
   HTML app — breaking precisely the deep-link case the spec exists to test.
7. **Every Workday board was silently cut off at 40 postings.** Workday reports the
   real total on the first page only and `"total": 0` on every later page; the
   adapter overwrote the total, so `seen >= total` held after page two. NVIDIA
   declares 1,010 matches and we read 40. Found by the first **live** run once
   egress opened - the hand-authored fixture (BLK-002) had the same `total` on
   every page, so no fixture test could see it. Fixing it recovered ~9,900
   postings across the 44 Workday companies. The regression test replays the
   real behaviour and was checked to fail on the old code (40 instead of 57).
8. **Two discovery bugs sent real companies to dead endpoints.** Lever boards on
   `jobs.eu.lever.co` lost their region (Cirrus Logic, Quantinuum → US API → 404),
   and Workday tenant ids were taken from the hyphenated subdomain (`osv-cci`
   instead of `osv_cci` → 422 on every request). Found live; both now resolve
   correctly from the seed URLs, and are pinned by tests.
9. **One search keyword silently missed bank internships.** Goldman titles them
   "Summer Analyst": searching "intern" finds 1 of 282 campus roles. Morgan
   Stanley's "intern" and "summer analyst" results are largely disjoint. The
   searching adapters now run several terms and merge the results; a later term
   failing keeps what the earlier ones found (it had discarded all 98 of
   Microsoft's postings once).
10. **Live validation would have unmonitored healthy companies.** A 429 mapped to
    `research-needed`; Microsoft's rate limit would have removed it from polling.
    And writing the generated `companies.json` in place broke `make verify`.
    See BLOCKERS.md BLK-012.

## Running things

```bash
make test              # levels 1-4, 6 - no docker, no network
make test-scrapers     # level 2 only
make test-app          # level 5: both clients
make test-mobile       # level 5a (jest-expo + React Native Testing Library)
make test-web          # level 5b (Vitest)
make test-app-e2e      # level 5c (Playwright, preinstalled Chromium)
make mobile-bundle     # prove the Expo app bundles (Metro + Hermes)
make infra-validate    # cdk synth + 49 assertions + cfn-lint
make local             # boot LocalStack and provision the architecture
make local-poll        # inject one scheduled-poll event
make e2e               # boot, provision, levels 7+8, tear down
make verify            # THE GATE: all of it, from clean, with a report
make test-live         # opt-in: real ATS endpoints (never a completion gate)
```

Debugging a LocalStack run:

```bash
docker compose logs -f localstack           # service logs
make local-health                           # the scraper health view
python scripts/local_provision.py --print   # resource ids
python scripts/local_poll.py --watch        # follow the queues until drained
LOCALSTACK_DEBUG=1 docker compose up -d localstack
```

## Deliberate skips

Skips are never silent — each names its reason in the test output.

| Skip | Why |
| --- | --- |
| `-m live` (all) | Requires `ENABLE_LIVE_TESTS=1`; blocked by egress here (BLK-001). Never gates completion, per PRD §31/§14-L7. |
| `-m aws_local` (all) | Requires a provisioned LocalStack; `make e2e` supplies one. |
| Pagination tests, 7 providers | Those APIs return a whole board in one response; a separate test asserts exactly one request is made. |
| Config-identity test, fallback companies | The community feed URL is shared by design; those entries are filtered by employer name instead. |
| `json_ld` wrong-JSON-shape test | It consumes HTML, covered by the garbage-body case. |
