"""
registry_clients/base.py — Registry Client Abstract Interface
=============================================================
Defines the interface all registry clients must implement.

Right now only the HashiCorp public registry is supported. The
abstract base means adding a new registry (private registry,
Terraform Enterprise, etc.) later is a new file implementing this
interface — nothing else changes.
"""

from abc import ABC, abstractmethod


class RegistryClient(ABC):
    """
    Abstract base class for Terraform provider registry clients.
    All implementations must implement get_latest_version().
    """

    @abstractmethod
    def get_latest_version(self, namespace: str, provider_name: str) -> str | None:
        """
        Returns the latest stable version string for a provider.

        Parameters:
            namespace     — str: the registry namespace (e.g. "hashicorp")
            provider_name — str: the provider name (e.g. "aws")

        Returns:
            str: version string (e.g. "5.100.0"), or None if lookup failed
        """
        pass
