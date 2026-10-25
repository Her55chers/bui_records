"""
Vercel entry point: one Python function serves the whole site (API + frontend) from backend/app.py.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

import database  # noqa: E402

# Vercel may not run ASGI lifespan events, so create (and seed) the /tmp database here.
# init_db() is idempotent: a warm instance that already has the file just reuses it.
database.init_db()

from app import app  # noqa: E402,F401  (Vercel looks for the ASGI variable named `app`)
