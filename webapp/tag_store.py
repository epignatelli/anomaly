"""
tag_store.py
------------
SQLite-backed storage for phase-group tags, keyed by (playlist_path, track_id).

A track's role in a set is set-dependent (an "opener" in one set might be
"plateau" filler in another), so tags are scoped per playlist by default,
with a global fallback (playlist_path='') for convenience so you don't have
to re-tag a track in every playlist it happens to appear in. Lookup order:
exact playlist match -> global default -> untagged (None).
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from set_builder import PHASES  # noqa: E402

GLOBAL_SCOPE = ""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS track_tags (
    playlist_path TEXT NOT NULL,
    track_id      TEXT NOT NULL,
    phase         TEXT,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (playlist_path, track_id)
);
"""


def _connect(db_path: Union[str, Path]) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(_SCHEMA)
    return conn


def _validate_phase(phase: Optional[str]) -> None:
    if phase is not None and phase not in PHASES:
        raise ValueError(f"Unknown phase '{phase}' (expected one of {PHASES}, or None to clear)")


def set_tag(db_path: Union[str, Path], playlist_path: str, track_id: str, phase: Optional[str]) -> None:
    """Set (or clear, if phase=None) a track's phase tag scoped to a specific
    playlist. An explicit clear at playlist scope masks the global default
    for that playlist (it does not fall through). Use set_global_default()
    for the fallback row instead."""
    _validate_phase(phase)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO track_tags (playlist_path, track_id, phase, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(playlist_path, track_id) DO UPDATE SET phase=excluded.phase, updated_at=excluded.updated_at",
            (playlist_path, track_id, phase, datetime.now(timezone.utc).isoformat()),
        )


def set_global_default(db_path: Union[str, Path], track_id: str, phase: Optional[str]) -> None:
    """Set a track's fallback phase tag, used when no playlist-specific tag exists."""
    set_tag(db_path, GLOBAL_SCOPE, track_id, phase)


def get_tags(db_path: Union[str, Path], playlist_path: str, track_ids: List[str]) -> Dict[str, Optional[str]]:
    """Resolve each track_id's effective phase tag: an exact match for this
    playlist, else the global-default row, else None (untagged)."""
    if not track_ids:
        return {}
    with _connect(db_path) as conn:
        placeholders = ",".join("?" for _ in track_ids)
        rows = conn.execute(
            f"SELECT playlist_path, track_id, phase FROM track_tags "
            f"WHERE track_id IN ({placeholders}) AND playlist_path IN (?, ?)",
            (*track_ids, playlist_path, GLOBAL_SCOPE),
        ).fetchall()

    exact: Dict[str, Optional[str]] = {}
    global_default: Dict[str, Optional[str]] = {}
    for pl, tid, phase in rows:
        if pl == playlist_path:
            exact[tid] = phase
        elif pl == GLOBAL_SCOPE:
            global_default[tid] = phase

    return {tid: (exact[tid] if tid in exact else global_default.get(tid)) for tid in track_ids}
