#!/usr/bin/env bash
# scripts/build.sh
# ----------------
# Packages the Lambda function and its Python dependencies for
# deployment. Run automatically by Terraform via null_resource
# local-exec when source files change.
#
# This project's lambda/ structure has THREE subdirectories to copy
# (checks/, git_providers/, registry_clients/) — different from the
# original driftmonitor project, which only had checks/.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
LAMBDA_DIR="$ROOT_DIR/lambda"
DIST_DIR="$ROOT_DIR/dist"
PACKAGE_DIR="$DIST_DIR/package"

echo "=== Building Lambda package ==="
echo "  Lambda source : $LAMBDA_DIR"
echo "  Output dir    : $PACKAGE_DIR"

echo "  Cleaning previous build..."
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"

echo "  Installing dependencies..."
pip install \
  -r "$LAMBDA_DIR/requirements.txt" \
  -t "$PACKAGE_DIR" \
  --quiet \
  --platform manylinux2014_x86_64 \
  --python-version 3.12 \
  --only-binary=:all: \
  --upgrade

echo "  Copying Lambda source files..."

# Root-level .py files (handler.py, notifier.py, terraform_parser.py)
cp "$LAMBDA_DIR"/*.py "$PACKAGE_DIR/"

# Subdirectories — this project has three, not one
cp -r "$LAMBDA_DIR/checks" "$PACKAGE_DIR/checks"
cp -r "$LAMBDA_DIR/git_providers" "$PACKAGE_DIR/git_providers"
cp -r "$LAMBDA_DIR/registry_clients" "$PACKAGE_DIR/registry_clients"

# Entry point check — fail loudly and early if it's missing, rather
# than deploying a broken zip and finding out when the Lambda errors
if [ ! -f "$PACKAGE_DIR/handler.py" ]; then
  echo "ERROR: handler.py not found in package dir. Build failed."
  exit 1
fi

echo "  Package contents:"
ls -la "$PACKAGE_DIR/"

echo "=== Lambda package built successfully ==="
echo "  Output: $PACKAGE_DIR"
