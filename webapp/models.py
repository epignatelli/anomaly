"""
models.py
---------
Bridges the Rekordbox-XML layer (rekordbox_reader.RBTrack) into
set_builder.Track instances, so build_set() never needs to know anything
about Rekordbox.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from set_builder import Track  # noqa: E402

from webapp.rekordbox_reader import RBTrack


def to_builder_tracks(rb_tracks: List[RBTrack]) -> Tuple[List[Track], Dict[int, str]]:
    """Adapt a list of RBTrack (in playlist order) into set_builder.Track
    instances, assigning each a 0-based idx matching its position in this
    list. Also returns idx -> Rekordbox track_id, since tag_store is keyed
    by track_id but build_set()'s phase_groups is keyed by idx - callers
    use this mapping to translate between the two."""
    tracks: List[Track] = []
    idx_to_track_id: Dict[int, str] = {}
    for i, t in enumerate(rb_tracks):
        tracks.append(
            Track(
                idx=i,
                title=t.name,
                artist=t.artist,
                key=t.key,
                energy=t.energy,
                bpm=t.bpm,
                duration_s=t.total_time_s,
                genre=t.genre,
            )
        )
        idx_to_track_id[i] = t.track_id
    return tracks, idx_to_track_id
