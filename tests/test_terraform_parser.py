"""
tests/test_terraform_parser.py
================================
Unit tests for terraform_parser.py. No AWS credentials, no network
calls — these test pure HCL parsing logic against fixture files and
inline HCL strings.

Run with: pytest tests/test_terraform_parser.py -v
"""

import os
import sys
import pytest

# Allow importing lambda/ modules directly without installing as a package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

from terraform_parser import parse_providers, parse_resource_types, has_loose_constraint

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_fixture(name: str) -> str:
    with open(os.path.join(FIXTURES_DIR, name)) as f:
        return f.read()


class TestParseProviders:
    def test_parses_single_provider(self):
        content = _load_fixture("healthy.tf")
        providers = parse_providers(content, filename="healthy.tf")

        assert len(providers) == 1
        assert providers[0]["name"] == "aws"
        assert providers[0]["source"] == "hashicorp/aws"
        assert providers[0]["constraint"] == "~> 5.100"
        assert providers[0]["namespace"] == "hashicorp"

    def test_parses_loose_constraint_provider(self):
        content = _load_fixture("loose_constraint.tf")
        providers = parse_providers(content, filename="loose_constraint.tf")

        assert len(providers) == 1
        assert providers[0]["constraint"] == ">= 5.0"

    def test_missing_source_is_skipped_not_crashed(self):
        # A provider block with no source should be skipped gracefully,
        # not raise an exception and abort the whole scan.
        content = """
        terraform {
          required_providers {
            broken = {
              version = "~> 1.0"
            }
          }
        }
        """
        providers = parse_providers(content, filename="broken.tf")
        assert providers == []

    def test_invalid_hcl_returns_empty_list_not_exception(self):
        # A malformed file should never crash the scan — one bad file
        # in a repo shouldn't abort checking every other file.
        content = "this is not { valid hcl at all ]["
        providers = parse_providers(content, filename="garbage.tf")
        assert providers == []

    def test_no_required_providers_block_returns_empty(self):
        content = """
        resource "aws_s3_bucket" "example" {
          bucket = "test"
        }
        """
        providers = parse_providers(content, filename="no_providers.tf")
        assert providers == []

    def test_multiple_providers_in_one_block(self):
        content = """
        terraform {
          required_providers {
            aws = {
              source  = "hashicorp/aws"
              version = "~> 5.0"
            }
            archive = {
              source  = "hashicorp/archive"
              version = "~> 2.0"
            }
          }
        }
        """
        providers = parse_providers(content, filename="multi.tf")
        names = {p["name"] for p in providers}
        assert names == {"aws", "archive"}


class TestParseResourceTypes:
    def test_extracts_resource_types(self):
        content = _load_fixture("breaking_change.tf")
        types = parse_resource_types(content, filename="breaking_change.tf")

        assert types == {"aws_ami", "aws_ecs_task_definition", "aws_s3_bucket"}

    def test_no_resources_returns_empty_set(self):
        content = _load_fixture("healthy.tf")
        # healthy.tf has one aws_s3_bucket resource
        types = parse_resource_types(content, filename="healthy.tf")
        assert types == {"aws_s3_bucket"}

    def test_file_with_zero_resources(self):
        content = """
        terraform {
          required_providers {
            aws = {
              source  = "hashicorp/aws"
              version = "~> 5.0"
            }
          }
        }
        """
        types = parse_resource_types(content, filename="no_resources.tf")
        assert types == set()

    def test_invalid_hcl_returns_empty_set_not_exception(self):
        content = "not valid hcl { [ }"
        types = parse_resource_types(content, filename="garbage.tf")
        assert types == set()

    def test_dunder_metadata_keys_are_excluded(self):
        # Regression test for a real bug caught via a live Lambda run:
        # python-hcl2 can inject internal metadata keys (e.g.
        # "__comments__", "__is_block__") into the same dict as real
        # resource type names. Without filtering these out, they'd be
        # incorrectly counted as resource types. This test simulates
        # that shape directly against the function's filtering logic
        # by checking real parsed output never contains a dunder key,
        # using a file that previously triggered exactly this issue.
        content = _load_fixture("breaking_change.tf")
        types = parse_resource_types(content, filename="breaking_change.tf")

        for t in types:
            assert not (t.startswith("__") and t.endswith("__")), (
                f"Found unfiltered internal metadata key: {t}"
            )


class TestHasLooseConstraint:
    @pytest.mark.parametrize("constraint,expected", [
        (None, True),                    # no constraint at all = loose
        ("", True),                      # empty string = loose
        (">= 5.0", True),                # no upper bound = loose
        ("~> 5.0", False),                # pessimistic constraint = not loose
        ("~> 5.1.0", False),              # pessimistic with patch = not loose
        ("5.0.0", False),                 # exact pin = not loose
        (">= 5.0, < 6.0", False),          # explicit upper bound = not loose
    ])
    def test_loose_constraint_detection(self, constraint, expected):
        assert has_loose_constraint(constraint) == expected
