"""
terraform_parser.py — Terraform HCL Parser
=============================================
Parses raw HCL content from .tf files. Two responsibilities:

  1. parse_providers()      — extracts provider version constraints
                               from terraform.required_providers blocks
  2. parse_resource_types()  — extracts the set of resource TYPES used
                               (e.g. "aws_instance"), needed by
                               checks/ai_changelog.py to filter AI-
                               extracted breaking changes down to only
                               what's relevant to a given repo

Why python-hcl2 instead of regex?
----------------------------------
HCL is a real configuration language — not easily handled by regex.
python-hcl2 is a proper parser that handles all valid HCL2 syntax
including multiline strings, nested blocks, and comments. If we used
regex and a user's .tf file had unusual formatting, we'd silently
miss things.
"""

import logging
import hcl2

logger = logging.getLogger(__name__)


def parse_providers(hcl_content: str, filename: str = "unknown") -> list:
    """
    Parses HCL content and returns a list of provider requirement dicts.

    From a versions.tf like this:

        terraform {
          required_providers {
            aws = {
              source  = "hashicorp/aws"
              version = "~> 5.0"
            }
          }
        }

    Produces:
        [
            {
                "name":       "aws",
                "source":     "hashicorp/aws",
                "namespace":  "hashicorp",
                "constraint": "~> 5.0",
                "file":       "versions.tf"
            },
            ...
        ]

    Parameters:
        hcl_content — str: raw content of a .tf file
        filename    — str: the filename, used in findings for context

    Returns:
        list of provider dicts (see schema above). Empty list if the
        file has no required_providers block or fails to parse — a
        bad file is logged and skipped, not allowed to abort the run.
    """
    providers = []

    try:
        # hcl2.loads() parses the HCL string into a Python dict.
        parsed = hcl2.loads(hcl_content)

    except Exception as e:
        logger.warning(f"Could not parse HCL from '{filename}': {e}")
        return providers

    # hcl2 returns the structure as:
    #   {"terraform": [{"required_providers": [{"aws": {...}, ...}]}]}
    terraform_blocks = parsed.get("terraform", [])

    for terraform_block in terraform_blocks:
        required_providers_list = terraform_block.get("required_providers", [])

        for required_providers in required_providers_list:
            # required_providers is a dict of {provider_name: {source, version}}
            for provider_name, provider_config in required_providers.items():

                # python-hcl2 injects internal metadata keys (e.g.
                # "__comments__", "__is_block__") into the SAME dict as
                # real provider names, depending on parsing mode. Without
                # this check, we'd try to treat hcl2's own bookkeeping
                # as if it were a provider — which is exactly what
                # produced the "__comments__ has no source" warnings.
                if provider_name.startswith("__") and provider_name.endswith("__"):
                    continue

                # provider_config can be a dict or a list containing a dict
                # depending on HCL2 formatting
                if isinstance(provider_config, list):
                    provider_config = provider_config[0] if provider_config else {}

                if not isinstance(provider_config, dict):
                    logger.warning(
                        f"Unexpected provider config format for '{provider_name}' "
                        f"in '{filename}'. Skipping."
                    )
                    continue

                source = provider_config.get("source")
                constraint = provider_config.get("version")

                # Defensive sanitization: python-hcl2 occasionally leaks
                # stray leading/trailing quote characters into string
                # values (the raw token text rather than the evaluated
                # string). Without stripping these, "hashicorp/aws"
                # (literal quotes included) gets sent to the registry
                # API as the namespace, which 404s on every single
                # lookup — this was a real bug caught via a live run,
                # not a hypothetical.
                if isinstance(source, str):
                    source = source.strip('"').strip("'")
                if isinstance(constraint, str):
                    constraint = constraint.strip('"').strip("'")

                if not source:
                    # Source is required to look up the provider in the registry.
                    logger.warning(
                        f"Provider '{provider_name}' in '{filename}' has no source. Skipping."
                    )
                    continue

                # Extract the namespace from the source string.
                # "hashicorp/aws" → namespace="hashicorp", name="aws"
                parts = source.split("/")
                namespace = parts[0] if len(parts) >= 2 else "hashicorp"

                provider_entry = {
                    "name":       provider_name,
                    "source":     source,
                    "namespace":  namespace,
                    "constraint": constraint,  # None if no version pin
                    "file":       filename,
                }

                providers.append(provider_entry)
                logger.info(
                    f"  Found provider: {provider_name} "
                    f"(source: {source}, constraint: {constraint or 'none'})"
                )

    return providers


def parse_resource_types(hcl_content: str, filename: str = "unknown") -> set:
    """
    Parses HCL content and returns the set of resource TYPES declared
    in `resource` blocks (e.g. {"aws_instance", "aws_s3_bucket"}).

    Used by checks/ai_changelog.py's filter_relevant_changes() to narrow
    a provider's full extracted breaking-changes list down to only the
    changes relevant to resources a given repo actually uses.

    From HCL like:
        resource "aws_instance" "web" { ... }
        resource "aws_s3_bucket" "data" { ... }

    Returns: {"aws_instance", "aws_s3_bucket"}

    Parameters:
        hcl_content — str: raw content of a .tf file
        filename    — str: filename, used only for log context on parse errors

    Returns:
        set of resource type strings. Empty set on parse failure —
        same "don't abort the run over one bad file" approach as
        parse_providers above.
    """
    resource_types = set()

    try:
        parsed = hcl2.loads(hcl_content)
    except Exception as e:
        logger.warning(f"Could not parse HCL from '{filename}' for resource types: {e}")
        return resource_types

    # hcl2 returns resource blocks as a list of dicts, each shaped like:
    #   {"aws_instance": {"web": {...resource body...}}}
    # We only need the TYPE (the outer key), not the local name or body.
    resource_blocks = parsed.get("resource", [])

    for resource_block in resource_blocks:
        for resource_type in resource_block.keys():
            # Same defensive skip as parse_providers() above — hcl2 can
            # inject internal metadata keys alongside real resource types.
            if resource_type.startswith("__") and resource_type.endswith("__"):
                continue
            resource_types.add(resource_type)

    return resource_types


def has_loose_constraint(constraint: str | None) -> bool:
    """
    Returns True if the version constraint has no upper bound,
    meaning it could silently auto-upgrade to a breaking major version.

    Examples:
        ">= 5.0"   → True  (no upper bound — could pull v6.0)
        "~> 5.0"   → False (upper bound implied — stays on 5.x)
        "~> 5.1.0" → False (upper bound implied — stays on 5.1.x)
        "5.0.0"    → False (exact pin — never auto-upgrades)
        None       → True  (no constraint at all — anything goes)

    Why this matters:
        A constraint like ">= 5.0" means "any version 5.0 or higher".
        If HashiCorp releases 6.0 with breaking changes, terraform init
        could silently pull it in and break your deployments.
        The ~> operator (pessimistic constraint) prevents this.
    """
    if not constraint:
        return True  # No constraint = completely loose

    # ">=" with no upper bound operator
    if ">=" in constraint and "~>" not in constraint and "<" not in constraint:
        return True

    return False
