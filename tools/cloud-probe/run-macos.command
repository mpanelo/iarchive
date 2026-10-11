#!/bin/zsh
set -eu

cd -- "$(dirname -- "$0")/../.."
export UV_CACHE_DIR="$PWD/.uv-cache"
export UV_PYTHON_INSTALL_DIR="$PWD/.smoke/cloud-tools/python"

probe_uv="$(command -v uv || true)"
if [[ -z "$probe_uv" && -x "$HOME/.local/bin/uv" ]]; then
    probe_uv="$HOME/.local/bin/uv"
fi
if [[ -z "$probe_uv" ]]; then
    print -u2 "uv is missing. Install it before running the cloud access probe."
    exit 2
fi

if [[ $# -eq 0 ]]; then
    set -- libraries
fi
"$probe_uv" run --locked --project tools/cloud-probe --python 3.12 \
    python tools/cloud-probe/probe.py "$@"
