# Internship Job Monitor — Final Product Requirements & Agent Execution Specification

**Status:** Implementation-ready  
**Primary consumer:** Autonomous coding agent (Claude / similar)  
**Project type:** Local-first, AWS-ready job monitoring system  
**Hard rule:** Do **not** deploy anything to a real AWS account during this implementation.

---

## 0. Agent Mission

Build a production-style system that continuously monitors high-value companies for newly posted Software Engineering internships and adjacent technical internships, detects new postings quickly, and delivers email + mobile/app notifications.

This is not complete when code merely exists. It is complete only after:

1. Every major component has its own automated tests.
2. Every scraper/provider adapter has meaningful parsing and data-quality tests.
3. The app/client has automated tests covering rendering, data loading, and notification/deep-link behavior.
4. The backend/storage/dedup/filtering/notification pipeline has automated tests.
5. The infrastructure-as-code can synthesize/validate successfully.
6. The AWS architecture is exercised end-to-end in a local AWS simulation/emulation environment.
7. The full system passes an end-to-end local test from scheduled event → scraping → normalization → filtering → persistence → notification → app/feed.
8. Duplicate suppression is proven.
9. Partial scraper failure is proven not to break the rest of the run.
10. The agent performs a final validation pass against this PRD and explicitly verifies every acceptance criterion.
11. A final Git commit is created with a clean working tree.

Do not declare success before the completion gates in this document pass.

---

# 1. Objective

Create an end-to-end system that monitors approximately **100–150 high-value companies** for newly posted technical internships and alerts the user approximately within one polling interval.

Target polling interval:

```text
10 minutes
```

The system should:

- Monitor ~100–150 companies.
- Detect newly listed internships.
- Prefer official company career pages / ATS endpoints.
- Normalize jobs into one schema.
- Filter for relevant technical internships.
- Persist discovered jobs.
- Deduplicate notifications.
- Send email notifications.
- Send mobile/app push notifications.
- Provide a simple mobile app or installable client/feed.
- Include direct links to original job postings.
- Be runnable locally.
- Be testable locally.
- Be AWS-ready via infrastructure-as-code.
- **Not require real AWS deployment during implementation.**
- Be easy to extend with more companies.
- Track scraper health and failures.
- Optionally support assisted application workflows later.

Primary priorities, in order:

1. Reliability
2. Earliest practical job detection
3. Coverage
4. Correct deduplication
5. Maintainability
6. Testability
7. Failure isolation
8. Low cloud cost
9. Simple deployment

---

# 2. Target Roles

Primary roles:

- Software Engineer Intern
- Software Developer Intern
- Backend Engineer Intern
- Infrastructure Engineer Intern
- Systems Engineer Intern
- Platform Engineer Intern
- Cloud Engineer Intern
- Site Reliability Engineer Intern
- Developer Productivity / DevTools Intern
- Machine Learning Engineer Intern
- AI Engineer Intern
- Data Engineer Intern
- Security Engineer Intern
- Quantitative Developer Intern
- Firmware Engineer Intern
- Embedded Software Intern
- Robotics Software Intern
- Research Engineer Intern where appropriate

Adjacent roles may be included when they are plausibly valuable to a strong CS/SWE candidate.

Avoid irrelevant internship categories such as pure marketing, HR, accounting, sales, legal, or non-software engineering unless technical content makes the role relevant.

When uncertain, prefer retaining a job with a lower relevance score over silently discarding it.

---

# 3. Company Quality / Universe

The company universe should emphasize companies whose engineering work, recruiting signal, technical depth, selectivity, compensation, or brand value can reasonably be considered competitive with strong large-tech/software internships.

Do **not** limit the universe to traditional Big Tech.

Include categories such as:

- Big Tech
- AI labs / AI infrastructure
- high-growth startups
- developer tools
- databases
- cloud infrastructure
- cybersecurity
- fintech
- payments
- quantitative trading
- hedge funds / market makers
- autonomous vehicles
- robotics
- aerospace / space
- semiconductor companies
- enterprise infrastructure
- consumer technology
- high-quality hardware/software companies

The system should support priority tiers such as:

```text
high
medium
experimental
```

These tiers are for monitoring/notification configuration, not a rigid public ranking.

---

# 4. Mandatory Seed Sources for Company Discovery

The agent must begin company/job-source discovery from **both** of these active community repositories:

## 4.1 Simplify / Pitt CSC Summer 2027 Internships

Repository:

```text
https://github.com/SimplifyJobs/Summer2027-Internships
```

This repository tracks Summer 2027 software engineering, data science, AI, quant, product, and hardware internships.

Use both:

- active listings
- inactive/closed listings

Closed listings are useful because they reveal companies that hire interns even if they currently have no open role.

## 4.2 Vansh & Ouckah / CSCareers Summer 2027 Internships

Repository:

```text
https://github.com/vanshb03/Summer2027-Internships
```

Use the main list and any off-season / archived listings that help identify recurring employers.

## 4.3 Source Policy

These two repositories are **starting guides**, not the authoritative job feed.

The agent must:

1. Extract unique company names.
2. De-duplicate aliases.
3. Identify official careers pages.
4. Identify ATS/provider when possible.
5. Prefer official/public company endpoints for monitoring.
6. Expand the universe using additional research.
7. Reach approximately 100–150 worthwhile monitored companies.
8. Avoid depending on Simplify/Ouckah as the live detection mechanism unless used only as a secondary fallback or discovery feed.

Produce:

```text
companies.json
```

Suggested schema:

```json
{
  "company": "Example",
  "careers_url": "https://...",
  "industry": "Developer Infrastructure",
  "priority": "high",
  "provider": "greenhouse",
  "provider_config": {},
  "source_discovered_from": ["simplify", "ouckah"],
  "support_status": "supported",
  "notes": "..."
}
```

Support statuses:

```text
supported
partial
blocked
needs-browser
unsupported
research-needed
```

---

# 5. Careers Source Discovery

For every company, determine the canonical source for job postings.

Possible sources:

- Greenhouse
- Lever
- Ashby
- Workday
- SmartRecruiters
- company-owned JSON API
- company GraphQL endpoint
- server-rendered careers page
- public jobs API
- other ATS provider

Preference order:

1. Documented/public jobs API
2. Publicly accessible structured JSON endpoint
3. ATS-specific endpoint
4. Public GraphQL/API request used by careers page
5. Server-rendered HTML
6. Browser automation only when necessary and allowed

Do not:

- bypass authentication
- solve/bypass CAPTCHAs
- defeat anti-bot controls
- evade explicit access restrictions
- intentionally bypass rate limits
- create unauthorized/burner cloud accounts

If a source blocks automation, mark it and continue.

---

# 6. Scraper Framework

Create a standardized provider abstraction.

Example:

```python
class JobSource:
    def fetch_jobs(self) -> list["Job"]:
        ...

    def normalize(self, raw_job) -> "Job":
        ...

    def healthcheck(self) -> "ScraperHealth":
        ...
```

Normalized job model:

```python
@dataclass
class Job:
    company: str
    title: str
    location: str | None
    url: str
    external_id: str | None
    date_posted: datetime | None
    source: str
    description: str | None
    employment_type: str | None
```

Prefer reusable provider adapters:

```text
scrapers/
  greenhouse.py
  lever.py
  ashby.py
  workday.py
  smartrecruiters.py
  custom/
    company_a.py
    company_b.py
```

Do not implement 150 fully independent scrapers when one provider adapter can cover many companies.

---

# 7. Scraper-Level Definition of Correctness

Each scraper/provider must prove that it obtains the data required by the rest of the system.

At minimum, validate that each returned job has:

- company
- title
- application/job URL
- stable external ID when available
- location when available
- posting date when available
- description when available
- source/provider

Tests must ensure:

- parser returns structured jobs
- required fields are non-empty
- URLs are syntactically valid
- links resolve to plausible job/application pages when live integration testing is enabled
- provider-specific IDs are stable where available
- pagination is handled if the provider paginates
- no page/result set is silently skipped
- malformed records do not crash the entire source
- duplicate records from the source are normalized/deduplicated
- empty internship results do not automatically imply scraper failure

For provider adapters, include fixture-based tests using saved representative responses.

For custom scrapers, include fixtures for the specific site.

---

# 8. Job Filtering

Filtering must be separate from scraping.

Use a configurable relevance model/score.

Possible positive signals:

```text
intern
internship
software
developer
backend
platform
infrastructure
systems
machine learning
AI
data
security
cloud
SRE
quant
firmware
embedded
robotics
research engineer
```

Potential negative signals:

```text
marketing
HR
sales
accounting
legal
civil
pure mechanical
nursing
```

Suggested field:

```text
relevance_score: 0–100
```

Notification threshold must be configurable.

The filter should be permissive enough to avoid missing strong adjacent technical roles.

---

# 9. New-Job Detection

The system must distinguish:

- existing jobs
- newly discovered jobs
- updated jobs
- removed jobs

Preferred identity:

```text
company + external_job_id
```

Fallback:

```text
hash(company + normalized_title + normalized_location + canonical_url)
```

Persist:

```text
job_id
company
title
location
url
external_id
date_posted
first_seen
last_seen
notification_sent
source
relevance_score
content_hash
```

`first_seen` is authoritative for detection timing because many careers sites provide unreliable/missing posting timestamps.

The same unchanged job must not trigger a second notification.

---

# 10. Storage

Use a serverless-friendly store.

DynamoDB is the expected default unless the agent documents a stronger alternative.

Required behavior:

- idempotent inserts
- lookup by stable job ID
- recent jobs query
- first_seen / last_seen updates
- notification state
- scraper health storage
- optional TTL for old health records

For local tests:

- use Moto for fast unit-level DynamoDB behavior where useful
- use LocalStack for higher-level AWS integration behavior

---

# 11. Initial Cloud Architecture

Starting idea:

```text
EventBridge Scheduler
        |
        v
Coordinator
        |
        v
Scraper execution
        |
        v
Normalize / Filter
        |
        v
DynamoDB
        |
        v
Notification pipeline
      /       \
   Email      Push
```

The agent should evaluate whether 100–150 sources should run in one Lambda.

A likely stronger architecture is:

```text
EventBridge Scheduler
        |
        v
Coordinator Lambda
        |
        v
       SQS
   / / / \ \ \
 worker Lambdas
        |
        v
   DynamoDB
        |
        v
Notification event
     /       \
 Email       Push
```

Alternative:

```text
EventBridge
    |
Step Functions
    |
parallel scraper groups
    |
DynamoDB
    |
notifications
```

The agent may choose another design.

Evaluate:

- Lambda duration limits
- total scrape duration
- concurrency
- rate limits
- retry semantics
- partial failures
- cost
- complexity
- cold starts
- queue backlog
- observability
- deployment simplicity
- local emulation quality

Document the final decision in:

```text
ARCHITECTURE.md
```

---

# 12. No Real AWS Deployment During Implementation

**Hard constraint: Do not deploy this project into a real AWS account as part of this task.**

Do not:

- create a burner AWS account
- create real Lambda functions
- create real EventBridge schedules
- create real DynamoDB tables
- create real queues/topics
- send test traffic that incurs AWS resource creation
- require AWS credentials for the primary test path

The implementation must nevertheless be as deployment-ready as possible.

The agent should produce infrastructure-as-code that a user could later deploy deliberately.

---

# 13. Required AWS Testing Strategy

Use multiple layers rather than trusting a single mocking system.

Recommended stack:

## 13.1 Unit-level AWS mocks — Moto

Use:

```text
moto
pytest
boto3
```

Moto is appropriate for fast Python unit/integration tests involving services such as DynamoDB, SQS, SNS, and other supported AWS APIs.

Use it for:

- storage repository tests
- queue publisher/consumer tests
- SNS abstraction tests
- failure handling
- AWS client logic

Do not use Moto as the only evidence the complete architecture works.

## 13.2 Client-call validation — botocore Stubber

Use:

```python
botocore.stub.Stubber
```

where exact AWS API requests/responses should be asserted.

Useful for:

- verifying request shape
- testing error branches
- testing throttling/service errors
- confirming code does not accidentally depend on network access

## 13.3 Local AWS environment — LocalStack

Use LocalStack for system-level AWS integration tests.

Prefer Docker Compose so the environment is reproducible.

Exercise as many selected services as possible, such as:

- Lambda
- DynamoDB
- SQS
- SNS
- EventBridge / scheduler-equivalent trigger simulation
- API Gateway if used
- CloudWatch/logging where practical

If a particular LocalStack service requires a paid feature or is unreliable, document the limitation and emulate that boundary with the closest reliable alternative.

The agent must not silently claim unsupported LocalStack behavior was tested.

## 13.4 Lambda-local execution — AWS SAM CLI if useful

Use AWS SAM CLI where it improves local Lambda/API execution.

Examples:

```text
sam local invoke
sam local start-api
sam validate
```

If the project uses CDK rather than SAM for IaC, SAM may still be used only where useful for local invocation, but avoid needless tooling duplication.

## 13.5 Infrastructure validation

If using AWS CDK:

Required:

```text
cdk synth
```

and automated CDK assertion tests.

Also validate emitted CloudFormation using a linter such as:

```text
cfn-lint
```

If practical, use:

```text
cdklocal
```

to provision the synthesized stack into LocalStack and exercise it there.

If using SAM:

Required:

```text
sam validate
```

plus template tests and local invocation.

If using Terraform:

Required:

```text
terraform fmt -check
terraform validate
```

and a LocalStack-compatible integration strategy where practical.

The agent should choose one IaC framework and document why.

## 13.6 Recommended default

Unless a materially better implementation is found, prefer:

```text
Python
pytest
Moto
botocore Stubber
Docker Compose
LocalStack
AWS CDK (Python)
CDK assertions
cfn-lint
```

This combination should provide:

- fast component tests
- deterministic AWS-client tests
- local cloud-level integration tests
- deployable infrastructure definitions

---

# 14. Test Pyramid — Mandatory

The project is not done unless all applicable levels pass.

## Level 1 — Pure unit tests

Test:

- normalization
- filtering
- fingerprints
- canonical URL logic
- relevance scoring
- retry policy
- backoff calculation
- config loading
- parser helpers

## Level 2 — Scraper/provider tests

For every provider/custom scraper:

- parse fixture
- required data exists
- pagination works
- malformed records handled
- retry behavior
- 429 behavior
- 5xx behavior
- empty results
- URL validation
- duplicate source records
- stable IDs

Representative transient-failure sequence:

```text
500
500
200
```

Expected: success after retry.

Representative rate limit:

```text
429
Retry-After
```

Expected: controlled backoff behavior.

## Level 3 — Persistence tests

Test:

- insert new job
- reinsert same job
- first_seen stability
- last_seen update
- notification flag
- content update
- recent jobs query
- idempotency

## Level 4 — Notification tests

Test:

- correct notification payload
- email formatter
- push formatter
- multiple jobs
- deep link
- failed delivery handling
- duplicate notification prevention

Real email/push delivery does not need to occur during automated local testing.

## Level 5 — App/client tests

The app must have automated tests.

At minimum:

- application boots/renders
- recent jobs load
- empty state renders
- error state renders
- job detail renders
- application URL action works
- notification payload is accepted
- tapping/deep-linking from a notification routes to the correct job
- duplicate events do not create duplicate visible items
- malformed backend data fails gracefully

If React Native/Expo is selected, use an appropriate test stack such as:

```text
Jest
React Native Testing Library
```

If a PWA/web client is selected, use:

```text
Vitest/Jest
Testing Library
Playwright
```

as appropriate.

At least one end-to-end or integration-level client test should cover:

```text
new scraper job
→ backend record
→ notification payload
→ client/deep link/feed visibility
```

## Level 6 — Backend integration tests

Exercise:

```text
scrape
→ normalize
→ filter
→ fingerprint
→ persist
→ identify new job
→ notification event
```

Cases:

1. One new job.
2. Existing job.
3. One new + many existing.
4. Updated existing job.
5. One scraper fails.
6. One provider is rate-limited.
7. Storage temporarily errors.
8. Notification temporarily errors.
9. Retry succeeds.
10. Permanent failure lands in failure handling/DLQ path if architecture uses one.

## Level 7 — Local AWS architecture test

Using LocalStack and/or SAM local, exercise the selected architecture as realistically as possible.

Required scenario:

```text
scheduled event
→ coordinator
→ queue/orchestration
→ scraper worker(s)
→ DynamoDB
→ notification event
→ email/push adapter
→ API/feed
```

The test may use deterministic scraper fixtures while exercising real local AWS-emulated infrastructure.

At least one architecture test should also run a live-public scraper when safe and stable, but completion must not depend on an external site remaining online.

## Level 8 — Final acceptance / regression suite

Run the complete automated suite from a clean local environment.

A command similar to:

```bash
make verify
```

must:

1. lint
2. type-check where applicable
3. run unit tests
4. run scraper tests
5. run backend integration tests
6. run app tests
7. validate/synthesize infrastructure
8. boot LocalStack
9. run local cloud end-to-end tests
10. shut down test infrastructure
11. produce a concise pass/fail report

---

# 15. Hard Completion Gates

The agent may only state **DONE** when all of the following are true.

## Gate A — Scraper correctness

- Scraper abstraction works.
- Every supported provider has tests.
- Every custom scraper has tests.
- Required fields are validated.
- Pagination is tested.
- Retry behavior is tested.
- URLs are validated.
- A broken scraper cannot crash the whole system.

## Gate B — Core business logic

- filtering tested
- normalization tested
- fingerprints tested
- persistence tested
- first_seen/last_seen tested
- deduplication tested
- notification suppression tested

## Gate C — App/client

- app builds/runs locally
- component tests pass
- data loading tests pass
- notification/deep-link tests pass
- job link behavior is tested

## Gate D — Infrastructure

- IaC validation passes
- IaC synthesis passes
- IaC assertion/template tests pass
- no unresolved invalid resource references
- IAM/resource wiring has automated checks where practical

## Gate E — Local AWS architecture

- selected local cloud environment boots from scratch
- resources are created/emulated
- end-to-end architecture path succeeds
- queues/events/storage work
- retry/failure isolation is exercised
- no real AWS credentials are required

## Gate F — Full system

Prove:

```text
new internship
→ discovered
→ normalized
→ considered relevant
→ stored
→ notification created
→ exposed to app/feed
```

Then run the same input again and prove:

```text
0 duplicate notifications
```

Then deliberately fail one scraper and prove:

```text
remaining scrapers continue
```

## Gate G — Final review

The agent must reread this PRD and create a checklist mapping each requirement to:

```text
implemented
tested
blocked
not applicable
```

There must be no unresolved critical requirement marked `blocked`.

If a noncritical company-specific scraper is blocked, that is acceptable only if it is documented in `BLOCKERS.md` and does not break the required overall company coverage/system behavior.

## Gate H — Git

- tests pass
- generated junk/temp files excluded
- documentation updated
- working tree clean
- final commit exists

Only after all gates pass may the agent declare the project complete.

---

# 16. Failure Isolation

A single provider failure must never abort the full polling run.

Example:

```text
Company A: FAIL
Company B: OK
Company C: OK
```

B and C must still be processed.

Record:

```json
{
  "company": "Company A",
  "provider": "custom",
  "status": "failed",
  "attempts": 3,
  "error_type": "HTTPError",
  "error": "403",
  "timestamp": "..."
}
```

Use dead-letter/failure queues where appropriate in the selected architecture.

---

# 17. Retry Policy

Use bounded retries.

Handle:

- timeout
- connection reset
- DNS/network error
- HTTP 429
- HTTP 5xx
- temporary AWS-emulated service failure
- malformed transient response where retry is reasonable

Use exponential backoff with jitter.

Respect `Retry-After`.

Do not hammer sites.

---

# 18. Agent Self-Recovery / Blocker Protocol

The agent must not spend the entire implementation loop repeating the same failed approach.

For any blocker, maintain:

```text
BLOCKERS.md
```

Each blocker entry must include:

```text
ID
timestamp / stage
requirement affected
component
observed failure
expected behavior
attempts made
evidence/logs
current hypothesis
alternative hypotheses
next actions
severity
status
```

Statuses:

```text
open
deferred
revisiting
resolved
accepted-limitation
```

## 18.1 Three-attempt rule

If the same underlying problem fails after **three materially different attempts**, stop brute-forcing it.

Do the following:

1. Write/update the blocker in `BLOCKERS.md`.
2. Preserve error output or reproduction steps.
3. Identify whether it blocks a critical path.
4. Move to another independent task if possible.
5. Return to the blocker during a later blocker sweep.

A "materially different attempt" means changing the hypothesis or approach, not rerunning the same command three times.

## 18.2 Backtracking procedure

For a critical blocker, perform structured backtracking.

Start from the failed acceptance criterion and trace backward:

```text
acceptance criterion
    ↓
dependent integration
    ↓
component
    ↓
interface
    ↓
assumption
    ↓
external dependency / implementation decision
```

Then test the assumptions from the bottom upward.

Example:

```text
Push notification not visible
    ↓
client deep-link handler
    ↓
notification payload schema
    ↓
backend publisher
    ↓
event construction
    ↓
job ID serialization
```

Do not only patch the final symptom.

## 18.3 Tree-style reversal

When appropriate, construct a small fault tree:

```text
Failure: E2E job never appears in app
├── scraper didn't emit job
│   ├── endpoint wrong
│   ├── parser wrong
│   └── filtering removed it
├── storage didn't persist
│   ├── ID collision
│   └── DynamoDB write failed
├── notification path failed
│   ├── event not published
│   └── consumer failed
└── app path failed
    ├── API result absent
    └── client rendering/deep-link issue
```

Eliminate branches with targeted tests.

## 18.4 Alternative implementation branch

If the architecture/library itself appears to be the blocker:

1. Return to the requirement.
2. Identify what behavior is actually required.
3. List 2–3 substitute approaches.
4. Pick the simplest compatible alternative.
5. Implement behind the same interface if practical.
6. Update `ARCHITECTURE.md`.

Example:

```text
LocalStack scheduler emulation unreliable
```

Do not abandon EventBridge compatibility.

Instead:

- retain EventBridge-compatible IaC
- directly inject the same scheduled event payload locally
- test downstream architecture through LocalStack
- document exactly which scheduler boundary was simulated rather than natively exercised

## 18.5 Blocker sweep

At natural milestones, revisit all open/deferred blockers.

Before final completion:

- revisit every unresolved blocker
- retry with knowledge gained later
- resolve, downgrade with justification, or explicitly accept as noncritical limitation

No critical blocker may remain open.

---

# 19. Git / Commit Discipline

The project will live in Git.

The agent must make **frequent, coherent commits**.

Commit after meaningful milestones such as:

- repository/bootstrap setup
- scraper interface
- first ATS adapter
- provider test suite
- company registry
- filtering/dedup logic
- persistence layer
- notification layer
- app scaffold
- app tests
- infrastructure-as-code
- LocalStack integration
- end-to-end test
- documentation/final verification

Do not wait until the end to create the first commit.

Prefer small, understandable commits.

Suggested commit style:

```text
feat(scrapers): add greenhouse provider adapter
test(scrapers): add pagination and retry fixtures
feat(storage): add idempotent DynamoDB job repository
feat(infra): add localstack-compatible queue pipeline
test(e2e): verify new-job notification path
docs: document local AWS validation strategy
```

Before each milestone commit:

- run relevant tests
- do not knowingly commit broken generated state unless it is explicitly a temporary checkpoint and clearly labeled

Do **not** rewrite/delete useful history just to make the repository look perfect.

Do not push to a remote unless explicitly configured/authorized.

## Final commit

After all completion gates pass:

1. Run the full `make verify` equivalent.
2. Update README/ARCHITECTURE/BLOCKERS.
3. Confirm working tree is clean or only contains intentionally ignored files.
4. Create a final commit such as:

```text
chore: finalize validated internship monitor MVP
```

The final report must include the final commit hash.

---

# 20. Local Development Commands

Prefer a simple developer workflow.

Target commands:

```bash
make setup
make test
make test-scrapers
make test-app
make infra-validate
make local
make e2e
make verify
```

`make verify` should be the hard final gate.

Example LocalStack lifecycle:

```bash
docker compose up -d localstack
make local-provision
make e2e
docker compose down -v
```

The exact commands may change, but README must document them.

---

# 21. Infrastructure as Code

Choose one:

- AWS CDK
- AWS SAM
- Terraform

Preferred default for this project:

```text
AWS CDK in Python
```

unless another option creates a clearly simpler/testable architecture.

Potential resources:

```text
EventBridge schedule
Coordinator Lambda
SQS scraper queue
DLQ
Worker Lambda
DynamoDB jobs table
DynamoDB scraper-health table (or shared table)
notification topic/event
API Gateway
API Lambda
IAM roles/policies
CloudWatch alarms/log groups
```

Keep permissions least-privilege where practical.

Use deterministic logical structure suitable for local emulation.

---

# 22. Notification System

## Email

Email notifications are required in production design.

Local tests should use a mock/local sink rather than sending real email.

Example:

```text
NEW INTERNSHIP

Company: Stripe
Role: Software Engineer Intern
Location: San Francisco
First Seen: 2026-09-26T...
Posted: if available

Apply:
https://...
```

The agent should decide between per-job and batched notifications.

Default preference:

- immediate alert for high-priority jobs
- optionally grouped alerts for lower-priority jobs found in the same run

## Mobile Push

Implement a practical push path.

Possible choices:

- Expo push notifications
- Firebase Cloud Messaging
- AWS-supported mobile push
- another low-complexity solution

The agent should choose based on:

- ease of local testing
- ease of personal installation
- low cost
- reliability
- deep-link support

Push-provider calls must be abstracted so tests can use a fake transport.

---

# 23. Client/App

The client exists primarily to:

1. receive/display alerts
2. show recent jobs
3. open the original job application

Minimum screens:

## Recent jobs

Show:

```text
company
title
location
first_seen
priority/relevance if useful
```

## Job detail

Show:

```text
company
title
location
posted date if known
first seen
description preview
application URL
```

## Apply

Button:

```text
Open Application
```

Open original careers/ATS page.

Do not expose AWS credentials to the client.

---

# 24. API

If required by the client:

```text
GET /jobs
GET /jobs/{id}
GET /jobs/recent
POST /devices/register
```

Read endpoints should be simple.

Protect device-registration or write paths appropriately.

---

# 25. Observability

Track:

```text
scrapers attempted
scrapers succeeded
scrapers failed
jobs fetched
jobs normalized
jobs relevant
new jobs
updated jobs
notifications emitted
notifications failed
poll duration
queue depth
DLQ count
```

Structured logs should include:

```json
{
  "company": "Example",
  "provider": "greenhouse",
  "duration_ms": 400,
  "jobs_found": 32,
  "relevant_jobs": 2,
  "new_jobs": 1,
  "status": "success"
}
```

Create a simple scraper-health view or command.

Example:

```text
Stripe       OK      2m ago
Datadog      OK      2m ago
Company X    FAIL    HTTP 403
Company Y    FAIL    parser mismatch
```

CLI, JSON, or lightweight developer page is acceptable.

---

# 26. Configuration / Secrets

Provide:

```text
.env.example
```

Never commit:

- AWS access keys
- notification-provider secrets
- email credentials
- mobile tokens
- private keys

Use environment variables locally.

Production-ready design may use SSM Parameter Store or Secrets Manager.

Tests must provide fake values.

---

# 27. Cost Target

This is a personal project.

Target production cost:

```text
approximately $0–$10/month
```

when practical.

Assume:

```text
144 polling intervals/day
100–150 companies
one primary user
```

Document rough costs for:

- Lambda
- SQS
- DynamoDB
- EventBridge
- API Gateway
- notification services
- logs

---

# 28. Repository Structure

Suggested:

```text
job-monitor/
├── README.md
├── PRD.md
├── ARCHITECTURE.md
├── BLOCKERS.md
├── companies.json
├── pyproject.toml
├── Makefile
├── docker-compose.yml
├── .env.example
├── .gitignore
│
├── src/
│   ├── models/
│   ├── scrapers/
│   │   ├── base.py
│   │   ├── greenhouse.py
│   │   ├── lever.py
│   │   ├── ashby.py
│   │   ├── workday.py
│   │   └── custom/
│   ├── filtering/
│   ├── storage/
│   ├── notifications/
│   ├── orchestration/
│   └── api/
│
├── infrastructure/
│   ├── app.py
│   ├── stacks/
│   └── tests/
│
├── mobile/
│
├── tests/
│   ├── unit/
│   ├── scrapers/
│   ├── fixtures/
│   ├── integration/
│   ├── aws_local/
│   └── e2e/
│
└── scripts/
```

Change structure if a better organization is justified.

---

# 29. Development Order

## Phase A — Bootstrap / Git

- initialize/inspect Git repository
- baseline commit
- tooling
- lint/test config
- Makefile
- Docker Compose
- README skeleton
- BLOCKERS.md

Commit.

## Phase B — Company Discovery

- collect Simplify companies
- collect Vansh/Ouckah companies
- normalize aliases
- research additional companies
- determine official careers URLs
- determine ATS/provider
- create 100–150-company registry

Commit.

## Phase C — Scraper Framework

- Job model
- source interface
- HTTP client
- retry system
- fixture system
- first provider adapters
- unit tests

Commit in coherent increments.

## Phase D — Provider Coverage

Implement reusable adapters first.

For each supported source:

```text
research endpoint
configure/implement
save fixture
parse
test
validate required data
```

Commit frequently.

## Phase E — Filtering / Dedup

Implement and test.

Commit.

## Phase F — Storage

Implement local + AWS abstraction.

Test with Moto.

Commit.

## Phase G — Orchestration

Implement coordinator/queue/workers or selected alternative.

Test isolated behavior.

Commit.

## Phase H — Notifications

Implement transport abstraction.

Add fake/local sinks.

Test.

Commit.

## Phase I — API + App

Implement minimal client.

Add client tests.

Commit.

## Phase J — Infrastructure

Implement IaC.

Run:

```text
synth
lint
assertion tests
```

Commit.

## Phase K — Local AWS Integration

Boot LocalStack.

Provision/emulate resources.

Exercise architecture.

Fix issues using blocker/backtracking protocol.

Commit.

## Phase L — E2E

Run full scenario.

Commit.

## Phase M — Blocker Sweep

Revisit all unresolved blockers.

Commit fixes.

## Phase N — Final Verification

Run:

```bash
make verify
```

Reread PRD.

Produce requirement matrix.

Update docs.

Create final commit.

---

# 30. Required E2E Acceptance Scenario

The final local E2E suite must prove this exact story.

## Scenario 1 — New job

A deterministic test careers source produces:

```text
Company: TestCo
Role: Software Engineer Intern
URL: https://example...
```

System processes:

```text
schedule event
→ orchestration
→ scraper
→ normalized Job
→ relevance filter
→ fingerprint
→ storage lookup
→ new record
→ notification event
→ email fake transport
→ push fake transport
→ API/feed
```

Assert:

- exactly one new record
- exactly one notification event (or one per configured channel)
- app/feed contains job
- deep link identifies job
- application URL is preserved

## Scenario 2 — Same poll again

Run exact same source.

Assert:

- no new job
- no duplicate notification
- first_seen unchanged
- last_seen updated

## Scenario 3 — Mixed

Source includes:

- 10 existing
- 1 new

Assert:

- 1 notification
- 11 stored/updated correctly

## Scenario 4 — Scraper failure

One scraper raises permanent error.

Other scrapers return jobs.

Assert:

- failed scraper logged
- retry limit respected
- successful scrapers continue
- results persist
- no full-run crash

## Scenario 5 — Transient failure

Scraper returns:

```text
500
500
200
```

Assert recovery.

## Scenario 6 — Rate limit

Scraper returns:

```text
429
```

Assert controlled retry/backoff.

## Scenario 7 — Queue/worker failure

If queue architecture is used:

- worker fails
- message retry occurs
- permanent failure follows configured DLQ path

## Scenario 8 — App notification path

Inject backend notification payload.

Assert:

- app accepts payload
- tapping it resolves expected job/deep link
- displayed job matches backend record

---

# 31. Live Scraper Validation

Fixtures are mandatory for determinism, but the agent should also validate selected real public endpoints where allowed.

For supported providers, spot-check representative companies.

Validation should answer:

- does endpoint currently respond?
- do we parse actual current jobs?
- are application links valid?
- is pagination accounted for?
- are IDs stable?

Do not make the full test suite dependent on every external careers site.

Record the date of live validation.

---

# 32. Coverage Reporting

Generate a coverage report from the company registry.

Example:

```text
Total companies researched: 145
Supported: 112
Partial: 12
Blocked: 8
Needs browser/manual: 6
Research needed: 7
```

Also break down:

```text
Greenhouse: 35
Lever: 18
Ashby: 22
Workday: 17
SmartRecruiters: 8
Custom: 24
Unsupported: ...
```

Do not claim a company is supported unless its configured source passes the appropriate scraper test.

---

# 33. Auto-Apply — Optional Phase

Do not prioritize auto-apply until all hard completion gates for monitoring pass.

Preferred future flow:

```text
notification
→ user opens job
→ Prepare Application
→ prefill known information
→ user reviews
→ user explicitly submits
```

Do not automatically submit applications without explicit user approval.

Do not bypass:

- CAPTCHA
- login requirements
- anti-bot controls
- site restrictions

Potential stored profile fields:

```text
name
email
phone
resume
university
graduation date
work authorization
LinkedIn
GitHub
portfolio
```

---

# 34. Agent Autonomy

The agent may:

- change architecture
- change libraries
- choose mobile framework
- choose provider implementations
- change repository layout
- use reusable ATS adapters
- select DynamoDB alternatives
- select push provider
- adjust concurrency

But it must preserve requirements and document major decisions in:

```text
ARCHITECTURE.md
```

When changing direction, optimize for:

1. correctness
2. reliability
3. local verifiability
4. AWS deployability
5. low maintenance
6. low cost

---

# 35. Prohibited Shortcuts

Do not declare completion because:

- files were generated
- unit tests alone pass
- mocks alone pass
- one scraper works
- IaC syntactically exists
- LocalStack starts but architecture is not exercised
- app renders without notification tests
- fixtures work but parser fields are not validated
- architecture diagrams look correct
- agent "believes" AWS will work

Evidence is required.

Do not comment out failing tests to pass the suite.

Do not silently skip tests.

If a test is intentionally skipped, document why and ensure it is not part of a hard completion gate.

---

# 36. Documentation Deliverables

Must include:

## README.md

- purpose
- prerequisites
- setup
- local run
- test commands
- LocalStack commands
- app run
- adding a company
- adding a provider
- debugging
- deployment instructions for later user use
- explicit warning that this implementation did not deploy real AWS resources

## ARCHITECTURE.md

- selected design
- diagram
- alternatives
- tradeoffs
- AWS services
- local simulation mapping
- known emulation gaps
- cost estimate

## BLOCKERS.md

- all blockers encountered
- resolution
- unresolved noncritical limitations

## TESTING.md

Strongly recommended.

Explain:

- test pyramid
- fixtures
- Moto
- Stubber
- LocalStack
- client testing
- E2E
- what is real vs mocked/emulated

## COMPANY_COVERAGE.md

- total researched
- total supported
- provider breakdown
- unsupported reasons

---

# 37. Final Requirement Matrix

Before completion, produce a machine/human-readable matrix such as:

```text
Requirement                         Status        Evidence
---------------------------------------------------------------
Greenhouse parser                   implemented   tests/...
Deduplication                      implemented   test_...
Push payload                       implemented   test_...
App deep link                      implemented   mobile/...
CDK synth                          passed        command output
LocalStack E2E                     passed        test_...
One scraper failure isolation      passed        test_...
100–150 company registry           passed        companies.json
Real AWS deployment                N/A            intentionally prohibited
```

Store this as:

```text
VALIDATION_REPORT.md
```

---

# 38. Final Engineering Report

At the end, report:

1. Final architecture.
2. Why it was chosen.
3. Number of companies researched.
4. Number successfully monitored.
5. Provider breakdown.
6. Unsupported companies and reasons.
7. Test counts/results.
8. Local AWS E2E result.
9. IaC validation/synthesis result.
10. Mobile/client test result.
11. Known LocalStack/SAM emulation gaps.
12. Estimated AWS monthly cost if later deployed.
13. Exact local commands.
14. Exact future deployment command (do not execute it).
15. Remaining manual configuration.
16. Open noncritical blockers.
17. Final Git commit hash.

Do not claim anything that was not actually tested.

---

# 39. Final Definition of Done

The project is done only when:

```text
ALL APPLICABLE COMPONENT TESTS PASS
AND
ALL SUPPORTED SCRAPER TESTS PASS
AND
APP TESTS PASS
AND
BACKEND INTEGRATION TESTS PASS
AND
IaC VALIDATES/SYNTHESIZES
AND
LOCAL AWS ARCHITECTURE E2E PASSES
AND
DUPLICATE SUPPRESSION PASSES
AND
FAILURE ISOLATION PASSES
AND
CRITICAL BLOCKERS = 0
AND
FINAL PRD REQUIREMENT REVIEW PASSES
AND
FINAL GIT COMMIT EXISTS
AND
WORKING TREE IS CLEAN
```

Only then may the agent write:

```text
DONE
```

If any critical completion gate cannot be satisfied, the correct final state is:

```text
NOT DONE
```

followed by the precise blocker, evidence, attempts, and the next technically plausible path.

Never replace missing validation with confidence or assumption.
