#!/usr/bin/env bash

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

export PYTHONPATH="$SCRIPT_DIR/:$PYTHONPATH"

exec python3 -B "$SCRIPT_DIR/__main__.py" "$@"
