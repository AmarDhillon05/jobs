# Architecture

How the internship monitor is built, why it is built that way, what the
alternatives were, and — importantly — exactly which parts of the AWS story were
verified against emulated infrastructure rather than the real thing.

> **This system has never been deployed to a real AWS account.** That was a hard
> constraint of the build (PRD §12). Every AWS claim below was verified under
> Moto, botocore stubs, or LocalStack, and the boundaries are named in
> [Local emulation gaps](#local-emulation-gaps).

---

## 1. The shape of the problem

The job is to notice a new internship posting within ~10 minutes, across ~150
companies, for one user, at roughly zero cost. That framing drives every decision
that follows:

- **150 sources × 144 polls/day** is ~21,600 fetches/day. Tiny by cloud standards,
  large enough that doing it serially in one process will not fit in a Lambda.
- **Detection latency is the product.** A design that is correct but 30 minutes
  late has failed at the thing it exists for.
- **The failure mode that matters is silence.** A scraper that quietly breaks, or
  an alert that is quietly suppressed, is worse than a loud crash — so failure
  isolation and duplicate suppression get more design attention than throughput.
- **One user.** No multi-tenancy, no sharding by customer, no read replicas. This
  licenses several deliberately simple choices flagged below.

## 2. Selected architecture

```text
              EventBridge rule  (rate: 10 minutes)
                        │
                        ▼
              Coordinator Lambda           reads companies.json, shards it
                        │                  8 companies per message
                        ▼
              SQS  jobmonitor-scrape ──────after 3 attempts──▶  scrape DLQ
                   ╱        │        ╲
          Worker Lambda  Worker    Worker      (concurrent, 1 shard each,
                   ╲        │        ╱          reserved concurrency 10)
                        ▼
              ┌──────────────────────┐
              │ DynamoDB jobs table  │  ← idempotent upsert, first_seen pinned
              │ DynamoDB health tbl  │
              └──────────────────────┘
                        │ new + relevant only
                        ▼
              SNS  jobmonitor-new-jobs
                        │
                        ▼
              SQS  jobmonitor-notify ───────after 3 attempts──▶  notify DLQ
                        │
                        ▼
              Notifier Lambda ──▶ SES email  +  Expo/SNS/Web Push
                                        │
                                        ▼
                                  deep link  /jobs/<id>

              API Gateway HTTP API ──▶ API Lambda ──▶ DynamoDB ──▶ app + web feed
```

### Why this and not the alternatives

The PRD sketched three candidates. All three were evaluated against the
constraints in §1.

| Option | Verdict |
| --- | --- |
| **One Lambda polls all 150 companies** | Rejected. At a conservative 2 s per source it is ~5 minutes serially, and the tail is unbounded — one slow Workday tenant with retries can push a run past the 15-minute Lambda ceiling, at which point *the whole poll is lost*, not just that company. It also couples unrelated failures into one invocation and makes retry all-or-nothing. |
| **EventBridge → Step Functions → parallel groups** | Rejected, narrowly. Map state concurrency is genuinely nice and the execution history is excellent for debugging. But it costs per state transition (~$0.025/1,000), needs its own IAM and error-handling vocabulary, and LocalStack's community Step Functions support is weaker than its SQS support — which would have traded real local verification for a nicer console. For a fan-out this small, SQS gives the same parallelism with better local fidelity. |
| **EventBridge → coordinator → SQS → workers** ✅ | **Selected.** Per-message retry and dead-lettering come free and are exactly the semantics needed: one company's outage must not replay its 7 shard-mates. Reserved concurrency caps the blast radius. SQS is the best-emulated service in LocalStack community, so the architecture could actually be *exercised* rather than asserted. And it is effectively free at this volume. |

### Decisions worth defending

**Shard size 8, one shard per invocation.** 150 companies ÷ 8 = 19 messages.
`BatchSize: 1` on the event source means one shard per invocation, so a retry
re-runs 8 companies rather than 80, and `batchItemFailures` stays unambiguous.
Eight was picked so a shard's worst case (8 × 15 s timeout × 3 attempts ≈ 6 min)
sits inside the 5-minute function timeout for the realistic case while leaving
headroom; the queue's visibility timeout is set to twice the function timeout so
SQS cannot redeliver a shard that is still running.

**Companies travel inside the message.** The coordinator inlines each company's
full config into the `ScrapeTask` rather than sending names for the worker to look
up. It costs a few KB per message and removes a whole class of bug: mid-deploy,
the worker cannot disagree with the coordinator about what a company's
configuration is. It also means the architecture test can inject a deterministic
company without touching `companies.json`.

**A failing scraper is not a failing message.** This distinction is the core of
the retry design. A dead careers site → health record, message *succeeds*
(retrying cannot fix someone else's outage, and replaying it would hammer them).
A failed DynamoDB write → `StorageFailure` raised, message reported in
`batchItemFailures`, retried, and eventually dead-lettered (the work genuinely did
not happen). Getting this backwards produces either infinite retry storms or
silent data loss.

**SNS in front of the notification queue.** A topic where a queue would do, so a
second consumer — a log sink, a webhook, a future Slack relay — is a subscription
rather than a code change.

**The notifier re-reads storage before sending.** The queue payload says what
*was* new; the stored record says what has already been alerted about. Storage
wins. That is what makes an at-least-once redelivery silent rather than a second
3 a.m. notification.

**EventBridge *rule*, not EventBridge Scheduler.** Identical function for a
fixed-rate trigger, one less service, and materially better local emulation.
Scheduler's advantages (flexible time windows, per-schedule IAM) buy nothing for
"every 10 minutes".

## 3. Storage design

One table for jobs, one for scraper health, one for push devices. All on-demand
billing — this workload is orders of magnitude below the break-even point for
provisioned capacity.

**`jobs`** — partition key `job_id`, plus three global secondary indexes:

| Index | Keys | Answers |
| --- | --- | --- |
| `recent-index` | `feed` / `first_seen` | "what's new?" — the feed, in one query |
| `company-index` | `company` / `first_seen` | "everything from Stripe" |
| `pending-index` | `notification_pending` / `first_seen` | "what still needs alerting?" |

`pending-index` is deliberately **sparse**: the key attribute is written when a
record is created and *removed* when it is notified, so the index holds the
handful of outstanding jobs instead of every job ever discovered.

`recent-index` uses a constant partition key (`feed = "JOB"`). For one user and
~150 companies that is a few hundred writes a day into one logical partition —
comfortably inside DynamoDB's per-partition limits — and it makes the feed a
single query. **If this ever served many users**, the fix is a date-bucketed
partition key (`first_seen_day`) queried across the last N days; that is strictly
more code for zero benefit today, so it is documented rather than built.

### The idempotent upsert

One `UpdateItem` per sighting, no read-then-write:

```
SET first_seen   = if_not_exists(first_seen, :now),     ← immovable once set
    last_seen    = :now,
    times_seen   = if_not_exists(times_seen, :zero) + :one,
    notification_sent = if_not_exists(notification_sent, :false)
ReturnValues = ALL_OLD                                   ← new vs seen-again,
                                                            and the old hash
```

`first_seen` being pinned by `if_not_exists` is what makes detection timing
trustworthy even under a concurrent retry. `ALL_OLD` answers *new vs. seen-again*
and *seen vs. changed* in the same round trip, so the steady state is exactly one
write per job per poll.

**A bug worth recording.** The obvious way to maintain the sparse index —
`notification_pending = if_not_exists(notification_pending, :pending)` — looks
right and is wrong. The marker is *removed* at notify time, so `if_not_exists`
re-adds it on the next sighting, putting an already-notified job back on the
pending queue and re-alerting the user every 10 minutes forever. The marker is now
written by a separate guarded call made only for genuinely new records.
`test_seeing_a_notified_job_again_does_not_make_it_pending` pins the behaviour and
a Stubber test asserts the expression never contains that clause again.

### Identity

Three different questions, deliberately three different functions:

- `canonical_url` — "same page?" Strips tracking parameters, so the same posting
  reached via Simplify's and Ouckah's `utm` tags cannot become two jobs.
- `job_id` — "seen before?" `company:external_id` when the provider gives an id
  (it survives title edits and URL churn); otherwise a hash of company +
  normalized title + normalized location + canonical URL.
- `content_hash` — "changed?" Covers the user-visible fields and **excludes
  `date_posted`**, because Workday reports relative text ("Posted 5 Days Ago"), so
  the absolute date we derive from it moves daily even when nothing changed.
  Including it marked all 44 Workday jobs as updated on every poll.

## 4. Scraping

Adapters cover 91 polled companies (183 before the user's 2026-09-28 cut to
employers at or above an AWS SDE internship). Eleven are reusable ATS adapters;
five serve one very large employer each, whose own site is the only first-party
source. Adding a company to an existing adapter is a registry entry, not code.

| Provider | Companies | Shape |
| --- | --- | --- |
| `greenhouse` | 41 | `GET` board API, whole board in one response |
| `workday` | 9 | `POST` CXS, paginated, 20/page, hard limit of 2,000 results |
| `ashby` | 17 | `GET` posting API |
| `lever` | 5 | `GET` postings array; US or **EU** instance (`region`) |
| `eightfold` | 3 | `GET` search (PCSX, or the older v2 API), 10/page — Microsoft, Millennium, Netflix |
| `smartrecruiters` | 1 | `GET`, paginated offset/limit against `totalFound` |
| `oracle_hcm` | 1 | `GET` Fusion HCM REST, 200/page — Uber |
| `jibe` | 1 | `GET` iCIMS Jibe `/api/jobs`, 10/page — Susquehanna International Group |
| `talentbrew` | 0 (ready; its companies were cut) | `GET` Radancy search XHR returning an HTML fragment (was Arm) |
| `rippling` | 1 | `GET` board array |
| `amazon` | 1 | `GET` amazon.jobs `search.json`, 100/page |
| `google` | 1 | `GET` results page; job data embedded in the HTML |
| `goldman` | 1 | `POST` GraphQL (schema open to introspection), campus section read whole |
| `ibm` | 0 (ready; its companies were cut) | `POST` Elasticsearch-style search API |
| `atlassian` | 1 | `GET` listings array |
| `workable` | 0 (ready) | `GET` account widget |
| `json_ld` | 0 (ready) | schema.org `JobPosting` in server-rendered HTML |
| `simplify_fallback` | 8 | community feed, **secondary only** |

**Searching adapters.** Greenhouse, Lever and Ashby hand over a whole board and
the relevance filter decides. Eightfold, Oracle, Jibe, TalentBrew, Amazon, Google,
IBM and (per tenant) Workday are too large for that and must *search*, and a search
only finds what its keyword names. One keyword measurably misses roles: Goldman
titles internships "Summer Analyst" (a search for "intern" finds 1 of 282 campus
roles), and Morgan Stanley's "intern" and "summer analyst" results are largely
disjoint. So these adapters run several terms (`scrapers/_search.py`) and the base
class merges the overlap. If the *first* term fails, the company fails, which is
how a dead or blocking site shows up; if a *later* one fails, the results already
found are kept and the scraper reports `DEGRADED`, naming the term.

**How the new sources were configured.** The first 150 companies' configs came from
application URLs in the seed repositories (below). The 35 added later, and the
companies that had moved platform, were configured from each company's own
careers page, by finding which ATS it links to, then verifying that ATS's live API,
checking that the board belongs to that company by where its apply URLs point,
and checking that job links resolve. A slug that answers is not proof: boards
named `linkedin` exist on both Greenhouse and Lever, and neither is LinkedIn's.

**Where the configs came from.** Both mandated seed repositories publish a
structured `listings.json` behind their README tables. 17,607 listings collapse to
3,177 employers, 2,382 of which expose a configured ATS endpoint. Every
`provider_config` in the registry was *extracted from real observed application
URLs* — Anthropic's Greenhouse board token comes from actual postings, not from a
slug that looked plausible.

**Runtime-resolved board tokens.** Six companies (Stripe, Databricks, Coinbase,
Waymo, Hudson River Trading, Datadog) run a Greenhouse board behind their own
domain. Their postings carry Greenhouse's `gh_jid`, which proves the provider but
never the board token. Rather than guess one and call it verified, those entries
carry `board_token_candidates` and the adapter keeps whichever board answers — a
404 means wrong token and moves on, while a 403 is `AccessBlocked` and a 5xx is a
real failure, neither disguised as a bad guess.

**The retry policy** is bounded: exponential backoff with full jitter (uniform in
`[0, cap]`, which avoids 19 workers retrying the same provider in lockstep),
`Retry-After` honoured and capped, 429/5xx/network retried, and 401/403 raised as
`AccessBlocked` and *never* retried — this project does not probe bot walls
(PRD §5).

**The HTTP transport is injected.** That single decision is why a `500, 500, 200`
sequence and a `429 + Retry-After` are ordinary unit tests: no network, no mocking
library, no real sleeping, and the same assertions run for every adapter.

## 5. Filtering

A small, inspectable, configurable weighted-keyword scorer — not a learned model.
Two reasons: every decision must be explainable (the score carries its reasons,
which both tests and the health view read), and a personal project polling every
10 minutes should not pay for inference.

Two thresholds implement PRD §2's tie-breaker — *prefer retaining a low-scoring
job over silently discarding it*:

- **keep (35)** — stored, visible in the feed, no alert.
- **notify (55)** — reaches the phone.

Running it over the 10,852 distinct **real** titles in the seed data found three
flaws no hand-written test would have:

1. Substring matching read "intern" out of "**Intern**al Audit" and
   "**Intern**ational Operations", inventing internships. Matching is now
   word-boundary aware.
2. "Student 2 — Software Engineering" was dropped outright; `student` was missing
   from the internship signals.
3. "Data Science Intern — Influencer **Marketing** AI" was discarded because a
   non-technical word appeared in the *team*, not the role. Titles are now split
   into role and qualifier, and a non-technical qualifier costs a quarter of what
   a non-technical role name does. "Marketing Intern" is still dropped.

200 of those real titles are committed as a regression corpus, so tuning a weight
now names every real-world title that crossed a boundary.

## 6. Clients

**Two of them, sharing one API:**

| | `mobile/` | `web/` |
| --- | --- | --- |
| What | Expo / React Native app | Installable React PWA |
| Runs on | the user's phone, via Expo Go or a build | any browser, installable to a home screen |
| Push | Expo push (APNs/FCM) | Web Push (VAPID) |
| Deep link | `internshipmonitor://jobs/<id>` and the https URL | `/jobs/<id>` |
| Tested with | jest-expo + React Native Testing Library, 122 tests | Vitest + Testing Library, 77 tests, plus 11 in real Chromium |

The Expo app is the one the user asked for, and it is the primary client: a real
phone notification is the point of the whole system. The PWA was built first and
is kept rather than deleted, because it is the only client whose notification-tap
and deep-link behaviour can be exercised in a **real browser** here — this build
environment has no iOS or Android simulator, so the Expo app's equivalents are
asserted against mocked native modules. Keeping both means the risky path is
covered two ways: mocked-but-on-the-real-app, and real-browser-but-different-app.

What that honestly leaves untested: that APNs or FCM actually wakes the handset.
That requires a physical device and is the user's step — `mobile/README.md` says
exactly what to do, and `TESTING.md` records it as a gap rather than a pass.

**Routing is deliberately not a navigation library.** Two states — feed and job —
because the only route that must work is the one a notification produces, and
keeping it explicit makes that directly testable.

**Push transports.** Four, all behind one interface:

- `expo` — one HTTPS POST to `exp.host` carrying up to 100 messages, over the
  project's own retrying HTTP client. No signing key, no extra dependency, no
  per-platform code. This is the path to `mobile/`. A ticket coming back
  `DeviceNotRegistered` retires that device instead of being retried, because
  retrying can never succeed and one dead phone would otherwise wedge every
  future alert behind it.
- `sns` — dependency-free (boto3 is in the runtime), so the whole push pipeline
  runs under Moto and LocalStack with zero installs; fans out to APNS/FCM/SMS
  platform endpoints if the user subscribes them.
- `webpush` — straight to the PWA, behind a lazy optional extra (VAPID signing
  needs `cryptography`, which the Lambda runtime lacks), with an error naming both
  the install command and the dependency-free alternative.
- `console` / `memory` — the local sink and the test fake.

**The client distrusts the backend.** Records are validated individually so one
bad row cannot blank the feed; non-`http(s)` apply URLs are refused outright (a
`javascript:` URL from a compromised upstream board must never become a clickable
button); scores are clamped; and jobs are merged by id so a redelivered push plus
a refresh cannot show the same job twice.

## 7. Deployment

AWS CDK in Python — the PRD's preferred default, and the right one here because
the stack is mostly IAM and event wiring, which CDK's `grant*` methods get right
more reliably than hand-written policy JSON.

**The Lambda asset is just `src/jobmonitor` + `companies.json`.** There is nothing
to `pip install`, because the backend uses only the standard library plus
boto3/botocore/urllib3 — all already in the runtime. That is why `cdk synth` needs
neither Docker nor network, which in turn is why infrastructure validation is part
of the ordinary test suite. A test AST-walks every module in the bundle and asserts
each import root is stdlib or one of those three, so the property cannot silently
regress.

Table key schemas live in `jobmonitor/storage/tables.py` and are read by the
provisioner, the tests, **and** asserted against the synthesized CloudFormation —
so a renamed index breaks loudly in one place instead of leaving the query code
and the deployed table quietly disagreeing.

`ARM_64` throughout: ~20% cheaper per GB-second at identical performance for this
workload. The jobs table is `RETAIN` on purpose — destroying it would make every
known job look new and re-notify the entire backlog.

## 8. Testing strategy

Full detail in [`TESTING.md`](TESTING.md). The short version: ~1,760 backend
tests, 49 CDK template assertions, 77 client unit tests, 11 Playwright browser
tests, and a LocalStack architecture suite.

The design principle is that **the same pipeline code runs in-process, in a
Lambda, and against LocalStack**. `PollRunner` is not reimplemented per
environment, so when an in-process scenario passes and its LocalStack twin fails,
the fault is isolated to the infrastructure rather than to a second copy of the
logic.

Both repository implementations — in-memory and DynamoDB — are held to one shared
contract suite that runs every persistence assertion twice. That is what makes the
fast in-memory store trustworthy as the default path.

## 9. Local emulation gaps

The honest ledger. What follows was **not** natively exercised, and what stands in
for it.

| Boundary | Status | Substitute, and why it is adequate |
| --- | --- | --- |
| **EventBridge schedule** | Simulated | LocalStack community's scheduler emulation is unreliable. Per PRD §18.4 the EventBridge rule stays in the CDK stack and is asserted in `infrastructure/tests` (expression, target, input payload, invoke permission); `scripts/local_poll.py` delivers the *identical* payload to the coordinator. Nothing downstream is simulated. |
| **API Gateway flavour** | Differs | The deployed stack uses an HTTP API (v2); v2 is a LocalStack **Pro** feature, so the local stack provisions a REST API (v1) instead. Both invoke the same Lambda with the same proxy event shape, `to_request` reads v1 and v2 alike, and a Level-7 test invokes the function with a real v2 payload directly. |
| **Lambda runtime image** | Substituted | LocalStack pulls `public.ecr.aws/lambda/python:3.11`, which this build network blocks. Remapped via `LAMBDA_RUNTIME_IMAGE_MAPPING` to `mlupin/docker-lambda:python3.11` from Docker Hub. Real containers, real cold starts, equivalent runtime — one line in `docker-compose.yml` to switch back. |
| **IAM enforcement** | Not enforced locally | LocalStack does not evaluate IAM by default, so the local role is permissive. The real least-privilege policies are asserted against the synthesized template instead: the coordinator holds *no* DynamoDB permissions, SES is scoped to one identity rather than `*`, no wildcard-action-on-wildcard-resource, and every function has its own role. |
| **SES delivery** | Never sent | Local runs use the console sink. SES request *shape* is verified under Moto, including that an unverified sender surfaces as a `DeliveryError` so the notifier falls back to the other channel. |
| **Web Push delivery** | Never sent | Needs a real browser subscription and VAPID keys. The payload, the deep link and the client's handling of it are all tested; only the final network hop is not. |
| **Expo push delivery** | Never sent | The request Expo would receive is asserted byte-for-byte against a scripted transport, as is the handling of every ticket status it can return. That APNs/FCM then wakes a handset needs a physical device — `mobile/README.md`. |
| **Live ATS endpoints** | **Blocked** | This environment's egress policy refuses every ATS host (`BLOCKERS.md` BLK-001), so provider fixtures were authored to documented response shapes rather than captured (BLK-002). The deployed worker's outbound path *is* proven — a Level-7 test polls a real company and asserts the health record names the real endpoint. `make validate-companies` closes the rest in one command. |
| **DynamoDB TTL expiry** | Not observed | TTL is configured and asserted in the template; actual expiry takes up to 48 h on real AWS and is not emulated. It only affects cleanup of health diagnostics. |
| **Real AWS deployment** | **Intentionally not done** | Hard constraint, PRD §12. |

## 10. Cost estimate

Assumptions: 144 polls/day, 150 companies, shard size 8 (19 messages/poll), one
user, `us-east-1`, ARM Lambdas, on-demand DynamoDB.

| Service | Monthly usage | Cost |
| --- | --- | --- |
| Lambda — coordinator | 4,320 invocations × ~1 s × 256 MB | ~$0.01 |
| Lambda — workers | 82,080 invocations × ~8 s × 512 MB | ~$2.80 |
| Lambda — notifier | ~1,500 invocations × 1 s × 256 MB | <$0.01 |
| Lambda — API | ~3,000 requests × 0.3 s × 256 MB | <$0.01 |
| SQS | ~170k requests (batched sends) | ~$0.07 |
| SNS | ~1,500 publishes | <$0.01 |
| DynamoDB | ~650k writes, ~200k reads, <1 GB | ~$1.10 |
| API Gateway HTTP API | ~3,000 requests | <$0.01 |
| EventBridge | 4,320 rule invocations | $0 (free) |
| CloudWatch Logs | ~1 GB ingest, 14-day retention | ~$0.55 |
| SES | ~1,500 emails | ~$0.15 |
| **Total** | | **≈ $4.70/month** |

Inside the PRD's $0–10 target. The workers dominate, as expected — they are the
only thing doing real work. The obvious lever if it ever mattered: raise the shard
size (fewer, longer invocations amortise cold starts) or drop the poll interval to
15 minutes, which would roughly halve it. Free-tier coverage on a new account
would make the first year closer to **$1–2/month**.

### Update - measured, after growing to 183 companies

The table above was estimated for 150 companies, each fetched with one request.
Growing the registry to 183 and adding searching adapters changed the workload,
so it was **measured**: a sequential live run over every company on 2026-09-27
took **619 s of fetching per poll** (median company 0.4 s; the slowest are
Applied Materials 53 s, JPMorgan 51 s, NVIDIA 44 s, Oracle 40 s). The worst shard
of 8 takes 94 s, against a 300 s worker limit.

Lambda bills wall time, so worker cost at 144 polls a day is now:

| Configuration | GB-s / month | Cost after the 400k free GB-s |
| --- | --- | --- |
| Today: 512 MB, a shard's companies fetched one after another | 1.34 M | **~$12.50** |
| 256 MB (one-line infrastructure change) | 0.67 M | ~$3.60 |
| Fetch a shard's 8 companies concurrently, 512 MB | 0.82 M | ~$5.60 |
| Both | 0.41 M | **~$0.10** |

That takes the whole deployment over the PRD's $0-10 target until one of those
changes is made. Both are assumptions to verify on real Lambda: its network timing
differs from this environment's, and 256 MB means proportionally less CPU for
parsing the largest pages. Recorded rather than silently fixed, because the
polling design is being reworked (hourly digest, per-job instant alerts).

### Update - after the cut to 91 companies (2026-09-28)

The user cut polling to the 91 employers at or above an AWS SDE internship for
resume value. Summing the same 2026-09-27 per-company timings over what remains
gives **195 s of fetching per poll** (slowest NVIDIA 44 s, Amazon 22 s,
Salesforce 21 s); the worst shard of 8 takes 61 s. At 512 MB that is ~0.42 M
GB-s a month, just over the free 400k: **~$0.40/month** for workers, and inside
the free tier at 256 MB. The rest of the table above is unchanged or smaller.

## 11. Deviations from the PRD's suggestions

| PRD suggested | Built | Why |
| --- | --- | --- |
| `src/models`, `src/api`, … | `src/jobmonitor/{models,api,…}` | Top-level `models` and `api` are too generic to sit on `sys.path`; one named package keeps imports unambiguous and makes the Lambda bundle a single directory. |
| React Native / Expo *or* PWA | **Both** | See §6 — Expo is the phone app the user wants; the PWA is the one whose deep links can be exercised in a real browser here. |
| EventBridge Scheduler | EventBridge rule | §2 — same function, better emulation. |
| DynamoDB (default) | DynamoDB | Kept. Nothing about this workload argues for anything else. |
| `cdklocal` to provision LocalStack | boto3 provisioner | Provisions in seconds instead of minutes, needs no CloudFormation bootstrap, and fails with an error that names the resource. Table schemas are shared with the CDK stack and asserted against its template, so the two cannot diverge. |
