import sys
from pathlib import Path

# `support/ci` is the import root the tools run under (run.sh puts it on
# PYTHONPATH); pytest is started from the repo root, so put it there ourselves.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
