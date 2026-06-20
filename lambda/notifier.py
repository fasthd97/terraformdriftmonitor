"""
notifier.py — Alert Notifier
=============================
Sends drift monitoring findings to SNS (email) and CloudWatch logs.

Same design as the original driftmonitor project's notifier.py:
  - CloudWatch always gets the full report, every run
  - SNS only fires when there are findings or errors (no clean-run noise)
  - Plain text email body (renders correctly everywhere)

Adapted finding shape for THIS project: findings use repo_name/provider/
source/constraint/pinned/latest (not stack_name/function_name/runtime
from the original CloudFormation-focused project), plus an optional
relevant_breaking_changes list from AI changelog analysis.
"""

import logging
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def send_report(config: dict, findings: list, error: str = None) -> None:
    """
    Sends the drift monitoring report to CloudWatch and optionally SNS.

    Parameters:
        config   — dict: runtime config (needs "sns_topic_arn")
        findings — list: Finding dicts from check_terraform_versions()
                   (optionally enriched with relevant_breaking_changes)
        error    — str: optional error message if the run had a problem
    """
    report = _build_report(findings, error)

    _log_report(report, findings)

    if findings or error:
        _send_sns(config["sns_topic_arn"], report, findings)
    else:
        logger.info(
            "Clean run — no findings, no errors. "
            "Skipping SNS (check CloudWatch logs for run confirmation)."
        )


def _build_report(findings: list, error: str | None) -> dict:
    critical = sum(1 for f in findings if f.get("severity") == "CRITICAL")
    warnings = sum(1 for f in findings if f.get("severity") == "WARNING")
    info     = sum(1 for f in findings if f.get("severity") == "INFO")

    return {
        "run_timestamp":  datetime.now(timezone.utc).isoformat(),
        "total_findings": len(findings),
        "critical_count": critical,
        "warning_count":  warnings,
        "info_count":     info,
        "error":          error,
        "findings":       findings,
    }


def _log_report(report: dict, findings: list) -> None:
    logger.info("=== TERRAFORM DRIFT MONITOR REPORT ===")
    logger.info(f"Run time      : {report['run_timestamp']}")
    logger.info(
        f"Findings      : {report['total_findings']} "
        f"(CRITICAL: {report['critical_count']}, "
        f"WARNING: {report['warning_count']}, "
        f"INFO: {report['info_count']})"
    )

    if report.get("error"):
        logger.error(f"Run error: {report['error']}")

    for i, finding in enumerate(findings, 1):
        severity = finding.get("severity", "UNKNOWN")
        log_fn = logger.error if severity == "CRITICAL" else (
            logger.warning if severity == "WARNING" else logger.info
        )

        log_fn(
            f"[{severity}] {i}/{len(findings)} — "
            f"{finding.get('repo_name', 'unknown')} / {finding.get('provider', 'unknown')} — "
            f"{finding.get('issue', '')}"
        )
        log_fn(f"  Detail: {finding.get('detail', '')}")
        log_fn(f"  Action: {finding.get('action', '')}")

        relevant = finding.get("relevant_breaking_changes")
        if relevant:
            log_fn(f"  AI-flagged breaking changes ({len(relevant)}):")
            for change in relevant:
                log_fn(f"    - [{change.get('resource_type')}] {change.get('description')}")

    logger.info("=== END REPORT ===")


def _send_sns(topic_arn: str, report: dict, findings: list) -> None:
    sns = boto3.client("sns")

    subject = _build_subject(report)
    body = _build_email_body(report, findings)

    try:
        sns.publish(TopicArn=topic_arn, Subject=subject[:100], Message=body)
        logger.info(f"SNS notification sent. Subject: '{subject}'")

    except ClientError as e:
        logger.error(
            f"Failed to send SNS notification: {e}. "
            "Findings are still logged to CloudWatch."
        )


def _build_subject(report: dict) -> str:
    if report.get("error"):
        return "[TF DRIFT MONITOR] ERROR — check CloudWatch logs"
    elif report["critical_count"] > 0:
        return f"[TF DRIFT MONITOR] CRITICAL — {report['critical_count']} critical finding(s)"
    elif report["warning_count"] > 0:
        return f"[TF DRIFT MONITOR] WARNING — {report['warning_count']} warning(s)"
    elif report["info_count"] > 0:
        return f"[TF DRIFT MONITOR] INFO — {report['info_count']} informational finding(s)"
    else:
        return "[TF DRIFT MONITOR] Report"


def _build_email_body(report: dict, findings: list) -> str:
    sep  = "=" * 60
    thin = "-" * 40

    lines = [
        sep,
        "TERRAFORM DRIFT MONITOR REPORT",
        sep,
        f"Run time      : {report['run_timestamp']}",
        f"Total findings: {report['total_findings']}",
        f"  CRITICAL    : {report['critical_count']}",
        f"  WARNING     : {report['warning_count']}",
        f"  INFO        : {report['info_count']}",
        "",
    ]

    if report.get("error"):
        lines += ["ERROR", thin, report["error"], ""]

    if not findings:
        lines.append("No issues found in this run.")
    else:
        lines += ["FINDINGS", thin]

        for i, f in enumerate(findings, 1):
            lines += [
                "",
                f"[{f.get('severity')}] Finding {i} of {len(findings)}",
                f"Repo     : {f.get('repo_name')}",
                f"Provider : {f.get('provider')} ({f.get('source')})",
                f"File     : {f.get('file')}",
                f"Pinned   : {f.get('constraint')}",
            ]

            if f.get("latest"):
                lines.append(f"Latest   : {f.get('latest')}")

            lines += [
                f"Issue    : {f.get('issue')}",
                f"Detail   : {f.get('detail')}",
                f"Action   : {f.get('action')}",
            ]

            relevant = f.get("relevant_breaking_changes")
            if relevant:
                lines.append(f"AI-flagged breaking changes relevant to this repo:")
                for change in relevant:
                    lines.append(f"  - [{change.get('resource_type')}] {change.get('description')}")

    lines += [
        "",
        sep,
        "Sent by: terraformdriftmonitor",
        "Full run logs: CloudWatch → /aws/lambda/tfdriftmonitor",
        sep,
    ]

    return "\n".join(lines)
