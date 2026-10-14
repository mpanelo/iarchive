#!/bin/sh
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$repo"
if [ ! -x target/release/iarchive ]; then
    echo 'Build iarchive first; see docs/workflows.md.' >&2
    exit 1
fi
export CARGO_HOME="${CARGO_HOME:-$repo/.cargo-home}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$repo/.uv-cache}"
exec uv run --locked --project image-worker python scripts/demo.py
