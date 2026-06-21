# Checkov Findings — Disposition and Rationale

**Project:** terraformdriftmonitor
**Scan tool:** Checkov, against `terraform/bootstrap/`, `terraform/lambda-infra/`, `terraform/lambda-code/`
**Total findings:** 36 (4 remediated in code, 32 risk-accepted with documented rationale below). One finding (CKV2_AWS_62 on `aws_s3_bucket.analysis_cache`) was identified after the initial 35-finding triage, via a real pipeline run that still failed after the other 31 were correctly dispositioned — added here with the same reasoning already established for the other two buckets sharing this check.
**Reviewed:** 2026-06-21

## Purpose

This document records the disposition of every Checkov finding in this
project. Each finding is addressed independently, on its own merits —
security controls are layered defense in depth, and a control being
risk-accepted for one resource does not imply the same disposition for
a different resource, even when the underlying check ID is identical.
Where two findings share genuinely overlapping reasoning, this document
says so explicitly and cross-references the related entry, rather than
merging them into a single combined rationale.

This document is written for a reader with no prior context on this
project's design discussions — every claim below is self-contained and
verifiable directly against the codebase.

Each risk-accepted finding is marked with a `#checkov:skip=<CHECK_ID>:<short
reason>` comment immediately above the affected resource in the Terraform
source, cross-referencing this document. Five findings (CKV_AWS_108,
109, 110, 111, 356 — all against the permissions boundary's `data
"aws_iam_policy_document"` block) are an exception: when tested,
inline skip comments on that block did not actually suppress these
findings — confirmed by a real failed pipeline run despite the
comments being present and correctly formatted. The exact root cause
in checkov was not conclusively identified. Their inline comments are
kept for explanatory value, but the actual, verified-working
suppression for those five is declared via the `skip_check` parameter
on the checkov action in `.github/workflows/dev.yml` instead.

---

## Remediated findings (4)

| Check | Resource | Fix applied |
|---|---|---|
| CKV_AWS_26 | `aws_sns_topic.pipeline_alerts` | Added `kms_master_key_id = "alias/aws/sns"` |
| CKV_AWS_26 | `aws_sns_topic.drift_findings` | Added `kms_master_key_id = "alias/aws/sns"` |
| CKV_AWS_300 | `aws_s3_bucket_lifecycle_configuration.deployments` | Added an `abort_incomplete_multipart_upload` rule |
| CKV_AWS_115 | `aws_lambda_function.drift_monitor` | Set `reserved_concurrent_executions = 2` instead of leaving concurrency unbounded |

All four were low-cost, no-tradeoff changes with no architectural
impact, so they were fixed rather than accepted as risk.

---

## Risk-accepted findings (31)

### CKV_AWS_337 — `aws_ssm_parameter.anthropic_api_key`

**Check:** SSM parameter should be encrypted with a customer-managed
KMS key (CMK), not an AWS-managed key.

**Analysis:** this parameter holds the Anthropic API key, currently
encrypted with AWS's managed key `alias/aws/ssm`. A CMK would require
its own key policy explicitly granting the SSM service principal and
the two IAM identities that need decrypt access (the Lambda execution
role and, during `plan`/`apply`, the CI/CD deploy role) — a new,
separate permission surface from the one already governing read access
to this parameter via IAM. Read access is the control actually
preventing unauthorized retrieval of this value; it is already scoped
to exactly those two identities.

**Decision:** risk-accepted. A CMK defends against a narrower
additional threat — compromise at the AWS-managed-key layer itself —
judged disproportionate to this project's scale for the added key
management cost (~$1/month plus per-request charges) and complexity.

### CKV_AWS_337 — `aws_ssm_parameter.github_token`

**Check:** same requirement as CKV_AWS_337 on `aws_ssm_parameter.anthropic_api_key` above — CMK encryption.

**Analysis:** this parameter holds an optional GitHub personal access
token, used only when scanning private repositories. The same
reasoning applies as for CKV_AWS_337 on `aws_ssm_parameter.anthropic_api_key`
above: IAM scoping, not key ownership, is the control limiting access. This
finding is evaluated independently because the two parameters have different
blast-radius profiles if ever compromised (a leaked GitHub token
exposes read access to configured repos; a leaked Anthropic key
exposes API spend) — but the same cost/complexity tradeoff applies to
both, and the conclusion is the same.

**Decision:** risk-accepted, for the reasons given under CKV_AWS_337 on
`aws_ssm_parameter.anthropic_api_key` above.

### CKV_AWS_337 — `aws_ssm_parameter.terraform_repos_config`

**Check:** same requirement as CKV_AWS_337 on `aws_ssm_parameter.anthropic_api_key` and `aws_ssm_parameter.github_token` above — CMK encryption.

**Analysis:** this parameter holds the JSON configuration describing
which repositories to scan — repo names, branches, and paths. Unlike
CKV_AWS_337 on `aws_ssm_parameter.anthropic_api_key` and `aws_ssm_parameter.github_token`
above, this value is not itself a credential; it
reveals which repos are monitored, not how to access them. This makes
the case for CMK encryption weaker here than for the two genuinely
secret values, not equivalent to them.

**Decision:** risk-accepted, on stronger grounds than CKV_AWS_337 on
`aws_ssm_parameter.anthropic_api_key` and `aws_ssm_parameter.github_token`
above — this value carries less sensitivity to begin with.

### CKV_AWS_145 — `aws_s3_bucket.deployments`

**Check:** S3 bucket should be encrypted by default using a CMK
rather than SSE-S3 (AES256).

**Analysis:** this bucket holds packaged Lambda deployment artifacts.
Access is restricted via bucket policy and IAM to the GitHub Actions
deploy role exclusively. Bucket contents (compiled Python dependencies
and source code already public in this repository) are not
confidential.

**Decision:** risk-accepted. Encrypting non-sensitive build artifacts
with a CMK adds cost and key-management complexity without a
corresponding confidentiality benefit, since the contents are already
public.

### CKV_AWS_145 — `aws_s3_bucket.lambda_state`

**Check:** same requirement as CKV_AWS_145 on `aws_s3_bucket.deployments` above — CMK default encryption.

**Analysis:** this bucket holds Terraform state for
`terraform/lambda-code/`. Unlike CKV_AWS_145 on `aws_s3_bucket.deployments`
above, state files conventionally can contain sensitive values, so this finding is
evaluated on its own footing rather than assumed equivalent to
that finding. In this specific project, the state in
question tracks only the Lambda function, its build artifacts, and an
EventBridge schedule — no secret values are stored in this state,
since all credentials are read from SSM at runtime rather than passed
through Terraform configuration.

**Decision:** risk-accepted, verified against this project's actual
state contents rather than assumed safe by category.

### CKV_AWS_145 — `aws_s3_bucket.analysis_cache`

**Check:** same requirement as CKV_AWS_145 on `aws_s3_bucket.deployments` and `aws_s3_bucket.lambda_state` above — CMK default encryption.

**Analysis:** this bucket exclusively caches AI-generated summaries of
publicly available software changelogs (e.g. HashiCorp provider
release notes). The cached content has no confidentiality requirement
at all — it is a cached copy of public information.

**Decision:** risk-accepted, with the least ambiguity of CKV_AWS_145's
three findings (on `aws_s3_bucket.deployments`, `aws_s3_bucket.lambda_state`,
and this bucket) — there is no sensitive data in this bucket under any
threat model.

### CKV_AWS_158 — `aws_cloudwatch_log_group.drift_monitor`

**Check:** CloudWatch log group should be encrypted with a CMK.

**Analysis:** this log group contains the Lambda's execution logs —
provider version numbers, repository names, and AI-extracted
changelog summaries. None of these values are credentials; the actual
secrets (API keys, tokens) are deliberately never logged by the
application code. Access to this log group is restricted by IAM to
the same two identities already governing every other resource in
this project.

**Decision:** risk-accepted. The data in this log group does not carry
a confidentiality requirement beyond what IAM access scoping already
provides.

### CKV_AWS_173 — `aws_lambda_function.drift_monitor`

**Check:** Lambda environment variables should be encrypted with a
CMK rather than the Lambda service's default encryption.

**Analysis:** the environment variables on this function are SSM
*parameter paths* (e.g. `/tfdriftmonitor/anthropic-api-key`) — strings
identifying where to look up a secret, not the secret value itself.
The actual credential is fetched at runtime via the AWS SDK and never
appears in the function's configuration. This finding is materially
lower-risk than the same check would be on a function that stored
literal secret values in its environment.

**Decision:** risk-accepted. There is no secret value present in this
function's environment configuration for a CMK to protect.

### CKV_AWS_108 — `data.aws_iam_policy_document.deploy_role_boundary`

**Check:** IAM policy should not permit data exfiltration actions
without resource constraints.

**Analysis:** this finding is against the permissions *boundary* — a
policy whose function is to define the maximum permissions a role can
ever hold, not to grant access on its own. The boundary contains
`Resource: "*"` for `sns:ListTopics`, which has no resource-scoped form
in the AWS API at all (SNS provides no "look up by name" call; any
caller must list all topics and filter). This is a structural
requirement of the boundary needing to cap that action type, not a
broad grant. The role's actual attached permissions policy
(`github_actions_deploy_permissions`), which is what governs real
access day to day, scopes every action to a specific resource ARN
wherever AWS's API makes that possible — verified directly against
that policy's source.

**Decision:** risk-accepted. The wildcard exists in a ceiling policy
covering one API limitation, not in the policy that actually grants
access.

### CKV_AWS_109 — `data.aws_iam_policy_document.deploy_role_boundary`

**Check:** IAM policy should not permit permissions-management or
resource-exposure actions without resource constraints.

**Analysis:** same underlying policy document and same root cause as
CKV_AWS_108 above — flagged separately by Checkov because it evaluates
a different category of restrictable action against the same
wildcard statement. Evaluated independently here because a boundary
permitting unconstrained data exfiltration and one permitting
unconstrained permissions management are different risks in
principle, even where, in this specific policy, both findings trace
back to the same `sns:ListTopics` statement.

**Decision:** risk-accepted, for the same structural reason as
CKV_AWS_108: the unscoped action has no resource-scoped alternative in
the AWS API, and the boundary's role is to cap permissions, not grant
them.

### CKV_AWS_110 — `data.aws_iam_policy_document.deploy_role_boundary`

**Check:** IAM policy should not permit privilege escalation.

**Analysis:** privilege escalation specifically means a role being
able to grant itself additional permissions beyond what it currently
holds. The boundary's wildcard statement is for `sns:ListTopics` (a
read-only list operation) — it does not grant any ability to create,
attach, or modify IAM policies. This project's actual privilege
escalation surface (the just-in-time `iam:PutRolePolicy` grant used by
the preflight check) is independently constrained: scoped to a single,
fixed policy name, and additionally capped by this same boundary,
which limits the maximum permissions any self-granted policy could
ever take effect with, regardless of its content.

**Decision:** risk-accepted. The flagged wildcard does not itself
enable privilege escalation; the actual escalation-capable action
elsewhere in this project is independently constrained by name and by
this same boundary.

### CKV_AWS_111 — `data.aws_iam_policy_document.deploy_role_boundary`

**Check:** IAM policy should not permit write access without
resource constraints.

**Analysis:** evaluated independently because "unconstrained write
access" is a distinct concern from the read-only `ListTopics` action
actually triggering this wildcard. No statement in this boundary
grants unconstrained write access to any resource type — every write
action elsewhere in the underlying policy (S3 `PutObject`, Lambda
`UpdateFunctionCode`, etc.) is scoped to specific resource ARN
patterns, verified directly against the policy source.

**Decision:** risk-accepted. The action triggering this finding is
read-only; no unconstrained write capability exists in this policy.

### CKV_AWS_356 — `data.aws_iam_policy_document.deploy_role_boundary`

**Check:** no IAM policy document should allow `"*"` as a resource for
actions that support resource-level restriction.

**Analysis:** this check would correctly flag a genuine oversight if a
restrictable action were wildcarded out of carelessness. In this case,
`sns:ListTopics` does not support resource-level restriction at all —
it is one of a small set of AWS APIs (mostly List/Describe operations)
with no ARN parameter to scope. The check's premise ("this action
supports restriction") does not hold for this specific action.

**Decision:** risk-accepted. Verified against AWS's own IAM action
reference that `sns:ListTopics` has no resource-level permission
option.

### CKV_AWS_50 — `aws_lambda_function.drift_monitor`

**Check:** Lambda function should have AWS X-Ray tracing enabled.

**Analysis:** X-Ray traces request latency and errors across
distributed, multi-service call chains. This function makes outbound
calls to external APIs (GitHub, HashiCorp, Anthropic) but is not part
of a multi-service AWS architecture with downstream Lambda-to-Lambda
or Lambda-to-API-Gateway chains that X-Ray is designed to visualize.
Full structured execution logging is already captured in CloudWatch
for every run.

**Decision:** risk-accepted. X-Ray's value proposition does not apply
to a single, independently scheduled function with no internal AWS
service call chain.

### CKV_AWS_117 — `aws_lambda_function.drift_monitor`

**Check:** Lambda function should be configured inside a VPC.

**Analysis:** this function requires outbound internet access to
reach the GitHub API, the HashiCorp provider registry, and the
Anthropic API — all public internet endpoints, not VPC-resident
resources. Placing the function in a VPC would require provisioning a
NAT gateway specifically to restore the public connectivity it already
has by default. There are no private AWS resources (RDS, internal
load balancers, etc.) in this architecture for VPC isolation to
protect.

**Decision:** risk-accepted. VPC placement would add cost and
complexity to restore functionality the function already has, with no
corresponding resource to isolate.

### CKV_AWS_116 — `aws_lambda_function.drift_monitor`

**Check:** Lambda function should be configured with a dead-letter
queue (DLQ) for failed asynchronous invocations.

**Analysis:** a DLQ captures invocations that fail after Lambda's
built-in async retry attempts are exhausted. This function is invoked
either synchronously (manual CLI invocation, where failure is visible
immediately to the caller) or via EventBridge on a schedule.
EventBridge itself retries failed scheduled targets according to its
own retry policy before giving up. The self-monitoring CloudWatch
alarms already built into this project (`drift-monitor-errors`,
`drift-monitor-not-invoked`) independently catch the two failure modes
a DLQ would otherwise need to surface: an execution that errors out,
and an execution that never happens at all.

**Decision:** risk-accepted. The failure-visibility gap a DLQ would
close is already closed by this project's existing alarms, via a
different mechanism.

### CKV_AWS_272 — `aws_lambda_function.drift_monitor`

**Check:** Lambda function should validate code-signing before
deployment.

**Analysis:** code signing verifies that deployed code originated
from a specific, trusted publisher — valuable when multiple parties
or pipelines can publish to a function. This function has exactly one
code path: this repository's own CI/CD pipeline, authenticated via
OIDC to a deploy role scoped to this repository alone (see
`terraform/bootstrap/main.tf`'s trust policy). There is no second
publisher, internal or external, that code signing would distinguish
from this one.

**Decision:** risk-accepted. Code signing defends against a
multi-publisher scenario that does not exist in this project's
deployment model.

### CKV2_AWS_62 — `aws_s3_bucket.deployments`

**Check:** S3 bucket should have event notifications enabled.

**Analysis:** event notifications would require wiring SNS, SQS, or
Lambda triggers off bucket activity (object creation, deletion, etc.).
Nothing in this project currently consumes such events for this
bucket — there is no monitoring or automation downstream that would
act on a "new deployment artifact uploaded" event.

**Decision:** risk-accepted. Implementing this control would add
infrastructure with no current consumer, evaluated specifically for
this bucket since it is the one whose contents change on every deploy.

### CKV2_AWS_62 — `aws_s3_bucket.lambda_state`

**Check:** same requirement as CKV2_AWS_62 on `aws_s3_bucket.deployments` above — S3 event notifications.

**Analysis:** evaluated independently from CKV2_AWS_62 on `aws_s3_bucket.deployments`
above because state-change notifications would be a meaningfully
different signal (state drift or unexpected modification) than
deployment notifications. No such consumer exists for this bucket
either — Terraform's own native S3 locking already prevents concurrent
modification, which is the risk an event-driven alert would otherwise
need to catch.

**Decision:** risk-accepted, on similar grounds to CKV2_AWS_62 on
`aws_s3_bucket.deployments` above, with the additional point that Terraform's locking mechanism
already addresses the most likely use case for such an alert.

### CKV2_AWS_62 — `aws_s3_bucket.analysis_cache`

**Check:** same requirement as CKV2_AWS_62 on `aws_s3_bucket.deployments` and `aws_s3_bucket.lambda_state` above — S3 event notifications.

**Analysis:** evaluated independently because this bucket's content
(cached AI changelog analysis) and access pattern (the Lambda
execution role, not the deploy role) differ from both other buckets.
The conclusion is the same regardless: nothing in this project
consumes bucket-activity events for any of the three S3 buckets, and
this bucket specifically has no monitoring or automation downstream
that would act on a "cache entry written" event.

**Decision:** risk-accepted, on the same no-event-consumer basis as
CKV2_AWS_62 on `aws_s3_bucket.deployments` and `aws_s3_bucket.lambda_state` above.

### CKV_AWS_18 — `aws_s3_bucket.deployments`

**Check:** S3 bucket should have access logging enabled.

**Analysis:** access logging would require provisioning a separate,
dedicated logging bucket to receive access records. This bucket's
access is already restricted by IAM policy to a single identity — the
GitHub Actions deploy role — verified directly against that role's
attached policy.

**Decision:** risk-accepted. The population capable of accessing this
bucket is a single known identity; access logging would record events
from that one known source, with limited incremental value relative to
the cost of a dedicated logging bucket.

### CKV_AWS_18 — `aws_s3_bucket.analysis_cache`

**Check:** same requirement as CKV_AWS_18 on `aws_s3_bucket.deployments` above — S3 access logging.

**Analysis:** evaluated independently because this bucket's access
pattern differs from `aws_s3_bucket.deployments` — it is read and written
by the Lambda execution role (via `lambda-infra`'s IAM policy) rather
than the deploy role. The conclusion is the same: a single known
identity, verified directly against that role's policy, makes access
logging low marginal value here too.

**Decision:** risk-accepted, on the same single-known-identity basis
as CKV_AWS_18 on `aws_s3_bucket.deployments` above,
verified against this bucket's specific IAM grant rather than assumed
from the deployments bucket finding.

### CKV_AWS_18 — `aws_s3_bucket.lambda_state`

**Check:** same requirement as CKV_AWS_18 on `aws_s3_bucket.deployments` and `aws_s3_bucket.analysis_cache` above — S3 access logging.

**Analysis:** this bucket is accessed exclusively by the GitHub
Actions deploy role during `terraform init`/`apply` against
`terraform/lambda-code/`, plus a human operator running the same
commands locally with their own AWS credentials. Both access paths
are already attributable without a separate access log — CloudTrail
(used directly during this project's own permissions debugging)
already records every API call against this bucket by identity.

**Decision:** risk-accepted. CloudTrail already provides the
attribution an access log would add, without provisioning a second
bucket.

### CKV_AWS_144 — `aws_s3_bucket.deployments`

**Check:** S3 bucket should have cross-region replication enabled.

**Analysis:** cross-region replication protects against single-region
data loss for workloads with production availability requirements.
Deployment artifacts in this bucket are regenerable on demand by
re-running the build process — they are not a unique, irreplaceable
data store.

**Decision:** risk-accepted. The data this control would protect is
fully regenerable; replication would add ongoing storage and transfer
cost against a low-impact loss scenario.

### CKV_AWS_144 — `aws_s3_bucket.analysis_cache`

**Check:** same requirement as CKV_AWS_144 on `aws_s3_bucket.deployments` above — cross-region replication.

**Analysis:** evaluated independently because this bucket's content
(cached AI analysis) has a different regeneration cost than
`aws_s3_bucket.deployments`'s build
artifacts — regenerating a cache entry requires a paid Anthropic API
call, not just a free rebuild. This makes the case for protecting it
marginally stronger than CKV_AWS_144 on `aws_s3_bucket.deployments` above, but the underlying
cache data is still non-unique: the same analysis could be regenerated
identically from the same public source changelog at any time.

**Decision:** risk-accepted, with slightly more nuance than CKV_AWS_144 on
`aws_s3_bucket.deployments` above, but
the same ultimate conclusion as CKV_AWS_144 on `aws_s3_bucket.deployments`: the data
is regenerable, just not free to regenerate.

### CKV_AWS_144 — `aws_s3_bucket.lambda_state`

**Check:** same requirement as CKV_AWS_144 on `aws_s3_bucket.deployments` and `aws_s3_bucket.analysis_cache` above — cross-region replication.

**Analysis:** Terraform state is the least replaceable data in this
project — unlike `aws_s3_bucket.deployments` and `aws_s3_bucket.analysis_cache`,
state cannot be regenerated
from source; losing it without a backup would mean Terraform no
longer knows what it manages. This finding is evaluated on
meaningfully different stakes than CKV_AWS_144 on
`aws_s3_bucket.deployments` and `aws_s3_bucket.analysis_cache` above. However, this risk is already mitigated by a different,
already-implemented control: S3 versioning is enabled on this bucket
(see `terraform/bootstrap/main.tf`), meaning state history is
recoverable within the region even without cross-region replication.

**Decision:** risk-accepted. The risk this control addresses
(unrecoverable state loss) is already mitigated by versioning;
cross-region replication would add protection against a regional
outage specifically, which is judged out of scope for a
single-maintainer project with no uptime requirement.

### CKV_AWS_21 — `aws_s3_bucket.analysis_cache`

**Check:** S3 bucket should have versioning enabled.

**Analysis:** this bucket caches AI-generated changelog summaries,
keyed by provider and version (e.g. `hashicorp-aws-6.0.0.json`).
Cached entries are deterministically regenerable from the same public
source data at any time. Versioning previous cache states provides no
recovery value, since any version can be reconstructed identically.

**Decision:** risk-accepted. Versioning protects against accidental
loss of irreplaceable data; this bucket contains no such data.

### CKV2_AWS_61 — `aws_s3_bucket.analysis_cache`

**Check:** S3 bucket should have a lifecycle configuration.

**Analysis:** this project's caching design supports an explicitly
configurable indefinite cache lifetime (`ai_changelog_cache_ttl_hours
= 0`), valid because a specific software release's changelog content
is immutable once published — there is no future point at which a
cached entry for, say, `hashicorp-aws-6.0.0` becomes stale. A
bucket-level lifecycle expiration rule would delete cache entries a
user explicitly configured to persist forever, silently triggering a
paid re-computation the user specifically chose to avoid.

**Decision:** risk-accepted. A lifecycle rule here would actively
defeat a deliberate, already-implemented feature rather than improve
this resource's safety.

### CKV2_AWS_61 — `aws_s3_bucket.lambda_state`

**Check:** same requirement as CKV2_AWS_61 on `aws_s3_bucket.analysis_cache` above — S3 lifecycle configuration.

**Analysis:** evaluated independently because the reasoning differs
entirely from CKV2_AWS_61 on `aws_s3_bucket.analysis_cache` above. This bucket holds
Terraform state history. State versions must persist indefinitely to
support recovery from a corrupted or incorrectly modified state file —
a lifecycle rule that expires old state versions would directly
undermine that recovery capability, which is the opposite outcome
from what this check is generally intended to encourage.

**Decision:** risk-accepted, for reasons specific to this resource's
role as a recovery mechanism, not because lifecycle rules are
generally undesirable.

### CKV2_AWS_34 — `aws_ssm_parameter.role_integrity_hash`

**Check:** SSM parameter should be encrypted (SecureString).

**Analysis:** this check evaluates confidentiality — whether a value
is unreadable to an unauthorized party. Confidentiality is not the
property this parameter requires. It stores a SHA256 digest of the
deploy role's IAM policy, used to detect unauthorized modification of
that policy (see `terraform/bootstrap/main.tf`). A hash does not
reveal the contents of the policy it was computed from, so reading it
discloses nothing. The property that actually matters for an
integrity check is who can *write* to it: if a party other than the
legitimate Terraform apply process could overwrite the stored hash,
they could update it to match a tampered policy and defeat the check
entirely, regardless of encryption. This was verified directly:
`ssm:PutParameter` does not appear anywhere in the deploy role's IAM
policy or its permissions boundary in `terraform/bootstrap/main.tf`.
Neither the CI/CD deploy role nor the Lambda execution role holds
write access to this parameter. The only path to modifying it is a
human operator running `terraform apply` against
`terraform/bootstrap/` directly — the same privilege level already
required to modify the IAM policy being hashed in the first place.

**Decision:** risk-accepted. Encryption would not change who can write
to this value, which is the control that actually determines whether
the integrity check can be defeated.

### CKV2_AWS_34 — `aws_ssm_parameter.simulate_policy_name`

**Check:** same requirement as CKV2_AWS_34 on `aws_ssm_parameter.role_integrity_hash` above — SecureString encryption.

**Analysis:** evaluated independently because this parameter's content
and purpose differ from CKV2_AWS_34 on `aws_ssm_parameter.role_integrity_hash`
above — it stores a
literal, non-secret policy name string
(`tfdriftmonitor-jit-simulate`), used to identify the just-in-time
permission-check policy by name. There is no confidential content to
protect, and — verified the same way as above — `ssm:PutParameter` is
absent from both the deploy role's policy and its boundary, so the
same write-access reasoning applies independently to this parameter.

**Decision:** risk-accepted, verified independently rather than
assumed identical to the `role_integrity_hash` finding, though the
underlying write-access conclusion is the same.

### CKV_AWS_338 — `aws_cloudwatch_log_group.drift_monitor`

**Check:** CloudWatch log group should retain logs for at least 365
days.

**Analysis:** this project's log retention is set to 30 days, below
the recommended threshold. Increasing retention to 365+ days was
evaluated and rejected on cost grounds: this is a personal,
self-funded project, and 12x the log storage duration is a direct,
ongoing cost increase with no corresponding audit or compliance
requirement driving the need for a full year of retained logs at this
project's current scale.

**Decision:** risk-accepted, explicitly on cost grounds rather than a
security or technical justification. Anyone deploying this project
with a genuine audit or compliance need for longer retention should
increase `log_retention_days` in `terraform/lambda-infra/variables.tf`
— see the README for instructions.

---

## Summary

| Disposition | Count |
|---|---|
| Remediated in code | 4 |
| Risk-accepted, documented above | 32 |
| **Total findings** | **36** |

`soft_fail` in `.github/workflows/dev.yml` is set to `false` following
this triage — all findings have an explicit, independently-evaluated
disposition, so the pipeline now fails on any *new*, undispositioned
finding rather than passing silently on everything.
