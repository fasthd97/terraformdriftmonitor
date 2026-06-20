"""
git_providers/base.py — Git Provider Abstract Interface
=======================================================
Defines the interface all git provider implementations must follow.

Phase 1 supports GitHub only. GitLab, Bitbucket, S3 are roadmap items —
adding one later is a new file implementing this interface, nothing
else changes.
"""

from abc import ABC, abstractmethod


class GitProvider(ABC):
    """
    Abstract base class for git provider implementations.
    All implementations must implement get_tf_files().
    """

    @abstractmethod
    def get_tf_files(self, repo: str, branch: str, paths: list) -> list:
        """
        Fetches all .tf file contents from the specified repo paths.

        Parameters:
            repo   — str: repository identifier (e.g. "fasthd97/terraformdriftmonitor")
            branch — str: branch to read from (e.g. "main")
            paths  — list: directory paths to scan (e.g. ["terraform/"])

        Returns:
            list of dicts, each with:
                filename — str: the file name (e.g. "versions.tf")
                path     — str: the full path in the repo
                content  — str: raw file content as a string
        """
        pass
