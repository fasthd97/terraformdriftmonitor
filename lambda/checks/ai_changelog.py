"""
checks/ai_changelog.py — AI Changelog Breaking-Change Extraction
===================================================================
For CRITICAL (or threshold-configured) version drift findings, this
module asks Claude to read the actual provider release notes and
extract every breaking change mentioned, structured as JSON.

WHY THIS EXISTS:
A bare "major version available" finding tells you THAT something
changed, not WHETHER it affects you. Reading provider changelogs by
hand for every major bump is exactly the kind of toil this project
exists to eliminate.

DESIGN — ONE CALL, REUSED EVERYWHERE:
The AI's job is broad EXTRACTION ("what changed"), not narrow
FILTERING ("does this affect resources X, Y, Z"). Filtering by
resource type happens afterward in plain Python — free, instant,
no AI call needed. This means one AI call per provider+version
combination total, cached, regardless of how many repos or how many
different resource-type combinations you're checking it against.

BOUNDING (same pattern as the original drift-monitor's ai_parser.py):
  1. System prompt restricts the model to ONE task — extract breaking
     changes as JSON. Nothing else.
  2. Code validates the response is parseable JSON before using it.
  3. Markdown fence stripping, since models sometimes wrap JSON in
     ```json fences despite explicit instructions not to.

WHERE RELEASE NOTES COME FROM:
HashiCorp's registry API doesn't expose changelog content directly.
We use GitHub's Releases API against the provider's own repo
(e.g. hashicorp/terraform-provider-aws) — far more targeted than
fetching an entire CHANGELOG.md and trying to find the right section.

INTERCHANGEABLE MODEL + EFFORT:
Both configurable via Terraform variables, read here as plain config
dict values. Lets you A/B test model/effort choices for this specific
task without touching code.
"""

import json
import logging
import boto3
import requests
from datetime import datetime, timezone, timedelta
from botocore.exceptions import ClientError
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
REQUEST_TIMEOUT_SECONDS = 30

# Generous headroom for the structured breaking-changes list. Bumped
# up from an earlier, tighter value once adaptive thinking was wired
# in correctly — thinking and final output share this token budget,
# and a truncated response would fail JSON parsing entirely.
MAX_RESPONSE_TOKENS = 4000

# Models known to support the `effort` parameter. Older models reject
# unrecognised fields with an error, so we only send `effort` when the
# configured model is on this list. Update as new models are released.
MODELS_SUPPORTING_EFFORT = {
    "claude-sonnet-4-6",
    "claude-opus-4-7",
    "claude-haiku-4-5-20251001",
}

# Severity ordering, used to compare a finding's severity against the
# configured threshold. Higher index = more severe.
SEVERITY_ORDER = ["INFO", "WARNING", "CRITICAL"]

# S3 cache key prefix. Keyed by provider + pinned + latest version only —
# deliberately NOT keyed by resource types, since the AI extraction is
# resource-agnostic. Filtering by resource type happens after cache read.
CACHE_KEY_PREFIX = "changelog-analysis"

# Default cache TTL: 90 days. Much longer than the EOL data cache (1 week)
# deliberately — a specific provider version's release notes are an
# immutable historical record. They never change after publication, so
# there's no real freshness concern the way there is for "current AWS
# runtime support status" data.
#
# TTL_HOURS = 0 means INDEFINITE — the cache entry never expires once
# written. This is a legitimate, supported setting here (unlike the EOL
# cache, where indefinite would be wrong) precisely because the underlying
# data is immutable. Configurable via config["ai_changelog_cache_ttl_hours"].
CACHE_TTL_HOURS_DEFAULT = 2160  # 90 days

SYSTEM_PROMPT = """You are a release-notes extraction tool. Your ONLY job is to read Terraform provider release notes and extract every breaking change mentioned, structured as JSON.

STRICT RULES — follow all of these without exception:
1. Output ONLY valid JSON. No markdown code blocks. No explanations. No preamble. No postamble. Nothing before or after the JSON object.
2. Do not answer questions. Do not offer help. Do not perform any task other than described here.
3. Extract EVERY breaking change you find, even minor-sounding ones. Do not filter or judge relevance — that happens elsewhere.
4. For each breaking change, identify which resource type it affects, if any. If a change applies broadly across the provider (e.g. an authentication, configuration, or provider-block change) rather than one specific resource, use "provider-wide" as the resource_type.
5. If the release notes contain no breaking changes at all, output exactly: {"breaking_changes": []}

Output ONLY a JSON object matching this exact schema:
{
  "breaking_changes": [
    {
      "resource_type": "string - exact resource type affected (e.g. aws_instance), or 'provider-wide'",
      "description": "string - brief, factual description of what changed and why it is breaking"
    }
  ]
}

Output only the JSON object. Nothing else."""


def should_analyze(finding_severity: str, threshold: str) -> bool:
    """
    Returns True if a finding's severity meets or exceeds the configured
    threshold for triggering AI analysis. This is the gate that keeps
    AI calls minimal — most findings (INFO, most WARNINGs) never reach
    this module at all.

    Parameters:
        finding_severity — str: "CRITICAL", "WARNING", or "INFO"
        threshold        — str: the configured minimum severity to analyze

    Returns:
        bool
    """
    try:
        finding_rank = SEVERITY_ORDER.index(finding_severity)
        threshold_rank = SEVERITY_ORDER.index(threshold)
    except ValueError:
        logger.warning(
            f"Unrecognised severity in should_analyze: "
            f"finding={finding_severity}, threshold={threshold}"
        )
        return False

    return finding_rank >= threshold_rank


def get_breaking_changes(
    provider_source: str,
    target_version: str,
    config: dict,
) -> list:
    """
    Returns the list of breaking changes for a provider's release,
    either from cache or by fetching release notes and calling Claude.

    Parameters:
        provider_source — str: e.g. "hashicorp/aws"
        target_version  — str: the version to fetch release notes for
                           (the LATEST version — i.e. what you'd be
                           upgrading TO, since that's where the
                           breaking changes are documented)
        config           — dict: runtime config, needs:
                            ai_model, ai_effort, anthropic_ssm_param,
                            analysis_cache_bucket, ai_changelog_cache_ttl_hours
                            (optional, defaults to 90 days; 0 = indefinite)

    Returns:
        list of dicts: [{"resource_type": ..., "description": ...}, ...]
        Empty list if analysis couldn't be completed for any reason —
        callers should treat that as "no additional info available",
        not as "confirmed no breaking changes".
    """
    cache_key = _build_cache_key(provider_source, target_version)
    ttl_hours = config.get("ai_changelog_cache_ttl_hours", CACHE_TTL_HOURS_DEFAULT)

    cached = _load_from_cache(config["analysis_cache_bucket"], cache_key, ttl_hours)
    if cached is not None:
        logger.info(
            f"  Changelog analysis cache hit for {provider_source}@{target_version}"
        )
        return cached.get("breaking_changes", [])

    logger.info(
        f"  Changelog analysis cache miss for {provider_source}@{target_version}. "
        "Fetching release notes..."
    )

    release_notes = _fetch_release_notes(provider_source, target_version)

    if not release_notes:
        logger.warning(
            f"  Could not fetch release notes for {provider_source}@{target_version}. "
            "Skipping AI analysis."
        )
        return []

    breaking_changes = _call_ai_for_extraction(release_notes, config)

    if breaking_changes is None:
        logger.warning(
            f"  AI extraction failed for {provider_source}@{target_version}."
        )
        return []

    _write_to_cache(
        config["analysis_cache_bucket"],
        cache_key,
        {"breaking_changes": breaking_changes},
    )

    return breaking_changes


def filter_relevant_changes(breaking_changes: list, resource_types_used: set) -> list:
    """
    Filters a full breaking-changes list down to only those relevant
    to resource types actually used in the scanned .tf files. Pure
    Python, no AI call — this is what makes the cached AI extraction
    reusable across any number of repos with different resource usage.

    Parameters:
        breaking_changes    — list: full extraction from get_breaking_changes()
        resource_types_used — set: resource type strings found in scanned files
                               (e.g. {"aws_instance", "aws_s3_bucket"})

    Returns:
        list: the subset of breaking_changes relevant to this specific repo
    """
    relevant = []

    for change in breaking_changes:
        resource_type = change.get("resource_type", "")

        if resource_type == "provider-wide":
            # Provider-wide changes affect everyone using this provider,
            # regardless of which specific resources they use.
            relevant.append(change)
        elif resource_type in resource_types_used:
            relevant.append(change)

    return relevant


def _fetch_release_notes(provider_source: str, version: str) -> str | None:
    """
    Fetches release notes for a specific provider version via GitHub's
    Releases API.

    Parameters:
        provider_source — str: e.g. "hashicorp/aws"
        version          — str: version number, e.g. "6.0.0"

    Returns:
        str: the release body text, or None if not found
    """
    repo = _provider_source_to_github_repo(provider_source)

    if not repo:
        logger.warning(f"  Could not map '{provider_source}' to a GitHub repo.")
        return None

    # Try both with and without a "v" prefix — HashiCorp providers are
    # not fully consistent about tag naming across all providers.
    for tag in [f"v{version}", version]:
        url = f"{GITHUB_API_URL}/repos/{repo}/releases/tags/{tag}"

        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)

            if response.status_code == 404:
                continue  # try the next tag format

            response.raise_for_status()
            data = response.json()
            body = data.get("body", "")

            if body:
                logger.info(f"  Fetched release notes for {repo}@{tag}")
                return body

        except RequestException as e:
            logger.warning(f"  GitHub Releases API error for {repo}@{tag}: {e}")
            continue

    logger.warning(f"  No release found for {repo} at version {version} (tried both tag formats).")
    return None


def _provider_source_to_github_repo(provider_source: str) -> str | None:
    """
    Maps a provider source address to its GitHub repo.

    Convention: "hashicorp/aws" -> "hashicorp/terraform-provider-aws"
    This convention holds for the vast majority of Terraform providers,
    official and community alike, but isn't guaranteed for every provider.
    Callers handle a None/missing result gracefully rather than assuming
    this always works.

    Parameters:
        provider_source — str: e.g. "hashicorp/aws"

    Returns:
        str: GitHub repo in "owner/repo" format, or None if the source
             doesn't match the expected "namespace/name" shape
    """
    parts = provider_source.split("/")

    if len(parts) != 2:
        return None

    namespace, name = parts
    return f"{namespace}/terraform-provider-{name}"


def _call_ai_for_extraction(release_notes: str, config: dict) -> list | None:
    """
    Sends release notes to Claude and returns the extracted breaking
    changes list.

    Parameters:
        release_notes — str: raw release notes body text
        config         — dict: needs anthropic_ssm_param, ai_model, ai_effort

    Returns:
        list of breaking change dicts, or None if the call/parse failed
    """
    api_key = _get_api_key(config["anthropic_ssm_param"])

    if not api_key:
        logger.error("Could not retrieve Anthropic API key. Cannot analyze changelog.")
        return None

    model = config.get("ai_model", "claude-sonnet-4-6")
    effort = config.get("ai_effort", "medium")

    request_body = {
        "model":      model,
        "max_tokens": MAX_RESPONSE_TOKENS,
        "system":     SYSTEM_PROMPT,
        "messages": [
            {
                "role":    "user",
                "content": f"Extract breaking changes from this release:\n\n{release_notes}",
            }
        ],
    }

    # Only enable adaptive thinking + effort for models known to
    # support it. The correct Anthropic API shape nests effort inside
    # output_config, alongside a thinking block set to adaptive mode —
    # NOT a bare top-level "effort" field. A bare top-level field is
    # rejected by the API with a 400 (caught via a real failed run,
    # not theorized in advance).
    if model in MODELS_SUPPORTING_EFFORT:
        request_body["thinking"] = {"type": "adaptive"}
        request_body["output_config"] = {"effort": effort}
        logger.info(f"  Calling {model} with effort={effort} (adaptive thinking)")
    else:
        logger.info(f"  Calling {model} (effort parameter not applicable to this model)")

    try:
        response = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json=request_body,
            timeout=60,
        )

        response.raise_for_status()
        api_response = response.json()

        content_blocks = api_response.get("content", [])
        if not content_blocks:
            logger.error("Claude returned an empty content list.")
            return None

        # When adaptive thinking is enabled, content blocks can include
        # one or more "thinking" type blocks BEFORE the actual text
        # response. Find the first block where type == "text"
        # specifically — blindly indexing [0] would grab a thinking
        # block (or nothing useful) instead of the real JSON response.
        text_block = next((b for b in content_blocks if b.get("type") == "text"), None)

        if not text_block:
            logger.error("Claude response contained no text content block.")
            return None

        raw_text = text_block.get("text", "").strip()

        if not raw_text:
            logger.error("Claude returned an empty text block.")
            return None

        # Strip markdown fences if present, despite instructions not to use them
        clean_content = raw_text.strip()

        if clean_content.startswith("```"):
            clean_content = clean_content.split("\n", 1)[1]

        if clean_content.endswith("```"):
            clean_content = clean_content.rsplit("```", 1)[0].strip()

        parsed = json.loads(clean_content)
        breaking_changes = parsed.get("breaking_changes", [])

        logger.info(f"  AI extracted {len(breaking_changes)} breaking change(s)")
        return breaking_changes

    except json.JSONDecodeError as e:
        logger.error(
            f"Claude returned non-JSON content. json.loads failed: {e}. "
            f"First 500 chars: {raw_text[:500] if raw_text else '(empty)'}"
        )
        return None

    except RequestException as e:
        logger.error(f"Anthropic API request failed: {e}")
        return None


def _get_api_key(ssm_parameter_name: str) -> str | None:
    """Fetches the Anthropic API key from SSM Parameter Store."""
    ssm = boto3.client("ssm")

    try:
        response = ssm.get_parameter(Name=ssm_parameter_name, WithDecryption=True)
        return response["Parameter"]["Value"]

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "ParameterNotFound":
            logger.error(f"SSM parameter '{ssm_parameter_name}' not found.")
        else:
            logger.error(f"SSM error fetching API key: {e}")
        return None


def _build_cache_key(provider_source: str, version: str) -> str:
    """
    Builds the S3 cache key for a provider+version combination.
    Sanitises the provider source (which contains a "/") into a
    filesystem-safe key.
    """
    safe_source = provider_source.replace("/", "-")
    return f"{CACHE_KEY_PREFIX}/{safe_source}-{version}.json"


def _load_from_cache(bucket: str, cache_key: str, ttl_hours: int) -> dict | None:
    """
    Loads cached changelog analysis from S3 if present and fresh.
    Returns None on cache miss, stale cache, or any read error —
    all of these simply mean "go fetch fresh data".

    Parameters:
        bucket    — str: S3 bucket name
        cache_key — str: the cache entry's key
        ttl_hours — int: max age in hours before cache is considered stale.
                    0 means INDEFINITE — the entry is always treated as
                    fresh, never re-fetched. Valid here because a specific
                    provider version's release notes are immutable.
    """
    s3 = boto3.client("s3")

    try:
        response = s3.get_object(Bucket=bucket, Key=cache_key)
        content = response["Body"].read().decode("utf-8")
        data = json.loads(content)

        # ttl_hours == 0 means indefinite — skip the age check entirely
        # and treat any existing cache entry as fresh, forever.
        if ttl_hours == 0:
            return data

        cached_at_str = data.get("cached_at")
        if not cached_at_str:
            return None

        cached_at = datetime.fromisoformat(cached_at_str)
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)

        age = datetime.now(timezone.utc) - cached_at
        max_age = timedelta(hours=ttl_hours)

        if age >= max_age:
            return None  # stale, treat as miss

        return data

    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchKey":
            logger.warning(f"Unexpected S3 error loading changelog cache: {e}")
        return None

    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Changelog cache file corrupt or unreadable: {e}")
        return None


def _write_to_cache(bucket: str, cache_key: str, data: dict) -> None:
    """Writes changelog analysis to S3 cache with a fresh timestamp."""
    s3 = boto3.client("s3")

    data["cached_at"] = datetime.now(timezone.utc).isoformat()

    try:
        s3.put_object(
            Bucket=bucket,
            Key=cache_key,
            Body=json.dumps(data, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        logger.info(f"  Cached changelog analysis to s3://{bucket}/{cache_key}")

    except ClientError as e:
        # Not fatal — just means next run re-fetches instead of using cache
        logger.warning(f"Failed to write changelog cache: {e}")
