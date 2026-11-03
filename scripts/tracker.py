#!/usr/bin/env python3
"""Command line for the fledge tracker. The code is in the fledge package next to this file."""
import sys
from pathlib import Path

# python3 -I leaves the script's folder off the path, so add it here.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fledge.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
