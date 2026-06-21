"""
tests/test_ai_changelog.py
=============================
Unit tests for checks/ai_changelog.py's pure logic functions —
should_analyze() and filter_relevant_changes(). Neither makes a
network call or needs credentials; both are plain data transformations.

Run with: pytest tests/test_ai_changelog.py -v
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

from checks.ai_changelog import should_analyze, filter_relevant_changes


class TestShouldAnalyze:
    @pytest.mark.parametrize("severity,threshold,expected", [
        ("CRITICAL", "CRITICAL", True),   # meets threshold exactly
        ("WARNING", "CRITICAL", False),    # below threshold
        ("INFO", "CRITICAL", False),       # well below threshold
        ("CRITICAL", "WARNING", True),     # exceeds lower threshold
        ("WARNING", "WARNING", True),      # meets lower threshold exactly
        ("INFO", "WARNING", False),        # below lower threshold
        ("INFO", "INFO", True),            # meets lowest threshold
        ("CRITICAL", "INFO", True),        # exceeds lowest threshold
    ])
    def test_threshold_comparison(self, severity, threshold, expected):
        assert should_analyze(severity, threshold) == expected

    def test_unrecognised_severity_returns_false_not_exception(self):
        # An unexpected severity string should degrade safely rather
        # than raise and abort the enrichment pass for every finding.
        assert should_analyze("UNKNOWN", "CRITICAL") is False

    def test_unrecognised_threshold_returns_false_not_exception(self):
        assert should_analyze("CRITICAL", "UNKNOWN") is False


class TestFilterRelevantChanges:
    def test_provider_wide_change_always_included(self):
        changes = [
            {"resource_type": "provider-wide", "description": "Auth change"},
        ]
        # Even with a resource set that doesn't obviously relate,
        # provider-wide changes affect everyone using the provider.
        result = filter_relevant_changes(changes, {"aws_s3_bucket"})

        assert len(result) == 1
        assert result[0]["resource_type"] == "provider-wide"

    def test_matching_resource_type_included(self):
        changes = [
            {"resource_type": "aws_ami", "description": "Filter behavior changed"},
        ]
        result = filter_relevant_changes(changes, {"aws_ami", "aws_s3_bucket"})

        assert len(result) == 1
        assert result[0]["resource_type"] == "aws_ami"

    def test_non_matching_resource_type_excluded(self):
        changes = [
            {"resource_type": "aws_ami", "description": "Filter behavior changed"},
        ]
        # Repo doesn't use aws_ami at all - should be filtered out.
        result = filter_relevant_changes(changes, {"aws_s3_bucket", "aws_lambda_function"})

        assert result == []

    def test_mixed_list_filters_correctly(self):
        changes = [
            {"resource_type": "aws_ami", "description": "Change A"},
            {"resource_type": "aws_ecs_task_definition", "description": "Change B"},
            {"resource_type": "provider-wide", "description": "Change C"},
            {"resource_type": "aws_rds_instance", "description": "Change D"},
        ]
        # Repo only uses aws_ami and aws_s3_bucket - expect Change A
        # (matches) and Change C (always included) but not B or D.
        result = filter_relevant_changes(changes, {"aws_ami", "aws_s3_bucket"})

        descriptions = {c["description"] for c in result}
        assert descriptions == {"Change A", "Change C"}

    def test_empty_changes_list_returns_empty(self):
        result = filter_relevant_changes([], {"aws_s3_bucket"})
        assert result == []

    def test_empty_resource_set_only_keeps_provider_wide(self):
        changes = [
            {"resource_type": "aws_ami", "description": "Change A"},
            {"resource_type": "provider-wide", "description": "Change B"},
        ]
        result = filter_relevant_changes(changes, set())

        descriptions = {c["description"] for c in result}
        assert descriptions == {"Change B"}
