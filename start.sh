#!/usr/bin/env sh
# Start Minecraft Name Finder on Linux/macOS: ./start.sh
# The first start installs everything it needs into the .venv folder next to this file.
set -e
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/minecraft-finder" ]; then
    echo "First start: setting up Minecraft Name Finder. This takes a minute..."
    if command -v python3 >/dev/null 2>&1; then
        python3 -m venv .venv
        .venv/bin/python -m pip install --quiet --disable-pip-version-check -e .
    elif command -v uv >/dev/null 2>&1; then
        uv venv --quiet --python ">=3.12" .venv
        uv pip install --quiet --python .venv/bin/python -e .
    else
        echo "Python 3.12 or newer is needed: https://www.python.org/downloads/"
        exit 1
    fi
fi

exec .venv/bin/minecraft-finder "$@"
