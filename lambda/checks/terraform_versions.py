"""
checks/terraform_versions.py — Terraform Provider Version Check
================================================================
Checks whether Terraform provider versions pinned in your .tf files
are out of date compared to what the HashiCorp registry has available.

How it works:
-------------
1. Reads repo config from SSM Parameter Store (path comes from config,
   set via the TERRAFORM_REPOS_SSM_PARAMETER Lambda env var — NOT
   hardcoded, since this project's SSM paths live under /tfdriftmonitor/,
   not the original driftmonitor project's /drift-monitor/ paths)
2. For each repo, fetches .tf files via the git provider (GitHub)
3. Parses provider version constraints AND resource types used from
   those files
4. Calls the HashiCorp registry API for the latest stable version
5. Diffs pinned vs latest and produces findings
6. For findings meeting the AI severity threshold, calls AI changelog
   analysis (checks/ai_changelog.py) and attaches relevant breaking
   changes — gated, cached, minimal calls by design

Severity logic:
---------------
CRITICAL — major version available (5.x → 6.x)
WARNING  — minor versions behind, OR loose constraint with no upper bound
INFO     — patch versions behind only
"""

import json
import logging
import time
import boto3
from botocore.exceptions import ClientError
from packaging.version import Version, InvalidVersion

from terraform_parser import parse_providers, parse_resource_types, has_loose_constraint
from registry_clients.hashicorp import HashiCorpRegistryClient
from git_providers.github import GitHubProvider
from checks import ai_changelog

logger = logging.getLogger(__name__)


def check_terraform_versions(config: dict) -> list:
    """
    Main entry point. Called from handler.py.

    Parameters:
        config — dict: runtime config from handler.py. Needs
                 "terraform_repos_ssm_param" (the SSM path to read repo
                 config from) and "github_token_ssm_param" (default
                 fallback token for repos that don't specify their own).

    Returns:
        list of Finding dicts (empty if no issues found)
    """
    findings = []

    repos = _load_repo_config(config["terraform_repos_ssm_param"])

    if repos is None:
        logger.warning(
            "No Terraform repo config found in SSM. "
            f"Set up '{config['terraform_repos_ssm_param']}' to enable this check. "
            "See terraform-repos-example.md for configuration examples."
        )
        return findings

    if not repos:
        logger.info("Terraform repo config is empty. Nothing to check.")
        return findings

    logger.info(f"Checking Terraform provider versions across {len(repos)} repo(s)...")

    registry_client = HashiCorpRegistryClient()
    github_provider = GitHubProvider()

    for repo_config in repos:
        repo_findings = _check_repo(repo_config, github_provider, registry_client, config)
        findings.extend(repo_findings)

    return findings


def enrich_findings_with_ai_analysis(findings: list, config: dict) -> list:
    """
    Second pass over findings: for any finding whose severity meets the
    configured AI analysis threshold, fetches (or reuses cached) AI
    extraction of breaking changes for that provider+version, then
    filters down to changes relevant to that finding's own repo's
    resource types.

    Deliberately a SEPARATE pass from check_terraform_versions() — keeps
    the core version-diffing logic AI-independent (faster, deterministic,
    easier to test) while isolating the AI-dependent step here.

    Because get_breaking_changes() is cached by provider+version (not by
    repo), if multiple findings across multiple repos in the SAME run
    hit the same provider+version combination, only ONE AI call happens
    for all of them — the cache serves every subsequent lookup within
    the same run, not just across separate scheduled runs.

    Parameters:
        findings — list: findings from check_terraform_versions(), each
                   with "resource_types_used" attached (a list, since
                   sets aren't JSON-serializable — converted back to a
                   set here before filtering)
        config   — dict: needs ai_analysis_severity_threshold plus
                   everything ai_changelog.get_breaking_changes() needs

    Returns:
        the same findings list, with "relevant_breaking_changes" added
        to any finding that met the threshold and had data available
    """
    threshold = config.get("ai_analysis_severity_threshold", "CRITICAL")

    # Safety budget: stop attempting NEW AI analyses once this much
    # wall-clock time has been spent on them in this run. Without this,
    # a flood of simultaneous cache misses (e.g. several providers all
    # crossing major version boundaries for the first time in the same
    # run) could consume the entire Lambda timeout — and since this
    # enrichment loop runs BEFORE Step 3 (reporting) in handler.py, a
    # timeout here would mean the ENTIRE run's report is lost, including
    # the already-correct version-diff findings from Step 1, not just
    # the AI enrichment. Cache hits don't count against this budget —
    # only genuine new API calls do, since hits are already fast.
    time_budget_seconds = config.get("ai_time_budget_seconds", 180)
    elapsed_seconds = 0.0
    skipped_count = 0

    for finding in findings:
        severity = finding.get("severity", "INFO")

        if not ai_changelog.should_analyze(severity, threshold):
            continue

        if elapsed_seconds >= time_budget_seconds:
            skipped_count += 1
            continue

        source = finding.get("source")
        pinned_str = finding.get("pinned")
        latest_str = finding.get("latest")

        if not source or not latest_str:
            # WARNING/INFO findings on loose constraints don't always
            # have a "latest" — nothing to analyze in that case.
            continue

        # For CRITICAL (major version) findings, the actual breaking
        # changes were introduced at the FIRST release of the new
        # major version line — not necessarily restated in whatever
        # the absolute latest patch/minor release happens to be. E.g.
        # if pinned is 5.0 and latest is 6.51.0, the breaking changes
        # relevant to THIS upgrade live in 6.0.0's release notes, not
        # 6.51.0's (which just describes incremental fixes added since).
        # Fetching "latest" instead of the major boundary version was
        # a real bug caught via a live run that returned a believable
        # but wrong "0 breaking changes" result.
        target_version = latest_str

        if severity == "CRITICAL" and pinned_str:
            try:
                pinned_major = Version(pinned_str).major
                target_version = f"{pinned_major + 1}.0.0"
            except InvalidVersion:
                pass  # fall back to latest_str if pinned can't be parsed

        logger.info(f"  Finding meets AI threshold — analyzing {source}@{target_version}")

        call_start = time.time()
        breaking_changes = ai_changelog.get_breaking_changes(source, target_version, config)
        elapsed_seconds += time.time() - call_start

        if not breaking_changes:
            continue

        resource_types_used = set(finding.get("resource_types_used", []))
        relevant = ai_changelog.filter_relevant_changes(breaking_changes, resource_types_used)

        if relevant:
            finding["relevant_breaking_changes"] = relevant
            logger.info(f"  {len(relevant)} relevant breaking change(s) attached to finding")

    if skipped_count:
        logger.warning(
            f"  AI time budget ({time_budget_seconds}s) reached — "
            f"{skipped_count} qualifying finding(s) skipped this run. "
            "Will be attempted again on the next scheduled run."
        )

    return findings


def _check_repo(
    repo_config: dict,
    github_provider: GitHubProvider,
    registry_client: HashiCorpRegistryClient,
    config: dict,
) -> list:
    """
    Runs the version check for a single repo.

    Parameters:
        repo_config     — dict: one repo entry from the SSM config
        github_provider — GitHubProvider: for fetching .tf files
        registry_client — HashiCorpRegistryClient: for fetching latest versions
        config          — dict: runtime config, needed for the default
                          GitHub token fallback

    Returns:
        list of Finding dicts
    """
    findings = []
    repo_name = repo_config.get("name", "unknown")
    repo = repo_config.get("repo")
    branch = repo_config.get("branch", "main")
    paths = repo_config.get("paths", ["/"])
    private = repo_config.get("private", False)

    # Per-repo token_ssm_param takes priority; fall back to the single
    # bootstrap-created default token if the repo entry doesn't specify
    # its own. This is what makes the simple/common case (one GitHub
    # account, your own repos) work without needing a custom SSM param
    # written out for every single repo entry.
    token_ssm_param = repo_config.get("token_ssm_param") or config.get("github_token_ssm_param")

    logger.info(f"Checking repo: {repo_name} ({repo}, branch: {branch})")

    tf_files = github_provider.get_tf_files(
        repo=repo,
        branch=branch,
        paths=paths,
        private=private,
        token_ssm_param=token_ssm_param,
    )

    if not tf_files:
        logger.info(f"  No .tf files found in {repo_name}. Skipping.")
        return findings

    # Parse providers AND resource types from every file. Resource
    # types are collected into one set for the whole repo — used later
    # to filter AI-extracted breaking changes down to what's relevant.
    all_providers = []
    resource_types_used = set()

    for tf_file in tf_files:
        providers = parse_providers(tf_file["content"], filename=tf_file["filename"])
        all_providers.extend(providers)

        resource_types_used |= parse_resource_types(tf_file["content"], filename=tf_file["filename"])

    if not all_providers:
        logger.info(f"  No required_providers blocks found in {repo_name}.")
        return findings

    logger.info(
        f"  Found {len(all_providers)} provider(s) and "
        f"{len(resource_types_used)} resource type(s) in {repo_name}."
    )

    for provider in all_providers:
        provider_findings = _evaluate_provider(provider, repo_name, registry_client)

        # Attach resource_types_used to every finding from this repo —
        # converted to a list since sets aren't JSON-serializable, and
        # this travels with the finding through notifier.py's reporting.
        for finding in provider_findings:
            finding["resource_types_used"] = list(resource_types_used)

        findings.extend(provider_findings)

    return findings


def _evaluate_provider(
    provider: dict,
    repo_name: str,
    registry_client: HashiCorpRegistryClient,
) -> list:
    """
    Evaluates a single provider against the registry and returns findings.

    Returns:
        list of Finding dicts (may be empty if provider is up to date)
    """
    findings = []
    name = provider["name"]
    source = provider["source"]
    namespace = provider["namespace"]
    constraint = provider["constraint"]
    filename = provider["file"]

    # Check 1: loose constraint (no upper bound) — flagged regardless
    # of whether newer versions exist.
    if has_loose_constraint(constraint):
        findings.append({
            "severity":   "WARNING",
            "repo_name":  repo_name,
            "provider":   name,
            "source":     source,
            "file":       filename,
            "constraint": constraint or "none",
            "issue":      f"Provider '{name}' has no upper bound version constraint",
            "detail": (
                f"Constraint '{constraint or 'none'}' has no upper bound. "
                "A future major version could be pulled automatically on terraform init, "
                "potentially introducing breaking changes."
            ),
            "action": (
                f"Change to a pessimistic constraint like '~> {_extract_major(constraint)}.0' "
                "to prevent automatic major version upgrades."
            ),
        })

    latest_str = registry_client.get_latest_version(namespace, name)

    if not latest_str:
        logger.warning(f"  Could not get latest version for {source}. Skipping diff.")
        return findings

    pinned_version = _extract_pinned_version(constraint)

    if not pinned_version:
        return findings  # already flagged as loose constraint above

    try:
        pinned = Version(pinned_version)
        latest = Version(latest_str)
    except InvalidVersion as e:
        logger.warning(f"  Could not parse version for {source}: {e}")
        return findings

    if latest <= pinned:
        logger.info(f"  {name}: pinned {pinned_version}, latest {latest_str} — OK")
        return findings

    severity = (
        "CRITICAL" if latest.major > pinned.major else
        "WARNING" if latest.minor > pinned.minor else
        "INFO"
    )
    logger.info(f"  {name}: pinned {pinned_version}, latest {latest_str} — {severity}")

    if severity == "CRITICAL":
        findings.append({
            "severity":   "CRITICAL",
            "repo_name":  repo_name,
            "provider":   name,
            "source":     source,
            "file":       filename,
            "constraint": constraint,
            "pinned":     pinned_version,
            "latest":     latest_str,
            "issue": (
                f"Provider '{name}' is {latest.major - pinned.major} major version(s) behind "
                f"({pinned_version} → {latest_str})"
            ),
            "detail": (
                f"Current constraint: '{constraint}'. Latest stable: {latest_str}. "
                "Major version bumps contain breaking changes. "
                "Your next terraform init may fail or behave unexpectedly."
            ),
            "action": (
                f"Review the {latest.major}.0 changelog, test in dev, "
                f"then update your constraint to '~> {latest.major}.0'."
            ),
        })

    elif severity == "WARNING":
        findings.append({
            "severity":   "WARNING",
            "repo_name":  repo_name,
            "provider":   name,
            "source":     source,
            "file":       filename,
            "constraint": constraint,
            "pinned":     pinned_version,
            "latest":     latest_str,
            "issue": (
                f"Provider '{name}' is {latest.minor - pinned.minor} minor version(s) behind "
                f"({pinned_version} → {latest_str})"
            ),
            "detail": (
                f"Current constraint: '{constraint}'. Latest stable: {latest_str}. "
                "Minor versions add new features and fixes."
            ),
            "action": f"Consider updating your constraint to '~> {latest.major}.{latest.minor}'.",
        })

    else:  # INFO
        findings.append({
            "severity":   "INFO",
            "repo_name":  repo_name,
            "provider":   name,
            "source":     source,
            "file":       filename,
            "constraint": constraint,
            "pinned":     pinned_version,
            "latest":     latest_str,
            "issue": f"Provider '{name}' is behind on patch versions ({pinned_version} → {latest_str})",
            "detail": (
                f"Current constraint: '{constraint}'. Latest stable: {latest_str}. "
                "Patch versions contain bug fixes only."
            ),
            "action": "Low priority. Update when convenient.",
        })

    return findings


def _load_repo_config(ssm_parameter_name: str) -> list | None:
    """
    Loads and parses the repo config JSON from SSM Parameter Store.

    Parameters:
        ssm_parameter_name — str: the SSM path to read from (comes from
                              config, not hardcoded — this is the fix
                              for the bug where this used to be hardcoded
                              to the WRONG project's SSM path)

    Returns:
        list of repo dicts, or None if the parameter doesn't exist
    """
    ssm = boto3.client("ssm")

    try:
        response = ssm.get_parameter(Name=ssm_parameter_name, WithDecryption=True)
        raw = response["Parameter"]["Value"]
        config = json.loads(raw)
        repos = config.get("repos", [])
        logger.info(f"Loaded {len(repos)} repo(s) from SSM config.")
        return repos

    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return None
        logger.error(f"SSM error loading repo config: {e}")
        return None

    except json.JSONDecodeError as e:
        logger.error(f"Repo config in SSM is not valid JSON: {e}")
        return None


def _extract_pinned_version(constraint: str | None) -> str | None:
    """
    Extracts a comparable version number from a constraint string.
    "~> 5.0" → "5.0", ">= 5.0" → "5.0", "5.0.0" → "5.0.0", None → None
    """
    if not constraint:
        return None

    for operator in ["~>", ">=", "<=", "!=", "=", ">", "<"]:
        constraint = constraint.replace(operator, "")

    version_str = constraint.strip()

    try:
        Version(version_str)
        return version_str
    except InvalidVersion:
        return None


def _extract_major(constraint: str | None) -> str:
    """Extracts just the major version number from a constraint, for suggested fixes."""
    version_str = _extract_pinned_version(constraint)
    if not version_str:
        return "X"
    try:
        return str(Version(version_str).major)
    except InvalidVersion:
        return "X"
