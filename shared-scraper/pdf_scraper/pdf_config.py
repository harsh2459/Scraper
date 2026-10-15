"""
PDF Scraper (Scraper B) configuration.

This service is a satellite of the root scraper — same server, same venv,
same .env file, same Postgres database. Rather than duplicating DB/auth
settings, it loads the root config.py directly and registers it under the
name "config" in sys.modules, so that db.py's own `import config` (used
when we import db.py below to reuse save_tma_document) resolves to the
REAL root config instead of accidentally re-resolving to this file.

This has to happen before anything else in this package imports db.py —
Python would otherwise put pdf_scraper/'s own directory first on sys.path
(since it's the running script's directory) and a plain `import config`
anywhere in the process would silently resolve to a from-scratch import of
this very module under a different identity, missing the DB settings db.py
needs (PG_HOST, PG_PORT, etc. — this file intentionally does not redefine
those, it only adds what's unique to this service).
"""

import importlib.util
import sys
from pathlib import Path

_ROOT_DIR = Path(__file__).parent.parent
_ROOT_CONFIG_PATH = _ROOT_DIR / "config.py"

if "config" in sys.modules and getattr(sys.modules["config"], "__file__", None) == str(_ROOT_CONFIG_PATH):
    _root_config = sys.modules["config"]
else:
    _spec = importlib.util.spec_from_file_location("config", _ROOT_CONFIG_PATH)
    _root_config = importlib.util.module_from_spec(_spec)
    sys.modules["config"] = _root_config
    _spec.loader.exec_module(_root_config)

BASIC_AUTH_USER: str = _root_config.BASIC_AUTH_USER
BASIC_AUTH_PASS: str = _root_config.BASIC_AUTH_PASS
USE_WAITRESS: bool = _root_config.USE_WAITRESS

PORT: int = _root_config._int("PDF_SCRAPER_PORT", 5002)

__all__ = ["BASIC_AUTH_USER", "BASIC_AUTH_PASS", "USE_WAITRESS", "PORT"]
