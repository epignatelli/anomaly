"""
config.py
---------
Default paths for the web app. Override via environment variables if your
layout differs (e.g. a different Rekordbox export location).
"""
import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent  # .../anomaly

REKORDBOX_XML_PATH = Path(os.environ.get("REKORDBOX_XML_PATH", str(PROJECT_DIR.parent / "rekordbox.xml")))
TAGS_DB_PATH = Path(os.environ.get("TAGS_DB_PATH", str(PROJECT_DIR / "webapp" / "tags.db")))
LIBRARY_BASE_DIR = Path(os.environ.get("LIBRARY_BASE_DIR", str(PROJECT_DIR.parent / "SoundCloud-LQ")))
