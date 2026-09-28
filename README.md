# Internship Job Monitor

Watches 91 high-value companies (employers at or above an AWS SDE internship
for resume value; 92 more are kept in `data/company_universe.json` under
`excluded` and can be restored) for newly posted software-engineering and
adjacent technical internships and alerts you within roughly one polling interval
(default: **10 minutes**) with a push notification to your phone, plus one
email per hour listing everything found that hour (no email when an hour found
nothing).

> ### This has never been deployed to a real AWS account
>
> That is a deliberate constraint of the build, not an oversight. Every AWS
> behaviour claimed anywhere in this repository was exercised against
> **LocalStack** (real Lambda containers, real SQS/SNS/DynamoDB APIs), **Moto**, or
> **botocore stubs**. `ARCHITECTURE.md` §9 lists the exact real-vs-emulated
> boundary; `VALIDATION_REPORT.md` maps every requirement to its evidence.
> Deploying it is a deliberate act you perform — see [Deploying it
> later](#deploying-it-later).

```text
EventBridge (every 10 min) → Coordinator → SQS → Worker Lambdas → DynamoDB
                                                        ↓
                                              SNS → SQS → Notifier → ntfy push (per job)
                                                        ↓
EventBridge (hourly) → Digest Lambda → DynamoDB → SES email (skipped if empty)
                                                        ↓
                          API Gateway → API Lambda → phone app + web feed
```

---

## Prerequisites

| | Needed for | Notes |
| --- | --- | --- |
| Python 3.11+ | everything backend | 3.11 is what the Lambdas run |
| Node 18+ | the two clients, and the CDK CLI | |
| Docker + Compose | LocalStack (Gate E, `make e2e`) | optional; the default test run needs neither Docker nor network |
| A phone | receiving an actual push | Expo Go from the App Store / Play Store |

No AWS account and no AWS credentials are required for anything in this README
except the final deploy, which you choose to run.

## Setup

```bash
make setup          # python venv + both client toolchains + CDK CLI
cp .env.example .env
```

`.env.example` is documented line by line. Nothing in it is required to run the
tests — they supply their own fake values and refuse to read your real ones.

## Running it locally

```bash
make test           # levels 1-4 and 6: no Docker, no network, ~15s
make verify         # THE GATE: everything, from clean, with a report
```

### A single poll, in process, with no AWS at all

```bash
python -m jobmonitor.cli poll --company Stripe    # real endpoint, real filter
python -m jobmonitor.cli jobs                     # what it found
python -m jobmonitor.cli health                   # the scraper health view
python -m jobmonitor.cli coverage                 # registry coverage summary
```

`poll` with no `--company` polls all 91. With `EMAIL_TRANSPORT=console` and
`PUSH_TRANSPORT=console` you see exactly what would have been delivered.

### The whole architecture, on emulated AWS

```bash
make local          # boot LocalStack and provision everything
make local-poll     # inject one scheduled-poll event
make local-health   # the health view, read from emulated DynamoDB
make e2e            # boot, provision, run levels 7 and 8, tear down
docker compose down -v
```

### The clients

```bash
make serve-api      # the jobs API on :8000 (stdlib http.server, no AWS)
make serve-app      # the web feed on :5173
make serve-mobile   # Expo dev server — scan the QR code with Expo Go
```

## Getting notifications on your phone

### Recommended: ntfy (no app of ours to install)

1. Install **ntfy** from the Play Store / App Store (free, open source).
2. Pick a long random topic name — it is the only secret between the world and
   your alerts:
   `python -c "import secrets;print('jobs-'+secrets.token_hex(12))"`
3. In the ntfy app, subscribe to that topic (server `ntfy.sh`).
4. Set `PUSH_TRANSPORT=ntfy` and `NTFY_TOPIC=<your topic>`, then:

```bash
python -m jobmonitor.cli push-test --sample   # a made-up job, straight to your phone
```

Each new job arrives as its own notification. Tapping it opens the application
page; the **Open application** and **Copy link** buttons do what they say. The
copy button works in ntfy's Android and web apps; on iPhone, ntfy shows the
notification and the tap target but not the copy button, so tap through and copy
from the browser.

### Email: one hourly digest

With `EMAIL_MODE=digest` (the default) the poll does not email. Once an hour the
digest function emails every job first seen in the previous clock hour — strong
matches first, lower-relevance ones listed underneath rather than dropped — and
sends **nothing** for an hour that found nothing. `EMAIL_MODE=instant` restores
one email per alert. Locally, `poll` prints the digest for that poll at the end;
`python -m jobmonitor.cli digest --url ...` runs the hourly one against stored jobs.

### Alternative: the Expo app

The Expo app lives in [`mobile/`](mobile) and its
[README](mobile/README.md) is the step-by-step: install Expo Go, point
`app.json` at an API your phone can reach, `npm start`, grant permission, set
`PUSH_TRANSPORT=expo`. Then:

```bash
python -m jobmonitor.cli devices      # confirm the token registered
python -m jobmonitor.cli push-test    # send one real alert and print its deep link
```

**One thing this repository cannot prove:** that APNs or FCM actually wakes your
handset. There is no simulator here, and a simulator could not mint a push token
anyway. Everything up to and including the request sent to `exp.host` is tested;
that last hop is yours. It is recorded as *not tested* in `TESTING.md` and as
BLK-008 in `BLOCKERS.md` rather than counted as a pass.

There is also a browser client in [`web/`](web) — an installable PWA with Web
Push. It exists because it is the only client whose notification deep links can be
exercised in a **real browser** in this environment, which is why it was kept
rather than deleted. Both clients read the same API.

## Test commands

```bash
make test              # levels 1-4, 6
make test-unit         # level 1
make test-scrapers     # level 2: every provider adapter
make test-integration  # levels 3, 4, 6
make test-app          # level 5: both clients
make test-mobile       # level 5a: the Expo app (122 tests)
make test-web          # level 5b: the web feed (77 tests)
make test-app-e2e      # level 5c: the web feed in real Chromium (11 tests)
make mobile-bundle     # prove the Expo app bundles (Metro + Hermes)
make infra-validate    # cdk synth + 54 template assertions + cfn-lint
make e2e-local         # level 8: the eight PRD §30 acceptance scenarios
make e2e-aws           # level 7: the architecture on emulated AWS
make e2e               # boot LocalStack, both of the above, tear down
make verify            # all of it, from clean, with a pass/fail report
make test-live         # opt-in: real ATS endpoints (never a completion gate)
```

`make verify` writes per-stage logs and `summary.json` to `.verify/`. It reports a
run as **PARTIAL** if any stage was skipped, so a green tick can never quietly
cover less than it claims. Read `TESTING.md` for what is real, what is mocked and
what is emulated.

## Adding a company

1. Find its careers page and work out which ATS backs it. The giveaway is in the
   apply URL: `boards.greenhouse.io/<token>`, `jobs.lever.co/<site>`,
   `jobs.ashbyhq.com/<name>`, `<tenant>.wd*.myworkdayjobs.com`,
   `careers.smartrecruiters.com/<id>`.
2. Add an entry to `data/company_universe.json` under `monitored`, then run
   `make companies`. (`companies.json` is *generated* from it - edit that file by
   hand and `make verify` fails its up-to-date check.) If the company already
   appears in the seed repositories, name, industry and priority are enough: the
   build recovers its board from the application URLs people filed. Otherwise pin
   the source, and cite your evidence in `notes`:

   ```json
   {
     "company": "Example",
     "industry": "Developer Infrastructure",
     "priority": "high",
     "provider": "greenhouse",
     "provider_config": { "board_token": "example" },
     "careers_url": "https://example.com/careers",
     "support_status": "supported",
     "source_discovered_from": ["curated", "live"],
     "notes": "careers page links to Greenhouse board 'example'; 40 postings, apply URLs on example.com"
   }
   ```

   Check the board is really theirs before adding it: a slug that answers is not
   proof. The Greenhouse and Lever boards named `linkedin` both answer, and belong
   to someone else (one has a job titled `123123`). Look at where the apply URLs
   point.
3. `make validate-companies` probes the new entry live and records the verdict in
   `data/validation.json`; transient failures (429, 5xx) are reported but never
   recorded as a verdict.
4. `make test-scrapers`. A per-company test is generated automatically: it builds
   the adapter from your config, checks the request is an absolute HTTPS URL
   against the right API host and embeds your identifiers, and replays the provider
   fixture through it. A copy-pasted slug fails here rather than silently
   monitoring somebody else's board.
5. `make coverage-report` to regenerate `COMPANY_COVERAGE.md`.

`companies.json` was built from the two seed repositories the PRD mandates, via
`scripts/build_company_registry.py`. `make companies` regenerates it; `make
validate-companies` probes every configured source live and rewrites each
`support_status` from what actually answered.

## Adding a provider

1. Write `src/jobmonitor/scrapers/<provider>.py` subclassing `JobSource`, and
   decorate it with `@register`. You implement `fetch_pages()` and `normalize()`;
   retries, backoff, `Retry-After`, pagination bookkeeping, malformed-record
   containment and health reporting are the base class's job.
2. Save one representative response to `tests/fixtures/<provider>/`.
3. Add the provider to the parametrized list in
   `tests/scrapers/test_provider_contract.py`.

If the site is too large to fetch whole and must be *searched*, read its terms
with `configured_queries(self.config)` and run them through `self.search_all(...)`.
One keyword is not enough: Goldman titles internships "Summer Analyst", and a
search for "intern" finds 1 of its 282 campus roles. `search_all` also keeps what
earlier terms found if a later one fails.

That last step is the point: the contract suite then asserts the whole PRD §7 list
against your adapter — required fields, valid absolute URLs, stable ids,
pagination with nothing skipped, malformed records contained, source duplicates
collapsed, empty results not a failure, `500,500,200` recovery, `429` backoff, and
`401/403` as blocked-and-never-retried. A tenth adapter cannot ship with weaker
guarantees than the others.

## Debugging

```bash
docker compose logs -f localstack            # LocalStack service logs
python scripts/local_provision.py --print    # the provisioned resource ids
python scripts/local_poll.py --watch         # follow the queues until drained
make local-health                            # which scrapers are failing, and why
python -m jobmonitor.cli jobs --json         # exactly what the API would serve
LOCALSTACK_DEBUG=1 docker compose up -d localstack
```

| Symptom | Where to look first |
| --- | --- |
| A company returns nothing | `make local-health` — `EMPTY` is a healthy board with no internships; `FAILED` carries the parse error |
| Everything returns 403 | An egress/network policy, not the code. `BLOCKERS.md` BLK-001 has the diagnosis |
| The app shows no jobs | `curl localhost:8000/jobs` — then whether `app.json`'s `apiBaseUrl` is reachable *from the phone* |
| A notification opens the feed, not the job | The payload's `job_id`/`deep_link`. `push-test` prints both |
| `cdk synth` fails | The venv must be on `PATH`; `make infra-synth` does that for you |
| A LocalStack Lambda errors on every invoke | It could not pull a runtime image. `BLOCKERS.md` BLK-003 |

## Deploying it later

**Nothing below has been run.** The stack synthesizes and passes `cfn-lint` plus 54
template assertions; that is the extent of the claim.

```bash
# 1. Configure your account and the values the stack needs
export CDK_DEFAULT_ACCOUNT=<your account id>
export CDK_DEFAULT_REGION=us-east-1

# 2. Review what would be created
make infra-synth
cd infrastructure && cdk diff

# 3. One-time CDK bootstrap, then deploy
cdk bootstrap
cdk deploy JobMonitorStack
```

Estimated cost at 144 polls/day over the 91 polled companies, one user: well
under **$1/month** for Lambda, itemised and measured in `ARCHITECTURE.md` §10.
ntfy.sh is free; SES is $0.10 per 1,000 emails (≤ 720 digests a month).

### Remaining manual configuration

Things the IaC deliberately does not do for you:

1. **Verify an SES identity** for `EMAIL_FROM` and `EMAIL_TO`, and request
   production access if your account is still in the SES sandbox.
2. **Set the secrets** — `EXPO_ACCESS_TOKEN` (only if you enabled Expo's enhanced
   push security), `VAPID_PRIVATE_KEY` (only for the web client's push), and
   `API_WRITE_TOKEN`. Put them in SSM Parameter Store or Secrets Manager, not in
   the template.
3. **Choose a push transport.** Pass `-c ntfyTopic=<your topic>` to
   `cdk deploy` for ntfy (recommended; the topic ends up in the notifier's
   environment, visible to anyone with console access to your account), or leave
   it unset for SNS. `expo` for the phone app, `sns` for a
   dependency-free path through platform endpoints you subscribe yourself,
   `webpush` for the PWA (which needs the `[push]` extra as a Lambda layer,
   because VAPID signing needs `cryptography` and the Lambda runtime lacks it).
4. **Point the clients at the deployed API** — `mobile/app.json` `extra.apiBaseUrl`
   and the web client's build-time API URL.
5. **Re-run `make validate-companies` now and then.** Every polled company was
   validated live on 2026-09-27 (`data/validation.json`); careers sites change.
6. **Set `APP_BASE_URL`** to wherever the web client is hosted, so notification
   deep links resolve.

The jobs table is `RETAIN` on stack delete, on purpose: `first_seen` is the
authoritative record of when you learned about a role, and losing it would make
every job look new again.

## Repository layout

```text
src/jobmonitor/
  models/        Job, JobRecord, Company, ScraperHealth, DeviceRegistration
  scrapers/      base + 11 ATS adapters + 5 company adapters + custom/ (json_ld, fallback, fixture)
  filtering/     relevance scoring, permissive by design
  storage/       JobRepository: in-memory and DynamoDB, one contract
  notifications/ events, formatters, 5 push + 3 email transports
  orchestration/ pipeline, PollRunner, the five Lambda handlers
  api/           framework-free router + Lambda and local-server adapters
infrastructure/  AWS CDK (Python) stack + 49 assertion tests
mobile/          the Expo / React Native phone app
web/             the installable React PWA
tests/           unit · scrapers · integration · e2e · aws_local · fixtures
scripts/         registry build, live validation, provisioning, verify
```

## Documents

| File | Contents |
| --- | --- |
| `CLAUDE.md` | The product requirements this build was specified by |
| `ARCHITECTURE.md` | Chosen design, alternatives weighed, emulation gaps, cost |
| `TESTING.md` | The pyramid, and what is real vs mocked vs emulated |
| `COMPANY_COVERAGE.md` | Coverage report, and what each support status asserts |
| `BLOCKERS.md` | Every blocker hit, its attempts, and its resolution |
| `VALIDATION_REPORT.md` | Requirement → status → evidence matrix |
| `mobile/README.md` | Getting push notifications onto your phone |
