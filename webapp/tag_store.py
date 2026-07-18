"""
tag_store.py
------------
SQLite-backed storage for phase-group tags, keyed by (playlist_path,
track_id). A track can carry more than one phase tag at once (e.g. both
"opening" and "valley"), so each active tag is its own row rather than a
single column value.

A track's role in a set is set-dependent (an "opener" in one set might be
"valley" filler in another), so tags are scoped per playlist by default,
with a global fallback (playlist_path='') for convenience so you don't have
to re-tag a track in every playlist it happens to appear in. Fallback rule:
if a track has ANY playlist-specific tag rows (even just one), those are its
complete, authoritative tag set for that playlist - the global default is
only consulted when there are zero playlist-specific rows for that track.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Union

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from set_builder import PHASES  # noqa: E402

GLOBAL_SCOPE = ""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS track_tags (
    playlist_path TEXT NOT NULL,
    track_id      TEXT NOT NULL,
    phase         TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (playlist_path, track_id, phase)
);

CREATE TABLE IF NOT EXISTS track_constraints (
    playlist_path TEXT NOT NULL,
    track_id      TEXT NOT NULL,
    state         TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (playlist_path, track_id)
);
"""

CONSTRAINT_STATES = ("ignore", "include")

# Renaming a phase name in set_builder.PHASES orphans any tags already
# stored under the old name (the phase column is a free-text string, not
# validated against PHASES at the DB level) - track renames here so existing
# tags self-heal on the next connection instead of silently disappearing.
_PHASE_RENAMES = {
    "plateau": "valley",
    "first_boost": "first_peak",
    "second_boost": "second_peak",
}
_migrated_dbs: set = set()


def _connect(db_path: Union[str, Path]) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    key = str(db_path)
    if key not in _migrated_dbs:
        for old, new in _PHASE_RENAMES.items():
            conn.execute("UPDATE OR IGNORE track_tags SET phase = ? WHERE phase = ?", (new, old))
        conn.commit()
        _migrated_dbs.add(key)
    return conn


def _validate_phase(phase: str) -> None:
    if phase not in PHASES:
        raise ValueError(f"Unknown phase '{phase}' (expected one of {PHASES})")


def add_tag(db_path: Union[str, Path], playlist_path: str, track_id: str, phase: str) -> None:
    """Add phase to a track's tag set for this playlist. Idempotent - adding
    a phase that's already set is a no-op (just refreshes updated_at)."""
    _validate_phase(phase)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO track_tags (playlist_path, track_id, phase, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(playlist_path, track_id, phase) DO UPDATE SET updated_at=excluded.updated_at",
            (playlist_path, track_id, phase, datetime.now(timezone.utc).isoformat()),
        )


def remove_tag(db_path: Union[str, Path], playlist_path: str, track_id: str, phase: str) -> None:
    """Remove phase from a track's tag set for this playlist. Idempotent -
    removing a phase that isn't set is a no-op."""
    _validate_phase(phase)
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM track_tags WHERE playlist_path = ? AND track_id = ? AND phase = ?",
            (playlist_path, track_id, phase),
        )


def add_global_tag(db_path: Union[str, Path], track_id: str, phase: str) -> None:
    add_tag(db_path, GLOBAL_SCOPE, track_id, phase)


def remove_global_tag(db_path: Union[str, Path], track_id: str, phase: str) -> None:
    remove_tag(db_path, GLOBAL_SCOPE, track_id, phase)


def get_tags(db_path: Union[str, Path], playlist_path: str, track_ids: List[str]) -> Dict[str, List[str]]:
    """Resolve each track_id's effective tag set: its playlist-specific
    phases if it has any at all, else its global-default phases, else []."""
    if not track_ids:
        return {}
    with _connect(db_path) as conn:
        placeholders = ",".join("?" for _ in track_ids)
        rows = conn.execute(
            f"SELECT playlist_path, track_id, phase FROM track_tags "
            f"WHERE track_id IN ({placeholders}) AND playlist_path IN (?, ?)",
            (*track_ids, playlist_path, GLOBAL_SCOPE),
        ).fetchall()

    exact: Dict[str, List[str]] = {}
    global_default: Dict[str, List[str]] = {}
    for pl, tid, phase in rows:
        target = exact if pl == playlist_path else global_default
        target.setdefault(tid, []).append(phase)

    return {tid: (exact[tid] if tid in exact else global_default.get(tid, [])) for tid in track_ids}


def set_constraint(db_path: Union[str, Path], playlist_path: str, track_id: str, state: Union[str, None]) -> None:
    """Set a track's build constraint for this playlist: 'ignore' (never
    include when building a set), 'include' (always include), or None to
    reset to the default (no constraint - the algorithm decides freely).
    Playlist-scoped only, no global fallback - this is meant as a per-set
    decision, not a durable property of the track."""
    if state is not None and state not in CONSTRAINT_STATES:
        raise ValueError(f"Unknown constraint state '{state}' (expected one of {CONSTRAINT_STATES}, or None to reset)")
    with _connect(db_path) as conn:
        if state is None:
            conn.execute(
                "DELETE FROM track_constraints WHERE playlist_path = ? AND track_id = ?",
                (playlist_path, track_id),
            )
        else:
            conn.execute(
                "INSERT INTO track_constraints (playlist_path, track_id, state, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(playlist_path, track_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
                (playlist_path, track_id, state, datetime.now(timezone.utc).isoformat()),
            )


def get_constraints(db_path: Union[str, Path], playlist_path: str, track_ids: List[str]) -> Dict[str, Union[str, None]]:
    """Each track_id's current constraint state ('ignore'/'include'), or None
    if unset."""
    if not track_ids:
        return {}
    with _connect(db_path) as conn:
        placeholders = ",".join("?" for _ in track_ids)
        rows = conn.execute(
            f"SELECT track_id, state FROM track_constraints "
            f"WHERE playlist_path = ? AND track_id IN ({placeholders})",
            (playlist_path, *track_ids),
        ).fetchall()
    states = dict(rows)
    return {tid: states.get(tid) for tid in track_ids}
