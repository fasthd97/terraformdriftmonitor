# Running the tests

These tests exercise the core checker logic (HCL parsing, version-diff
classification, AI changelog relevance filtering) without needing AWS
credentials — the one network-dependent function (`_evaluate_provider`'s
registry lookup) is mocked throughout.

## Setup

The Lambda's runtime dependencies aren't installed in your shell by
default — they're meant to be packaged into the deployment zip, not
necessarily present locally. Install them plus `pytest` before running
tests:

```bash
pip install pytest boto3 python-hcl2 packaging requests --break-system-packages
```

(Drop `--break-system-packages` if you're using a virtualenv instead of
your system Python.)

## Run

```bash
cd terraformdriftmonitor
python3 -m pytest tests/ -v
```

Expected: `53 passed`.

## What's covered

- `tests/test_terraform_parser.py` — HCL parsing: provider extraction,
  resource-type extraction, loose-constraint detection. Includes
  regression tests for two real bugs caught during development (an
  internal-metadata-key leak and a stray-quote leak from `python-hcl2`).
- `tests/test_terraform_versions.py` — version-diff severity
  classification (CRITICAL/WARNING/INFO), loose-constraint flagging,
  and graceful handling of a failed registry lookup. Registry calls
  are mocked.
- `tests/test_ai_changelog.py` — severity-threshold comparison logic
  and breaking-change relevance filtering against a repo's actual
  resource types.

## What's NOT covered

This suite deliberately does not test:
- `check_terraform_versions()` / `_check_repo()` — the top-level
  orchestration functions that call SSM, S3, and the GitHub API
  directly. Testing these meaningfully would require mocking most of
  AWS, for limited additional confidence beyond what's already covered
  here at the unit level.
- The AI changelog extraction call itself
  (`_call_ai_for_extraction`) — this calls the Anthropic API and would
  need either a live API key or an extensive mock of the response
  schema. Not covered here; verified instead via real end-to-end runs
  against the live Lambda (see TROUBLESHOOTING.md for specific bugs
  caught that way).
