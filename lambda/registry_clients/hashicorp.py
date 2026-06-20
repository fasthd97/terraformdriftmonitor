"""
registry_clients/hashicorp.py — HashiCorp Public Registry Client
=================================================================
Fetches the latest available version for a Terraform provider from
the HashiCorp public registry API.

API endpoint:
    GET https://registry.terraform.io/v1/providers/{namespace}/{type}/versions

Returns a list of all published versions. We filter to stable releases
(no pre-release tags like -alpha, -beta, -rc) and return the highest one,
using the packaging library for correct semantic version comparison
(plain string sort gets "5.9.0" vs "5.100.0" wrong).
"""

import logging
import requests
from packaging.version import Version, InvalidVersion
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

REGISTRY_BASE_URL = "https://registry.terraform.io/v1/providers"
REQUEST_TIMEOUT_SECONDS = 15


class HashiCorpRegistryClient:
    """
    Fetches provider version information from the HashiCorp public registry.
    Implements the RegistryClient interface.
    """

    def get_latest_version(self, namespace: str, provider_name: str) -> str | None:
        """
        Returns the latest stable version string for a provider.

        Parameters:
            namespace     — str: registry namespace (e.g. "hashicorp")
            provider_name — str: provider name (e.g. "aws")

        Returns:
            str: latest stable version (e.g. "5.100.0"), or None if lookup failed
        """
        url = f"{REGISTRY_BASE_URL}/{namespace}/{provider_name}/versions"
        logger.info(f"  Fetching latest version for {namespace}/{provider_name} from registry...")

        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            data = response.json()

        except RequestException as e:
            logger.warning(
                f"  Registry API request failed for {namespace}/{provider_name}: {e}"
            )
            return None

        versions_raw = data.get("versions", [])

        if not versions_raw:
            logger.warning(f"  No versions found for {namespace}/{provider_name}.")
            return None

        stable_versions = []

        for v in versions_raw:
            version_str = v.get("version", "")
            try:
                parsed = Version(version_str)
                if not parsed.is_prerelease:
                    stable_versions.append(parsed)
            except InvalidVersion:
                continue

        if not stable_versions:
            logger.warning(
                f"  No stable versions found for {namespace}/{provider_name}."
            )
            return None

        latest = str(max(stable_versions))
        logger.info(f"  Latest stable version: {latest}")
        return latest
