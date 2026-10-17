"""Vercel entry point: the FastAPI app as a Python serverless function."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from company_brain.api import create_app

app = create_app()
