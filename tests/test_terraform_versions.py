"""
tests/test_terraform_versions.py
===================================
Unit tests for checks/terraform_versions.py's version-comparison logic.
The registry client is mocked throughout — these tests never make a
real network call or need AWS credentials.

Run with: pytest tests/test_terraform_versions.py -v
"""

import os
import sys
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

from checks.terraform_versions import (
    _extract_pinned_version,
    _extract_major,
    _evaluate_provider,
)


class TestExtractPinnedVersion:
    @pytest.mark.parametrize("constraint,expected", [
        ("~> 5.0", "5.0"),
        (">= 5.0", "5.0"),
        ("5.0.0", "5.0.0"),
        ("~> 5.100", "5.100"),
        (None, None),
        ("", None),
        ("not a version", None),
    ])
    def test_extraction(self, constraint, expected):
        assert _extract_pinned_version(constraint) == expected


class TestExtractMajor:
    @pytest.mark.parametrize("constraint,expected", [
        ("~> 5.0", "5"),
        (">= 2.0", "2"),
        ("5.0.0", "5"),
        (None, "X"),
        ("garbage", "X"),
    ])
    def test_major_extraction(self, constraint, expected):
        assert _extract_major(constraint) == expected


class TestEvaluateProvider:
    """
    Each test builds a provider dict matching what parse_providers()
    produces, mocks the registry client's response, and checks the
    resulting findings list — without any real HTTP call.
    """

    def _make_provider(self, constraint):
        return {
            "name": "aws",
            "source": "hashicorp/aws",
            "namespace": "hashicorp",
            "constraint": constraint,
            "file": "versions.tf",
        }

    def _mock_registry(self, latest_version):
        registry = MagicMock()
        registry.get_latest_version.return_value = latest_version
        return registry

    def test_up_to_date_produces_no_findings(self):
        provider = self._make_provider("~> 5.100")
        registry = self._mock_registry("5.100.0")

        findings = _evaluate_provider(provider, "test-repo", registry)

        assert findings == []

    def test_major_version_behind_is_critical(self):
        provider = self._make_provider("~> 2.0")
        registry = self._mock_registry("6.51.0")

        findings = _evaluate_provider(provider, "test-repo", registry)

        assert len(findings) == 1
        assert findings[0]["severity"] == "CRITICAL"
        assert findings[0]["pinned"] == "2.0"
        assert findings[0]["latest"] == "6.51.0"
        assert findings[0]["repo_name"] == "test-repo"

    def test_minor_version_behind_is_warning(self):
        provider = self._make_provider("~> 5.0")
        registry = self._mock_registry("5.5.0")

        findings = _evaluate_provider(provider, "test-repo", registry)

        assert len(findings) == 1
        assert findings[0]["severity"] == "WARNING"

    def test_patch_version_behind_is_info(self):
        provider = self._make_provider("~> 5.0.0")
        registry = self._mock_registry("5.0.3")

        findings = _evaluate_provider(provider, "test-repo", registry)

        assert len(findings) == 1
        assert findings[0]["severity"] == "INFO"

    def test_loose_constraint_produces_warning_independent_of_version(self):
        # A loose constraint (>= with no upper bound) should be flagged
        # even if the pinned version happens to already be the latest.
        provider = self._make_provider(">= 5.0")
        registry = self._mock_registry("5.0.0")

        findings = _evaluate_provider(provider, "test-repo", registry)

        severities = [f["severity"] for f in findings]
        assert "WARNING" in severities
        # Confirm it's specifically the loose-constraint finding, not
        # a version-diff finding (which shouldn't exist here at all
        # since latest == pinned).
        loose_finding = next(f for f in findings if "constraint" in f["issue"].lower())
        assert "no upper bound" in loose_finding["detail"].lower()

    def test_loose_constraint_and_major_behind_both_flagged(self):
        # A provider can be BOTH loose AND behind - both findings
        # should be present, not just one.
        provider = self._make_provider(">= 2.0")
        registry = self._mock_registry("6.51.0")

        findings = _evaluate_provider(provider, "test-repo", registry)

        severities = sorted(f["severity"] for f in findings)
        assert severities == ["CRITICAL", "WARNING"]

    def test_registry_failure_does_not_crash(self):
        # If the registry lookup fails (returns None), the function
        # should degrade gracefully, not raise an exception that would
        # abort the entire scan over one provider's lookup failure.
        provider = self._make_provider("~> 5.0")
        registry = self._mock_registry(None)

        findings = _evaluate_provider(provider, "test-repo", registry)

        # No version-diff finding possible without a latest version,
        # but this must not raise.
        assert isinstance(findings, list)
