"""
settings_store.py
------------------
Persists a small number of user-configurable settings (currently just the
Rekordbox XML export path) to a local JSON file, so they survive server
restarts and can be changed from the UI instead of requiring an env var set
before launch.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from webapp import config

SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"

# Where an uploaded (browser file-picker) XML gets saved. Browsers don't
# expose the real filesystem path of a picked file for security reasons, so
# "browse for a file" has to mean "upload its bytes", not "tell the server
# where it is".
UPLOADED_XML_PATH = Path(__file__).resolve().parent / "uploaded_rekordbox.xml"


def _read() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write(data: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(data, indent=2))


def get_rekordbox_xml_path() -> Path:
    """The configured path, falling back to config.REKORDBOX_XML_PATH
    (env var or default) if nothing's been saved via the UI yet."""
    data = _read()
    saved = data.get("rekordbox_xml_path")
    return Path(saved) if saved else config.REKORDBOX_XML_PATH


def set_rekordbox_xml_path(path: str) -> None:
    """Raises FileNotFoundError if the path doesn't exist - callers should
    validate before persisting a path that can't actually be read."""
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise FileNotFoundError(f"'{path}' does not exist or is not a file")
    data = _read()
    data["rekordbox_xml_path"] = str(resolved)
    _write(data)


def save_uploaded_xml(content: bytes) -> Path:
    """Save a browser-uploaded XML's bytes to a fixed local path and make it
    the active rekordbox_xml_path. Returns the saved path."""
    UPLOADED_XML_PATH.write_bytes(content)
    set_rekordbox_xml_path(str(UPLOADED_XML_PATH))
    return UPLOADED_XML_PATH
