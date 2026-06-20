# terraformdriftmonitor

Monitors Terraform provider version drift across GitHub repos — flags
when a pinned provider version (e.g. `aws ~> 5.0`) falls behind what
HashiCorp's registry has available, with AI-assisted changelog analysis
on major version bumps to judge whether breaking changes actually
affect resources you use.

This project is a deliberate continuation of
[fasthd97/driftmonitor](https://github.com/fasthd97/driftmonitor),
which monitors AWS CloudFormation stacks for Lambda runtime EOL drift.
That tool is already deployed and running. This repository extends the
same underlying idea — catching silent dependency drift before it
causes an incident — into the Terraform/provider-version space, with a
full CI/CD security pipeline built around it.

> **Status:** actively being built. Sections below marked
> `(coming soon)` cover features not yet implemented.

---

## How it works

```
EventBridge (schedule)
       ↓
   Lambda
       ↓
1. Reads configured repo list from SSM
2. Fetches .tf files from each repo via GitHub API
3. Parses provider version constraints (HCL)
4. Checks each provider against the HashiCorp registry API
5. For CRITICAL findings (major version available), AI extracts
   breaking changes from the provider's release notes — cached,
   one call per provider+version, ever — see lambda/checks/ai_changelog.py
       ↓
6. Findings → SNS (email) + CloudWatch logs
```

---

## Architecture

Two **separate** Terraform roots, deliberately decoupled:

| Root | Applied by | Contains |
|---|---|---|
| `terraform/bootstrap/` | A human, manually, once | OIDC provider, GitHub Actions deploy role + permissions boundary, deployments bucket, SES (AURORA alert), SNS (pipeline alerts), SSM placeholders |
| `terraform/lambda/` | The CI/CD pipeline, every deploy | The actual monitoring Lambda, its own execution role, analysis cache bucket, drift-finding SNS topic, EventBridge schedule, self-monitoring alarms |

**Why separate:** the deploy role that the pipeline assumes lives in
`bootstrap/`. If the pipeline's own Terraform could modify that root,
a compromised pipeline could escalate its own permissions. Keeping
them apart means the pipeline can only ever touch what's in
`terraform/lambda/` — nothing else.

### Deployment order — hard requirement

**`terraform/bootstrap/` MUST be applied before `terraform/lambda/`,
every time.** The lambda root reads bootstrap's SSM parameters via
`data "aws_ssm_parameter"` lookups (not constructed ARNs) specifically
so that if bootstrap hasn't been applied yet, `terraform plan` on the
lambda root fails immediately with a clear "parameter not found"
error — rather than letting a misconfiguration through to a confusing
runtime failure inside the deployed Lambda later.

---

## Setup

### 1. Bootstrap (one-time, manual)

```bash
cd terraform/bootstrap
terraform init
terraform plan \
  -var="ses_alert_email=you@example.com" \
  -var="sns_alert_email=you@example.com"
terraform apply \
  -var="ses_alert_email=you@example.com" \
  -var="sns_alert_email=you@example.com"
```

After apply, the output includes a checklist — confirm both the SES
and SNS subscription emails, then fill in the SSM placeholders:

```bash
aws ssm put-parameter --name "/tfdriftmonitor/anthropic-api-key" --value "sk-ant-..." --type SecureString --overwrite --region us-east-1
aws ssm put-parameter --name "/tfdriftmonitor/github-token" --value "ghp_..." --type SecureString --overwrite --region us-east-1
aws ssm put-parameter --name "/tfdriftmonitor/terraform-repos" --value '{"repos":[...]}' --type SecureString --overwrite --region us-east-1
```

See `terraform-repos-example.md` for repo config examples.

Set the GitHub repo variables (Settings → Secrets and variables →
Actions → Variables) using the bootstrap outputs:

```bash
gh variable set AWS_ROLE_ARN --body "<github_actions_role_arn output>"
gh variable set DEPLOYMENTS_BUCKET --body "<deployments_bucket_name output>"
gh variable set AWS_REGION --body "us-east-1"
```

### 2. Lambda root (deployed by the pipeline)

Not yet wired into a deploy workflow step — see roadmap. Can be
applied manually for now, same dependency order:

```bash
cd terraform/lambda
terraform init
terraform plan -var="alert_email=you@example.com"
terraform apply -var="alert_email=you@example.com"
```

---

## Configuration

All settings in `terraform/lambda/variables.tf`. Override with `-var`
or a `terraform.tfvars` file.

| Variable | Default | Description |
|---|---|---|
| `aws_region` | `us-east-1` | Must match the region bootstrap was applied to |
| `project_name` | `tfdriftmonitor` | Must match bootstrap's value exactly |
| `alert_email` | *(required)* | Drift-finding SNS notifications |
| `schedule_expression` | `rate(7 days)` | How often the check runs |
| `ai_model` | `claude-sonnet-4-6` | Swap to compare model performance on changelog extraction |
| `ai_effort` | `medium` | Anthropic API effort level (`low`/`medium`/`high`/`max`) |
| `ai_analysis_severity_threshold` | `CRITICAL` | Minimum severity that triggers an AI changelog call |
| `ai_changelog_cache_ttl_hours` | `2160` (90 days) | `0` = indefinite — valid here since a given provider version's release notes never change |
| `lambda_timeout_seconds` | `300` | See reasoning below |
| `lambda_memory_mb` | `256` | See reasoning below |
| `log_retention_days` | `30` | CloudWatch log retention |

### Lambda timeout & memory — reasoning, and where to adjust

**Where to change these:** `terraform/lambda/variables.tf` →
`lambda_timeout_seconds` and `lambda_memory_mb`.

**Why 300 seconds:** this Lambda's per-run work is: read SSM config →
fetch `.tf` files per configured repo (GitHub API) → check each
provider against the registry (HashiCorp API) → for CRITICAL findings
not already cached, call Claude for changelog extraction. A small
config (1-3 repos) finishes in well under a minute. The default is
sized for the worst case: many configured repos, several hitting
CRITICAL on the same cache-cold run, meaning multiple un-cached AI
calls happen back to back in one invocation. Each of those can take
several seconds, more at higher `ai_effort` settings.

**Why 256 MB:** counterintuitively, this isn't really about RAM
capacity — this workload barely uses any memory (no large HTML
payloads, just small JSON/text). The real reason is that AWS Lambda
ties memory allocation to CPU and **network throughput**. This Lambda
is almost entirely I/O-bound — a chain of small HTTP calls to GitHub,
HashiCorp, and Anthropic. Dropping to AWS's 128 MB minimum could make
those network calls slower, which could *increase* timeout risk rather
than just save a few cents — the opposite of the intended savings.

If you expand this project significantly (many more repos, much
larger `.tf` files, higher `ai_effort` as the default), re-check these
values against your actual usage rather than assuming the defaults
still fit.

---

## How AI changelog costs actually scale

**The short version: cost scales with the number of distinct
`(provider, major-version-boundary)` combinations across your fleet —
not with the number of repos you monitor.**

This surprises people at first, so here's the mechanism in detail.

### The cache key is the whole story

`lambda/checks/ai_changelog.py` caches every AI extraction result in
S3, keyed by **provider + version only**:

```
changelog-analysis/hashicorp-aws-6.0.0.json
```

Notice what's *not* in that key: which repo asked for it. That one
design choice is what decouples cost from repo count.

### Worked example — 10 repos, same provider

| Step | What happens | AI call? | Why |
|---|---|---|---|
| Repo 1 checked, pinned `aws ~> 5.0`, CRITICAL finding | Cache miss for `hashicorp/aws@6.0.0` | **Yes — 1 call** | First time this exact question has been asked |
| Repo 2 checked, also pinned `aws ~> 5.0` | Cache **hit** for the same key | No | Identical question, already answered |
| Repo 3 through Repo 10, same situation | Cache **hit**, every time | No | Same as above |
| **Total for all 10 repos** | | **1 AI call** | Adding repos 2-10 cost nothing extra |

### What actually *does* trigger a new call

| Situation | New AI calls | Why |
|---|---|---|
| 10 repos, all on `aws ~> 5.0` | 1 | Same cache key for all 10 |
| 10 repos, each on a *different* provider (aws, azurerm, google...) hitting CRITICAL | Up to 10 | Each provider is a genuinely different question |
| 10 repos all on `aws`, but spread across different pinned majors (some `~>3.0`, some `~>4.0`, some `~>5.0`) | Up to 3 | One call per distinct major-version boundary crossed — not per repo |
| Same 10 repos, checked again next scheduled run, nothing changed | 0 | Still cached from before |
| AWS eventually ships `7.0.0` someday | 1 (whenever that actually happens) | A genuinely new question — unrelated to how many repos you have |

### The actual rule

Cost is driven by **how many distinct "what changed going from major
version X to X+1" questions exist** across everything you monitor —
not by how many places are asking that same question. An organization
with 50 repos that mostly standardize on similar provider version
ranges will see this specific cost stay close to flat as they add more
repos, since most additions just produce cache hits, not new API calls.

### Real cost per call, for reference

Pricing for `claude-sonnet-4-6` (current as of this writing — check
[Anthropic's pricing page](https://www.anthropic.com/pricing) for
current rates) is $3 per million input tokens, $15 per million output
tokens.

| Scenario | Rough cost per call |
|---|---|
| Typical release (few or no breaking changes) | ~$0.02 |
| Large release with many breaking changes (e.g. a major version bump like AWS provider 6.0.0) | ~$0.14 |

Both numbers are **one-time costs per provider+version**, not
per-repo, per-run costs — see the tables above for why.



Every run — scheduled or manual — writes detailed step-by-step output
to CloudWatch. This is where you actually see *why* a run found what
it found, or why it failed, in far more detail than the SNS email
provides.

### The easy way — `aws logs tail`

For almost everything, this one command is all you need:

```bash
aws logs tail /aws/lambda/tfdriftmonitor --region us-east-1
```

This fetches the most recent log events automatically — no need to
look up a stream name first. Useful flags:

```bash
# Follow new log output live, as it happens (like `tail -f`)
aws logs tail /aws/lambda/tfdriftmonitor --region us-east-1 --follow

# Only show the last 10 minutes
aws logs tail /aws/lambda/tfdriftmonitor --region us-east-1 --since 10m

# Only show logs from the last hour, formatted with short timestamps
aws logs tail /aws/lambda/tfdriftmonitor --region us-east-1 --since 1h --format short
```

Use `--follow` right after triggering a manual run (see below) to
watch it execute in real time.

### The manual way — when you need more control

`aws logs tail` covers most cases, but sometimes you want a *specific*
run's output, or to fetch a stream's events as a single block. That
requires two steps, because CloudWatch organizes logs into **streams**
(roughly: one stream per "warm" Lambda execution environment, which
can contain several invocations) and you need a stream's exact name
before you can read its events.

**Step 1 — find the most recent stream name:**

```bash
aws logs describe-log-streams \
  --log-group-name /aws/lambda/tfdriftmonitor \
  --region us-east-1 \
  --order-by LastEventTime \
  --descending \
  --output text \
  --query "logStreams[0].logStreamName"
```

This prints something like:
```
2026/06/20/[$LATEST]3b6cfff605e043bd9b9084e41a09247f
```

**Step 2 — fetch that stream's events:**

```bash
aws logs get-log-events \
  --log-group-name /aws/lambda/tfdriftmonitor \
  --log-stream-name 'PASTE_THE_STREAM_NAME_FROM_STEP_1_HERE' \
  --region us-east-1 \
  --query "events[].message" \
  --output text
```

**The one gotcha that will bite you:** the stream name contains a
literal `$` (in `[$LATEST]`). **Always wrap it in single quotes**, not
double quotes. In bash/zsh, double quotes still let `$LATEST` be
interpreted as an (empty, undefined) shell variable — silently
mangling the stream name into something that doesn't exist, and
you'll get a confusing "log stream does not exist" error that has
nothing to do with your actual logs. Single quotes prevent any of that
substitution.

```bash
# WRONG — $LATEST gets expanded by the shell before AWS ever sees it
--log-stream-name "2026/06/20/[$LATEST]3b6cfff..."

# RIGHT — single quotes, no shell expansion happens
--log-stream-name '2026/06/20/[$LATEST]3b6cfff...'
```

### Triggering a manual run to test

You don't have to wait for the weekly schedule. Trigger a run on
demand and immediately follow its logs:

```bash
aws lambda invoke \
  --function-name tfdriftmonitor \
  --payload '{"manual": true}' \
  --cli-binary-format raw-in-base64-out \
  response.json \
  --region us-east-1 \
  && cat response.json
```

The `response.json` output gives you a one-line summary
(`{"statusCode": 200, "body": "..."}`) — for the full step-by-step
detail behind that summary, follow up with `aws logs tail` (or the
manual two-step approach above) right after.



- [ ] `lambda/handler.py` + `lambda/notifier.py` — application entry point
- [ ] Test fixtures + unit tests for the version-diff and HCL parsing logic
- [ ] `preflight.py` — role + code integrity checking, JIT permission grant/revoke
- [ ] `incident.py` — tamper response (silent revoke, out-of-band alert)
- [ ] `prod.yml` — approval-gated production pipeline
- [ ] bandit, pip-audit, gitleaks layered onto the existing checkov-based `dev.yml`
- [ ] PR template + branch protection rules
- [ ] Negative test proving tamper detection actually works
- [ ] Remote Terraform state with locking (currently local state — see TROUBLESHOOTING.md for the risk this carries)
- [ ] Windows PowerShell build script (`scripts/build.ps1`) for local
      `terraform apply` on Windows. Not required — the actual pipeline
      runs on Linux GitHub Actions runners. The `build_script` Terraform
      variable already supports swapping this in once built, with no
      changes needed elsewhere.
- [ ] Multi-vendor AI model support (OpenAI, Gemini) — deliberately deferred, Claude-only for now
- [ ] SES sender migrated to a verified custom domain (cosmetic, not a security requirement)

---

## Known scaling limitations (honest, not yet addressed)

The current design works well for a small-to-moderate number of
repos (tens, maybe low hundreds). It does **not** scale to enterprise
fleets (thousands of repos) as-is — and importantly, the AI changelog
cache (S3-based) is **not** the part that breaks. Two unrelated things
would break first:

### 1. Repo config storage hits a hard size ceiling

The full repo list is stored as one JSON blob in a single SSM
parameter. SSM Parameter Store caps out around 8KB per parameter
even on the "advanced" tier. A list of a few thousand repo entries
(each with name, repo path, branch, paths array, token reference)
would exceed that — not a performance issue, a hard wall the config
simply wouldn't fit through.

**Real fix:** move repo configuration to something built for this —
DynamoDB, or even just one S3 object per repo instead of one giant
blob. SSM was the right choice for the current scale; it isn't at
enterprise scale.

### 2. The execution model is sequential, not parallel

One Lambda invocation processes every configured repo in a loop,
within a single execution. Even ignoring AI calls entirely, a few
thousand repos at a couple seconds of GitHub API latency each adds
up to hours of sequential work — far past Lambda's hard 15-minute
maximum, regardless of any timeout tuning.

**Real fix:** a fan-out architecture — a dispatcher that pushes one
queue message per repo (e.g. SQS), consumed by a pool of *concurrent*
worker Lambdas, rather than one Lambda doing everything in a loop.

### What doesn't need to change

The S3-based AI changelog cache scales fine as-is. S3 has no
meaningful concurrency ceiling for this access pattern (exact-key
read/write), and cache hits stay cheap and instant regardless of how
many repos or workers are asking — see "How AI changelog costs
actually scale" above for why repo count was never the cost driver
in the first place.

**Not built now, deliberately** — this is a genuinely different
architecture, not a tuning change, and out of scope for the current
timeline. Documenting the real bottlenecks precisely now so the next
iteration doesn't have to rediscover them from scratch.



- `TROUBLESHOOTING.md` — known gotchas hit during real setup (state loss, OIDC trust issues, email privacy, hidden dotfiles)
- `terraform-repos-example.md` — repo configuration examples, single repo through org-wide multi-repo setups
