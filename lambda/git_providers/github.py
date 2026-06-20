"""
git_providers/github.py — GitHub Git Provider
==============================================
Fetches .tf file contents from GitHub repositories using the
GitHub REST API.

Authentication:
---------------
Public repos: no token needed, unauthenticated requests allowed
              (rate limit: 60 requests/hour — plenty for our use case)
Private repos: requires a GitHub personal access token with
               Contents: Read-only permission, stored in SSM.
               Each repo entry in the config can specify its own
               token_ssm_param; if it doesn't, the caller (checks/
               terraform_versions.py) falls back to the single
               bootstrap-created default token parameter.

API used:
---------
GET /repos/{owner}/{repo}/contents/{path}
Returns a list of files/directories at the path. For files, we then
fetch the content directly. GitHub returns file content as base64.
"""

import base64
import logging
import boto3
import requests
from botocore.exceptions import ClientError
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 15


class GitHubProvider:
    """
    Fetches .tf file contents from GitHub repositories.
    Implements the GitProvider interface.
    """

    def __init__(self):
        # Token cache — fetched from SSM once per Lambda invocation,
        # reused across multiple repos that share the same token param.
        self._token_cache = {}

    def get_tf_files(
        self,
        repo: str,
        branch: str,
        paths: list,
        private: bool = False,
        token_ssm_param: str = None,
    ) -> list:
        """
        Fetches all .tf files from the specified paths in a GitHub repo.

        Parameters:
            repo            — str: "owner/repo-name" format
            branch          — str: branch name (e.g. "main")
            paths           — list: directory paths to scan
            private         — bool: whether the repo is private
            token_ssm_param — str: SSM parameter name for GitHub token
                              (required if private=True)

        Returns:
            list of dicts with filename, path, content keys
        """
        token = None
        if private:
            if not token_ssm_param:
                logger.error(
                    f"Repo '{repo}' is marked private but no token_ssm_param "
                    "was resolved (not set on the repo entry and no default "
                    "available). Skipping."
                )
                return []
            token = self._get_token(token_ssm_param)
            if not token:
                return []

        headers = self._build_headers(token)
        tf_files = []

        for path in paths:
            clean_path = path.strip("/")
            logger.info(f"  Scanning {repo}/{clean_path} on branch '{branch}'...")

            files = self._list_tf_files(repo, branch, clean_path, headers)

            for file_info in files:
                content = self._fetch_file_content(
                    repo, branch, file_info["path"], headers
                )
                if content is not None:
                    tf_files.append({
                        "filename": file_info["name"],
                        "path":     file_info["path"],
                        "content":  content,
                    })

        logger.info(f"  Found {len(tf_files)} .tf file(s) in {repo}")
        return tf_files

    def _list_tf_files(self, repo: str, branch: str, path: str, headers: dict) -> list:
        """Lists all .tf files in a directory path on a branch."""
        url = f"{GITHUB_API_URL}/repos/{repo}/contents/{path}"
        params = {"ref": branch}

        try:
            response = requests.get(
                url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS
            )

            if response.status_code == 404:
                logger.warning(
                    f"  Path '{path}' not found in '{repo}' on branch '{branch}'. "
                    "Check the paths config."
                )
                return []

            response.raise_for_status()
            items = response.json()

            tf_files = [
                {"name": item["name"], "path": item["path"]}
                for item in items
                if item["type"] == "file" and item["name"].endswith(".tf")
            ]

            logger.info(f"  Found {len(tf_files)} .tf file(s) at '{path}'")
            return tf_files

        except RequestException as e:
            logger.warning(f"  GitHub API error listing '{path}' in '{repo}': {e}")
            return []

    def _fetch_file_content(self, repo: str, branch: str, file_path: str, headers: dict) -> str | None:
        """Fetches and decodes the content of a single file (base64-encoded by GitHub)."""
        url = f"{GITHUB_API_URL}/repos/{repo}/contents/{file_path}"
        params = {"ref": branch}

        try:
            response = requests.get(
                url, headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            data = response.json()

            encoded_content = data.get("content", "")
            decoded = base64.b64decode(encoded_content).decode("utf-8")
            logger.info(f"  Fetched: {file_path}")
            return decoded

        except (RequestException, Exception) as e:
            logger.warning(f"  Failed to fetch '{file_path}' from '{repo}': {e}")
            return None

    def _build_headers(self, token: str | None) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _get_token(self, ssm_parameter_name: str) -> str | None:
        """Fetches a GitHub token from SSM, cached per Lambda invocation."""
        if ssm_parameter_name in self._token_cache:
            return self._token_cache[ssm_parameter_name]

        ssm = boto3.client("ssm")
        try:
            response = ssm.get_parameter(Name=ssm_parameter_name, WithDecryption=True)
            token = response["Parameter"]["Value"]
            self._token_cache[ssm_parameter_name] = token
            return token

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "ParameterNotFound":
                logger.error(
                    f"GitHub token SSM parameter '{ssm_parameter_name}' not found."
                )
            else:
                logger.error(f"SSM error fetching GitHub token: {e}")
            return None
