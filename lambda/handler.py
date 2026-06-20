"""
handler.py — Terraform Drift Monitor Lambda Entry Point
=========================================================
AWS invokes lambda_handler() when the function runs, whether triggered
by EventBridge (scheduled) or manually via the CLI.

Flow:
  1. Read config from environment variables (set by terraform/lambda/main.tf)
  2. Check Terraform provider versions across all configured repos
  3. Enrich CRITICAL (or threshold-met) findings with AI changelog analysis
  4. Report everything via SNS + CloudWatch
"""

import os
import json
import logging
from datetime import datetime, timezone

from checks.terraform_versions import check_terraform_versions, enrich_findings_with_ai_analysis
from notifier import send_report

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event: dict, context) -> dict:
    """
    Main Lambda entry point called by AWS.

    Parameters:
        event   — dict sent by the trigger. EventBridge sends a scheduler
                  event dict. Manual invocations should send {"manual": true}.
        context — LambdaContext object (runtime metadata, not used here)

    Returns:
        dict with statusCode and a JSON body summary.
    """
    is_manual = event.get("manual", False)
    run_type = "MANUAL" if is_manual else "SCHEDULED"

    logger.info(
        f"=== Terraform Drift Monitor starting [{run_type}] "
        f"at {datetime.now(timezone.utc).isoformat()} ==="
    )

    # -----------------------------------------------------------------
    # Config keys here are deliberately named to match what
    # checks/terraform_versions.py and checks/ai_changelog.py expect —
    # see those files' docstrings for the full list each one reads.
    # -----------------------------------------------------------------
    config = {
        "sns_topic_arn":                  os.environ["SNS_TOPIC_ARN"],
        "analysis_cache_bucket":          os.environ["ANALYSIS_CACHE_BUCKET"],
        "anthropic_ssm_param":            os.environ["ANTHROPIC_SSM_PARAMETER"],
        "github_token_ssm_param":         os.environ["GITHUB_TOKEN_SSM_PARAMETER"],
        "terraform_repos_ssm_param":       os.environ["TERRAFORM_REPOS_SSM_PARAMETER"],
        "ai_model":                       os.environ.get("AI_MODEL", "claude-sonnet-4-6"),
        "ai_effort":                      os.environ.get("AI_EFFORT", "medium"),
        "ai_analysis_severity_threshold": os.environ.get("AI_ANALYSIS_SEVERITY_THRESHOLD", "CRITICAL"),
        "ai_changelog_cache_ttl_hours":   int(os.environ.get("AI_CHANGELOG_CACHE_TTL_HOURS", "2160")),
        "ai_time_budget_seconds":         int(os.environ.get("AI_TIME_BUDGET_SECONDS", "180")),
    }

    logger.info(
        f"AI analysis threshold: {config['ai_analysis_severity_threshold']} | "
        f"Model: {config['ai_model']} | Effort: {config['ai_effort']}"
    )

    # -----------------------------------------------------------------
    # Step 1: Check provider versions across all configured repos.
    # Pure version-diffing — no AI calls happen in this step at all.
    # -----------------------------------------------------------------
    logger.info("Step 1: Checking Terraform provider versions...")

    try:
        findings = check_terraform_versions(config)
    except Exception as e:
        error_msg = f"Terraform version check failed unexpectedly: {e}"
        logger.error(error_msg)
        send_report(config, findings=[], error=error_msg)
        return {"statusCode": 500, "body": error_msg}

    logger.info(f"Version check complete — {len(findings)} finding(s).")

    # -----------------------------------------------------------------
    # Step 2: AI enrichment for findings meeting the severity threshold.
    # A failure here should not lose the version-diff findings already
    # gathered — log and continue with what we have.
    # -----------------------------------------------------------------
    logger.info("Step 2: AI changelog analysis for qualifying findings...")

    try:
        findings = enrich_findings_with_ai_analysis(findings, config)
    except Exception as e:
        logger.error(f"AI enrichment failed unexpectedly: {e}. Continuing with un-enriched findings.")

    # -----------------------------------------------------------------
    # Step 3: Report
    # -----------------------------------------------------------------
    logger.info(f"Step 3: Reporting {len(findings)} finding(s)...")
    send_report(config, findings=findings)

    logger.info("=== Terraform Drift Monitor complete ===")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "run_type": run_type,
            "findings": len(findings),
        }),
    }
