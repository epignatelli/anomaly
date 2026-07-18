"""
build_service.py
----------------
Adapts a Rekordbox playlist + phase tags into set_builder.build_set()'s
inputs and calls it in-process (no subprocess) - this is the one place the
web backend and the CLI share the exact same algorithm implementation.

Caches each BuildResult server-side by a build_id so the export endpoint can
reuse the already-computed order without re-running the build.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from set_builder import DEFAULT_SHAPE, BuildError, BuildResult, build_set  # noqa: E402

from webapp import config, settings_store
from webapp.models import to_builder_tracks
from webapp.rekordbox_reader import flatten_playlists, parse_library
from webapp.rekordbox_reader import playlist_tracks as rb_playlist_tracks
from webapp.tag_store import get_constraints, get_tags

_cache: Dict[str, BuildResult] = {}


def run_build(
    playlist_path: str,
    *,
    num_tracks: Optional[int] = None,
    target_minutes: Optional[float] = None,
    pins: Optional[List[str]] = None,
    shape: str = DEFAULT_SHAPE,
    use_phase_tags: bool = False,
    phase_shape: Optional[List[float]] = None,
    key_strict: bool = False,
    key_weight: float = 1.0,
    key_energy_blend: float = 0.5,
    iterations: int = 20000,
) -> tuple[str, BuildResult, Optional[Dict[int, Set[str]]]]:
    """Runs build_set() for a Rekordbox playlist, returns (build_id, result,
    phase_groups) - phase_groups (idx -> set of phase names) is returned
    alongside so the caller can annotate each row of the response with its
    assigned phase, without needing a second lookup against tag_store.
    Raises BuildError on any validation problem (bad pins, unknown playlist,
    not enough phase-tagged candidates, etc.) - the caller (main.py) turns
    that into an HTTP 400."""
    collection, root = parse_library(settings_store.get_rekordbox_xml_path())
    playlists = dict(flatten_playlists(root))
    node = playlists.get(playlist_path)
    if node is None:
        raise BuildError(f"Playlist '{playlist_path}' not found")

    rb_tracks = rb_playlist_tracks(node, collection)
    tracks, idx_to_track_id = to_builder_tracks(rb_tracks)
    track_ids = [t.track_id for t in rb_tracks]

    phase_groups = None
    if use_phase_tags:
        tag_map = get_tags(config.TAGS_DB_PATH, playlist_path, track_ids)
        phase_groups = {
            idx: set(tag_map[tid])
            for idx, tid in idx_to_track_id.items()
            if tag_map.get(tid)
        }

    constraint_map = get_constraints(config.TAGS_DB_PATH, playlist_path, track_ids)
    ignored_ids = {idx for idx, tid in idx_to_track_id.items() if constraint_map.get(tid) == "ignore"}
    required_ids = {idx for idx, tid in idx_to_track_id.items() if constraint_map.get(tid) == "include"}

    result = build_set(
        tracks,
        num_tracks=num_tracks,
        target_minutes=target_minutes,
        pins_spec=pins or [],
        shape=shape,
        key_strict=key_strict,
        key_weight=key_weight,
        key_energy_blend=key_energy_blend,
        iterations=iterations,
        phase_groups=phase_groups,
        phase_segments=phase_shape,
        ignored_ids=ignored_ids,
        required_ids=required_ids,
    )

    build_id = str(uuid.uuid4())
    _cache[build_id] = result
    return build_id, result, phase_groups


def get_cached(build_id: str) -> BuildResult:
    result = _cache.get(build_id)
    if result is None:
        raise BuildError(f"Unknown build_id '{build_id}' (server may have restarted since it was built)")
    return result
