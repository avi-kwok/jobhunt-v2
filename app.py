"""Vercel entrypoint (zero-config FastAPI detection looks for `app` in app.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from jobhunt.web.app import app  # noqa: E402,F401
