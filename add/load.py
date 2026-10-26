"""Compatibility entry point for the loader used in laboratory exercise 2."""

from pathlib import Path
import runpy
import sys

if __name__ == "__main__":
    # Works in the repository (add/load.py) and the image (/app/load.py).
    loader = Path(__file__).resolve().parents[1] / "app" / "python" / "load.py"
    sys.path.insert(0, str(loader.parent))
    runpy.run_path(str(loader), run_name="__main__")
