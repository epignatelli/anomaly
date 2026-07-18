#!/usr/bin/env python3
"""
set_builder.py
---------------
Suggest a track order for a DJ set, balancing two criteria:

  a) energy  - follows a target shape over the course of the set
               (default: starts slow, rises, plateaus, rises again)
  b) in tune - prefers harmonically compatible Camelot-key transitions,
               using the exact same rules as plot_set_energy.py
               (classify_transition / parse_key / mod12, imported not
               reimplemented)

Input is the same Rekordbox TSV/TXT export plot_set_energy.py reads
(tab-separated, UTF-16, with Track Title / Artist / Time / BPM / Comments /
Key columns) - e.g. any of the files under "Session Notes/".

The pool of tracks in the file may be larger than the set you actually
want to play: pass --num-tracks or --target-minutes and the algorithm will
pick the best-fitting subset, not just reorder everything.

You can pin specific tracks to specific positions (e.g. force an opener):

    --pin "Getting Started=1" --pin "Some Closer=last"

Usage
=====
python set_builder.py "Session Notes/Session #7 - Schranz.txt" \
    --num-tracks 20 --pin "Getting Started=1"
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import numpy as np
from scipy.optimize import linear_sum_assignment

from sc_sync import build_id_index
from plot_set_energy import (
    classify_transition,
    parse_comment,
    parse_key,
    read_rekordbox_tsv,
)

Key = Tuple[int, str]

DEFAULT_SHAPE = "0:3,0.35:6,0.6:6,1:9"

# Named energy-phase groups a set can be segmented into, in set order. A
# phased build restricts each segment's candidate pool to only tracks tagged
# with the matching phase, instead of picking freely by energy value alone.
PHASES = ["opening", "first_boost", "plateau", "second_boost", "closing"]
DEFAULT_PHASE_SHAPE = [0.2, 0.2, 0.2, 0.2, 0.2]


class BuildError(ValueError):
    """Raised for user-facing errors during set building (bad --pin/--phase-tags
    input, not enough tagged candidates, etc.), as opposed to programming bugs.
    Caught at the CLI boundary and turned into a plain SystemExit message."""

# Penalty applied per transition category during the local-search refinement.
# Lower = smoother. "other" (off-key clash) is heavily penalized; classify_transition
# and its categories come straight from plot_set_energy.py.
KEY_PENALTY = {
    "perfect": 0.0,
    "boost+": 1.0,
    "drop-": 1.0,
    "boost++": 2.0,
    "drop--": 2.0,
    "boost+++": 2.5,
    "drop---": 2.5,
    "mood": 4.0,
    "other": 9.0,
}

# Signed energy delta implied by the key transition itself (same magnitudes
# plot_set_energy.py's WEIGHTS use for its cumulative-energy chart). This is
# a *separate* axis from KEY_PENALTY above: KEY_PENALTY says how disruptive/
# off-key a transition feels regardless of direction; this says which way
# the transition pushes the perceived energy.
KEY_ENERGY_DELTA = {
    "perfect": 0.0,
    "boost+": 1.0,
    "boost++": 2.0,
    "boost+++": 3.0,
    "drop-": -1.0,
    "drop--": -2.0,
    "drop---": -3.0,
    "mood": 0.0,
    "other": 0.0,
}


@dataclass
class Track:
    idx: int
    title: str
    artist: str
    key: Optional[Key]
    energy: Optional[int]
    bpm: Optional[float]
    duration_s: Optional[int]
    genre: str

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else self.title


@dataclass
class BuildResult:
    """Everything build_set() computes, for a caller (CLI or, in future, a web
    backend) to report/act on however it likes - build_set() itself never
    prints or exits, it only returns data or raises BuildError."""

    order: List[Track]
    targets: List[float]
    pins: Dict[int, Track]
    key_energy_blend: float
    bad_transitions: List[Tuple[int, int, Track, Track]]
    missing_tags: List[Track]      # pool tracks missing Key and/or Energy
    phase_untagged: List[Track]    # eligible tracks with no phase tag (phased builds only, else [])
    not_selected: List[Track]      # eligible (and phase-tagged, if phased) tracks not chosen
    shrink_warning: Optional[str]  # set if the pool didn't have enough candidates to hit the requested n


# ------------------------------
# Parsing
# ------------------------------
def parse_time(s: str) -> Optional[int]:
    s = s.strip()
    if not s or s.lower() == "nan":
        return None
    parts = s.split(":")
    try:
        parts_i = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts_i) == 2:
        m, sec = parts_i
        return m * 60 + sec
    if len(parts_i) == 3:
        h, m, sec = parts_i
        return h * 3600 + m * 60 + sec
    return None


def fmt_duration(total_s: int) -> str:
    m, s = divmod(max(0, total_s), 60)
    return f"{m}:{s:02d}"


def fmt_key(key: Optional[Key]) -> str:
    if key is None:
        return "?"
    n, mode = key
    return f"{n}{mode}"


def _clean(v) -> str:
    s = str(v).strip()
    return "" if s.lower() == "nan" else s


def load_pool(tsv_path: str) -> List[Track]:
    df = read_rekordbox_tsv(tsv_path)
    tracks: List[Track] = []
    for i, row in df.iterrows():
        title = _clean(row.get("Track Title", ""))
        artist = _clean(row.get("Artist", ""))
        comments = _clean(row.get("Comments", ""))

        key = parse_key(_clean(row.get("Key", "")))
        if key is None:
            key = parse_key(comments)

        energy_raw = parse_comment(comments) if comments else -1
        energy = energy_raw if energy_raw >= 0 else None

        try:
            bpm = float(row.get("BPM"))
        except (TypeError, ValueError):
            bpm = None

        duration_s = parse_time(_clean(row.get("Time", "")))
        genre = _clean(row.get("Genre", ""))

        tracks.append(
            Track(
                idx=i,
                title=title,
                artist=artist,
                key=key,
                energy=energy,
                bpm=bpm,
                duration_s=duration_s,
                genre=genre,
            )
        )
    return tracks


def parse_shape(spec: str) -> List[Tuple[float, float]]:
    points: List[Tuple[float, float]] = []
    for part in spec.split(","):
        t_str, e_str = part.split(":")
        points.append((float(t_str), float(e_str)))
    points.sort(key=lambda p: p[0])
    if points[0][0] > 0:
        points.insert(0, (0.0, points[0][1]))
    if points[-1][0] < 1:
        points.append((1.0, points[-1][1]))
    return points


def interpolate(points: List[Tuple[float, float]], t: float) -> float:
    for (t0, e0), (t1, e1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            if t1 == t0:
                return e1
            frac = (t - t0) / (t1 - t0)
            return e0 + frac * (e1 - e0)
    return points[-1][1]


def target_energy_curve(points: List[Tuple[float, float]], n: int) -> List[float]:
    if n == 1:
        return [points[-1][1]]
    return [interpolate(points, i / (n - 1)) for i in range(n)]


# ------------------------------
# Key compatibility (rules live in plot_set_energy.py)
# ------------------------------
def transition_penalty(a: Optional[Key], b: Optional[Key]) -> Tuple[str, float]:
    if a is None or b is None:
        return "unknown", KEY_PENALTY["mood"]
    cat = classify_transition(a, b)
    return cat, KEY_PENALTY.get(cat, KEY_PENALTY["other"])


# ------------------------------
# Pins
# ------------------------------
def parse_pins(
    pin_args: List[str], pool: List[Track], eligible_ids: set, n: int
) -> Dict[int, Track]:
    pins: Dict[int, Track] = {}
    used_ids: Dict[int, int] = {}  # track.idx -> position
    for spec in pin_args:
        if "=" not in spec:
            raise BuildError(f"Invalid --pin '{spec}', expected TITLE_SUBSTRING=POSITION")
        needle, pos_str = spec.rsplit("=", 1)
        needle = needle.strip()
        pos_str = pos_str.strip().lower()
        if pos_str == "first":
            pos = 1
        elif pos_str == "last":
            pos = n
        else:
            try:
                pos = int(pos_str)
            except ValueError:
                raise BuildError(f"Invalid position '{pos_str}' in --pin '{spec}'")
        if not (1 <= pos <= n):
            raise BuildError(f"--pin position {pos} out of range 1..{n}")

        matches = [t for t in pool if needle.lower() in t.label.lower()]
        if not matches:
            raise BuildError(f"--pin '{needle}' matched no track in the input file")
        if len(matches) > 1:
            listing = "\n".join(f"  - {t.label}" for t in matches[:10])
            raise BuildError(f"--pin '{needle}' matched {len(matches)} tracks, be more specific:\n{listing}")
        track = matches[0]
        if track.idx not in eligible_ids:
            raise BuildError(
                f"--pin '{needle}' matched '{track.label}' but it has no parseable "
                f"Key and/or 'Energy N' comment, so it can't be placed."
            )
        if pos in pins:
            raise BuildError(f"Position {pos} pinned more than once")
        if track.idx in used_ids:
            raise BuildError(
                f"'{track.label}' pinned to both position {used_ids[track.idx]} and {pos}"
            )
        pins[pos] = track
        used_ids[track.idx] = pos
    return pins


# ------------------------------
# Selection + placement (energy-fit assignment)
# ------------------------------
def select_and_place(
    candidates: List[Track], free_positions: List[int], targets: List[float]
) -> Dict[int, Track]:
    # Intrinsic-tag-only cost: there's no adjacency/order yet at this stage,
    # so the key-driven cumulative curve (achieved_energy_curve) doesn't apply
    # here - this just gives the local search a good starting point, which is
    # where the blended objective actually takes over.
    if not candidates or not free_positions:
        return {}
    cost = np.empty((len(free_positions), len(candidates)))
    for i, pos in enumerate(free_positions):
        t_target = targets[pos - 1]
        for j, track in enumerate(candidates):
            cost[i, j] = abs(track.energy - t_target)
    row_ind, col_ind = linear_sum_assignment(cost)
    return {free_positions[r]: candidates[c] for r, c in zip(row_ind, col_ind)}


# ------------------------------
# Phase groups: constrain each segment of the set to a tagged subset
# ------------------------------
def phase_segment_ranges(n: int, segments: List[float]) -> Dict[str, range]:
    """Split 1..n into 5 contiguous position ranges, one per PHASES, sized by
    the given proportions (must sum to ~1.0). Cumulative rounding keeps the
    ranges partitioning 1..n exactly, with no gaps or overlaps."""
    if len(segments) != len(PHASES):
        raise BuildError(f"--phase-shape must have {len(PHASES)} values (one per phase: {', '.join(PHASES)})")
    total = sum(segments)
    if not math.isclose(total, 1.0, abs_tol=1e-6):
        raise BuildError(f"--phase-shape values must sum to 1.0 (got {total})")

    boundaries = [0]
    cumulative = 0.0
    for s in segments:
        cumulative += s
        boundaries.append(round(cumulative * n))
    boundaries[-1] = n  # guard against rounding drift

    return {
        phase: range(boundaries[i] + 1, boundaries[i + 1] + 1)
        for i, phase in enumerate(PHASES)
    }


def parse_phase_tags(path: str, pool: List[Track], eligible_ids: set) -> Dict[int, str]:
    """Load a {title_substring: phase_name} JSON mapping, resolved via the same
    fuzzy substring match parse_pins() uses. CLI-only convenience for testing
    the phased algorithm before the web app's tagging UI exists."""
    with open(path) as f:
        raw = json.load(f)

    phase_of: Dict[int, str] = {}
    for needle, phase in raw.items():
        phase = phase.strip().lower().replace(" ", "_")
        if phase not in PHASES:
            raise BuildError(f"--phase-tags: unknown phase '{phase}' for '{needle}' (expected one of {PHASES})")

        matches = [t for t in pool if needle.lower() in t.label.lower()]
        if not matches:
            raise BuildError(f"--phase-tags: '{needle}' matched no track in the input file")
        if len(matches) > 1:
            listing = "\n".join(f"  - {t.label}" for t in matches[:10])
            raise BuildError(f"--phase-tags: '{needle}' matched {len(matches)} tracks, be more specific:\n{listing}")
        track = matches[0]
        if track.idx not in eligible_ids:
            raise BuildError(
                f"--phase-tags: '{needle}' matched '{track.label}' but it has no parseable "
                f"Key and/or 'Energy N' comment, so it can't be placed."
            )
        phase_of[track.idx] = phase
    return phase_of


def select_and_place_phased(
    candidates: List[Track],
    free_positions: List[int],
    targets: List[float],
    phase_of: Dict[int, str],
    segment_ranges: Dict[str, range],
) -> Dict[int, Track]:
    """Run select_and_place() once per phase segment, restricted to that
    segment's positions and only candidates tagged with that phase."""
    placement: Dict[int, Track] = {}
    for phase in PHASES:
        seg_positions = [p for p in free_positions if p in segment_ranges[phase]]
        if not seg_positions:
            continue
        seg_candidates = [t for t in candidates if phase_of.get(t.idx) == phase]
        if len(seg_candidates) < len(seg_positions):
            raise BuildError(
                f"Not enough '{phase}'-tagged eligible track(s) ({len(seg_candidates)}) "
                f"for {len(seg_positions)} position(s) in that segment."
            )
        placement.update(select_and_place(seg_candidates, seg_positions, targets))
    return placement


# ------------------------------
# Local search: swap-based refinement for key compatibility
# ------------------------------
def achieved_energy_curve(order: List[Track], key_energy_blend: float) -> List[float]:
    """The energy value actually fit against the target shape at each position.

    Walks forward: each step is the previous perceived energy plus the key
    transition's signed KEY_ENERGY_DELTA, then pulled back toward that track's
    own intrinsic Energy tag by (1 - key_energy_blend), so key-driven boosts/
    drops nudge the curve without letting it drift away from what the tracks
    are actually tagged. key_energy_blend=0 reduces to intrinsic tags only
    (ignores key-driven energy); =1 is a pure cumulative key-driven walk,
    matching plot_set_energy.py's default (non-baseline) mode.
    """
    curve = [float(order[0].energy)]
    for i in range(1, len(order)):
        cat, _ = transition_penalty(order[i - 1].key, order[i].key)
        delta = KEY_ENERGY_DELTA.get(cat, 0.0)
        walked = curve[i - 1] + delta
        curve.append(key_energy_blend * walked + (1 - key_energy_blend) * order[i].energy)
    return curve


def sequence_cost(
    order: List[Track],
    targets: List[float],
    key_weight: float,
    strict: bool,
    key_energy_blend: float,
) -> float:
    curve = achieved_energy_curve(order, key_energy_blend)
    total = sum(abs(curve[i] - targets[i]) for i in range(len(order)))
    for i in range(len(order) - 1):
        cat, pen = transition_penalty(order[i].key, order[i + 1].key)
        if strict and cat == "other":
            return math.inf
        total += key_weight * pen
    return total


def local_search(
    order: List[Track],
    pinned_positions: set,
    targets: List[float],
    key_weight: float,
    strict: bool,
    max_iterations: int,
    key_energy_blend: float,
    segment_of_position: Optional[Dict[int, str]] = None,
) -> Tuple[List[Track], float]:
    order = order[:]
    movable = [i for i in range(len(order)) if (i + 1) not in pinned_positions]
    best_cost = sequence_cost(order, targets, key_weight, strict, key_energy_blend)
    it = 0
    improved = True
    while improved and it < max_iterations:
        improved = False
        for a in range(len(movable)):
            for b in range(a + 1, len(movable)):
                i, j = movable[a], movable[b]
                if segment_of_position is not None and segment_of_position.get(i + 1) != segment_of_position.get(j + 1):
                    continue  # never swap tracks across a phase-segment boundary
                order[i], order[j] = order[j], order[i]
                new_cost = sequence_cost(order, targets, key_weight, strict, key_energy_blend)
                if new_cost < best_cost - 1e-9:
                    best_cost = new_cost
                    improved = True
                else:
                    order[i], order[j] = order[j], order[i]
                it += 1
                if it >= max_iterations:
                    break
            if it >= max_iterations:
                break
    return order, best_cost


# ------------------------------
# Output
# ------------------------------
def print_result(
    order: List[Track], targets: List[float], pins: Dict[int, Track], key_energy_blend: float
) -> None:
    curve = achieved_energy_curve(order, key_energy_blend)
    print()
    header = f"{'#':>3}  {'Track':<50}  {'Key':>4}  {'En':>3}  {'Achv':>5}  {'Tgt':>4}  {'BPM':>6}  {'Time':>6}  Transition"
    print(header)
    print("-" * len(header))
    total_s = 0
    for i, t in enumerate(order):
        pos = i + 1
        trans = ""
        if i > 0:
            cat, _ = transition_penalty(order[i - 1].key, t.key)
            trans = cat
        dur = t.duration_s or 0
        total_s += dur
        name = t.label
        if len(name) > 50:
            name = name[:47] + "..."
        pin_flag = " *" if pos in pins else ""
        print(
            f"{pos:>3}  {name:<50}  {fmt_key(t.key):>4}  {t.energy:>3}  {curve[i]:>5.1f}  "
            f"{targets[i]:>4.1f}  {(t.bpm or 0):>6.1f}  {fmt_duration(dur):>6}  {trans}{pin_flag}"
        )
    print("-" * len(header))
    print(f"Total duration: {fmt_duration(total_s)}  ({len(order)} tracks)   * = pinned   En = intrinsic tag, Achv = after key-transition blend")


def write_excluded_report(
    not_selected: List[Track], untagged: List[Track], path: str
) -> None:
    """List tracks from the pool that didn't make it into the built set:
    either untagged (missing Key/Energy so they weren't even eligible) or
    eligible but not chosen by the subset selection."""
    lines = []
    lines.append(f"Not selected for this set ({len(not_selected)} eligible track(s), sorted by energy):")
    for t in sorted(not_selected, key=lambda t: (t.energy, t.label)):
        lines.append(f"  [{fmt_key(t.key):>4}  En {t.energy}]  {t.label}")
    lines.append("")
    lines.append(f"Skipped entirely - missing Key and/or Energy tag ({len(untagged)} track(s)):")
    for t in untagged:
        lines.append(f"  {t.label}")
    Path(path).write_text("\n".join(lines) + "\n")


def write_csv(
    order: List[Track], targets: List[float], pins: Dict[int, Track], key_energy_blend: float, path: str
) -> None:
    curve = achieved_energy_curve(order, key_energy_blend)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["Position", "Artist", "Title", "Key", "Energy", "AchievedEnergy", "TargetEnergy", "BPM", "Duration", "TransitionFromPrev", "Pinned"]
        )
        for i, t in enumerate(order):
            pos = i + 1
            trans = ""
            if i > 0:
                cat, _ = transition_penalty(order[i - 1].key, t.key)
                trans = cat
            w.writerow(
                [pos, t.artist, t.title, fmt_key(t.key), t.energy, round(curve[i], 2), round(targets[i], 2),
                 t.bpm, fmt_duration(t.duration_s or 0), trans, pos in pins]
            )


def plot_result(
    order: List[Track], targets: List[float], key_energy_blend: float, out_path: Optional[str], show: bool
) -> None:
    import matplotlib.pyplot as plt

    xs = list(range(1, len(order) + 1))
    actual = achieved_energy_curve(order, key_energy_blend)

    plt.figure()
    plt.plot(xs, actual, marker="o", label="Set energy")
    plt.plot(xs, targets, linestyle="--", label="Target shape")

    other_xs, other_ys = [], []
    for i in range(len(order) - 1):
        cat, _ = transition_penalty(order[i].key, order[i + 1].key)
        if cat == "other":
            other_xs.append(xs[i + 1])
            other_ys.append(actual[i + 1])
    if other_xs:
        plt.scatter(other_xs, other_ys, marker="x", c="red", s=70, label="Off-key transition", zorder=5)

    plt.title("Suggested set - energy vs target shape")
    plt.xlabel("Track #")
    plt.ylabel("Energy (0-10)")
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    if out_path:
        plt.savefig(out_path, dpi=150, bbox_inches="tight")
    if show or not out_path:
        plt.show()
    plt.close()


# ------------------------------
# Rekordbox XML export
# ------------------------------
def _normalize_words(s: str) -> set:
    return {w for w in re.sub(r"[^a-z0-9]+", " ", s.lower()).split() if len(w) > 2}


def resolve_track_paths(
    order: List[Track], base_dir: Path, meta_dir: Path
) -> Dict[int, Optional[Path]]:
    """Map each track's idx to its actual audio file on disk, if findable.

    Tries the SoundCloud ID embedded in the title first (matching sc_sync.py's
    own id_path_map.json / build_id_index), then falls back to fuzzy filename
    matching for tracks whose title lost its bracketed ID somewhere along the way.
    """
    id2file = build_id_index(base_dir, meta_dir)
    resolved: Dict[int, Optional[Path]] = {}
    for t in order:
        m = re.search(r"\[(\d{6,})\]", t.title)
        if m and m.group(1) in id2file:
            resolved[t.idx] = id2file[m.group(1)]
            continue

        needle = _normalize_words(f"{t.artist} {t.title}")
        best_path, best_score = None, 0
        for path in id2file.values():
            score = len(needle & _normalize_words(path.stem))
            if score > best_score:
                best_score, best_path = score, path
        resolved[t.idx] = best_path if best_score >= max(2, len(needle) // 2) else None
    return resolved


def write_playlist_folder(
    order: List[Track], resolved_paths: Dict[int, Optional[Path]], folder_path: str
) -> List[Track]:
    """Create a folder of numbered symlinks to the real audio files, in set
    order. Select-all + drag this folder's contents straight into a new,
    empty Rekordbox playlist - Finder's alphabetical sort matches the numeric
    prefix, so drop order matches the built set order. This sidesteps
    Rekordbox's XML-import path-matching entirely (see write_rekordbox_xml):
    dragging real files is Rekordbox's most basic, most reliable operation,
    and works the same whether a track was already in your library or not.

    Returns the list of tracks that could NOT be resolved to a file and were
    skipped.
    """
    out_dir = Path(folder_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("*"):
        if existing.is_symlink():
            existing.unlink()

    skipped: List[Track] = []
    width = len(str(len(order)))
    for i, t in enumerate(order):
        src = resolved_paths.get(t.idx)
        if not src:
            skipped.append(t)
            continue
        safe_label = re.sub(r'[/:]', '-', t.label)
        link_name = f"{i + 1:0{width}d} - {safe_label}{src.suffix}"
        link_path = out_dir / link_name
        try:
            if link_path.exists() or link_path.is_symlink():
                link_path.unlink()
            os.symlink(src, link_path)
        except OSError as e:
            print(f"WARNING: could not symlink '{t.label}': {e}")
            skipped.append(t)
    return skipped


def write_rekordbox_xml(
    order: List[Track],
    resolved_paths: Dict[int, Optional[Path]],
    playlist_name: str,
    out_path: str,
) -> List[Track]:
    """Write a minimal Rekordbox-XML with a COLLECTION + one PLAYLISTS node,
    importable via Rekordbox's File > Library > Import Library. Track IDs
    reuse the SoundCloud numeric ID (already unique, easy to cross-reference).

    Returns the list of tracks that could NOT be resolved to a file and were
    skipped from the export.
    """
    root = ET.Element(
        "DJ_PLAYLISTS", Version="1.0.0", CreatedByApp="set_builder.py", CreationPlatform="Mac"
    )
    ET.SubElement(root, "PRODUCT", Name="rekordbox", Version="7.2.0", Company="AlphaTheta")

    placed = [t for t in order if resolved_paths.get(t.idx)]
    skipped = [t for t in order if not resolved_paths.get(t.idx)]

    id_pattern = re.compile(r"\[(\d{6,})\]")

    def track_id_for(t: Track, path: Path) -> str:
        # Prefer the ID embedded in the actual filename (most reliable - it's
        # what sc_sync.py itself keys everything on), then the TSV title text,
        # then fall back to a synthetic-but-stable-within-this-file id.
        m = id_pattern.search(path.stem) or id_pattern.search(t.title)
        if m:
            return m.group(1)
        return str(900000000 + t.idx)

    track_ids: Dict[int, str] = {t.idx: track_id_for(t, resolved_paths[t.idx]) for t in placed}

    collection = ET.SubElement(root, "COLLECTION", Entries=str(len(placed)))
    for t in placed:
        path = resolved_paths[t.idx]
        location = "file://localhost" + quote(str(path))
        ET.SubElement(
            collection,
            "TRACK",
            TrackID=track_ids[t.idx],
            Name=t.title,
            Artist=t.artist,
            Album="",
            Genre=t.genre,
            Kind=f"{path.suffix.lstrip('.').upper()} File",
            TotalTime=str(t.duration_s or 0),
            AverageBpm=f"{t.bpm:.2f}" if t.bpm else "0.00",
            Tonality="",
            Comments=f"{fmt_key(t.key)} - Energy {t.energy}",
            Location=location,
        )

    playlists = ET.SubElement(root, "PLAYLISTS")
    root_node = ET.SubElement(playlists, "NODE", Type="0", Name="ROOT", Count="1")
    playlist_node = ET.SubElement(
        root_node, "NODE", Name=playlist_name, Type="1", KeyType="0", Entries=str(len(placed))
    )
    for t in placed:
        ET.SubElement(playlist_node, "TRACK", Key=track_ids[t.idx])

    tree = ET.ElementTree(root)
    ET.indent(tree, space="    ")
    tree.write(out_path, encoding="UTF-8", xml_declaration=True)
    return skipped


# ------------------------------
# Reusable pipeline: same function for the CLI and (in future) a web backend
# ------------------------------
def build_set(
    pool: List[Track],
    *,
    num_tracks: Optional[int] = None,
    target_minutes: Optional[float] = None,
    pins_spec: List[str] = [],
    shape: str = DEFAULT_SHAPE,
    key_strict: bool = False,
    key_weight: float = 1.0,
    key_energy_blend: float = 0.5,
    iterations: int = 20000,
    phase_groups: Optional[Dict[int, str]] = None,
    phase_segments: Optional[List[float]] = None,
) -> BuildResult:
    """Run the full set-building pipeline. Raises BuildError on any
    user-facing validation problem (bad pins, no eligible tracks, not enough
    phase-tagged candidates for a segment, etc.) - never prints, never exits,
    so it's safe to call from a long-running process (a future web backend)
    as well as the CLI below."""
    eligible = [t for t in pool if t.key is not None and t.energy is not None]
    missing_tags = [t for t in pool if t.key is None or t.energy is None]

    if not eligible:
        raise BuildError("No tracks with both a parseable Key and an 'Energy N' comment were found in the input.")

    if num_tracks is not None:
        n = num_tracks
    elif target_minutes is not None:
        durations = [t.duration_s for t in eligible if t.duration_s]
        avg = (sum(durations) / len(durations)) if durations else 240
        n = max(1, round(target_minutes * 60 / avg))
    else:
        n = len(eligible)
    n = max(1, min(n, len(eligible)))

    eligible_ids = {t.idx for t in eligible}
    pins = parse_pins(pins_spec, pool, eligible_ids, n)

    shape_points = parse_shape(shape)
    targets = target_energy_curve(shape_points, n)

    pinned_positions = set(pins.keys())
    pinned_ids = {t.idx for t in pins.values()}
    free_positions = [p for p in range(1, n + 1) if p not in pinned_positions]
    candidates = [t for t in eligible if t.idx not in pinned_ids]

    shrink_warning = None
    if len(candidates) < len(free_positions):
        new_n = len(pins) + len(candidates)
        shrink_warning = (
            f"only {len(candidates)} eligible unpinned track(s) available for "
            f"{len(free_positions)} open slot(s); shrinking the set to {new_n} tracks."
        )
        n = new_n
        targets = target_energy_curve(shape_points, n)
        free_positions = [p for p in free_positions if p <= n]

    phase_untagged: List[Track] = []
    segment_of_position: Optional[Dict[int, str]] = None
    if phase_groups is not None:
        phase_untagged = [t for t in candidates if t.idx not in phase_groups]
        candidates = [t for t in candidates if t.idx in phase_groups]
        segments = phase_segments if phase_segments is not None else DEFAULT_PHASE_SHAPE
        segment_ranges = phase_segment_ranges(n, segments)
        segment_of_position = {p: phase for phase, rng in segment_ranges.items() for p in rng}
        placement = select_and_place_phased(candidates, free_positions, targets, phase_groups, segment_ranges)
    else:
        placement = select_and_place(candidates, free_positions, targets)

    order_by_pos: Dict[int, Track] = {}
    order_by_pos.update(pins)
    order_by_pos.update(placement)
    order = [order_by_pos[p] for p in range(1, n + 1)]

    order, _ = local_search(
        order, pinned_positions, targets, key_weight, key_strict,
        iterations, key_energy_blend, segment_of_position,
    )

    bad_transitions = []
    for i in range(len(order) - 1):
        cat, _ = transition_penalty(order[i].key, order[i + 1].key)
        if cat == "other":
            bad_transitions.append((i + 1, i + 2, order[i], order[i + 1]))

    phase_untagged_ids = {t.idx for t in phase_untagged}
    selected_ids = {t.idx for t in order}
    not_selected = [t for t in eligible if t.idx not in selected_ids and t.idx not in phase_untagged_ids]

    return BuildResult(
        order=order,
        targets=targets,
        pins=pins,
        key_energy_blend=key_energy_blend,
        bad_transitions=bad_transitions,
        missing_tags=missing_tags,
        phase_untagged=phase_untagged,
        not_selected=not_selected,
        shrink_warning=shrink_warning,
    )


# ------------------------------
# main
# ------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Suggest a DJ set order balancing energy shape and in-key (Camelot) mixing."
    )
    ap.add_argument("tsv", help="Path to a Rekordbox TSV/TXT export (tab-separated, UTF-16).")
    ap.add_argument("--num-tracks", type=int, default=None, help="Target number of tracks (selects best-fit subset if the pool is larger).")
    ap.add_argument("--target-minutes", type=float, default=None, help="Approximate target set length in minutes (estimates track count from the pool's average track length). Ignored if --num-tracks is given.")
    ap.add_argument("--pin", action="append", default=[], metavar="TITLE_SUBSTR=POSITION", help="Force a track to a 1-indexed position ('first'/'last' also accepted). Repeatable.")
    ap.add_argument("--shape", default=DEFAULT_SHAPE, help=f"Target energy curve as 'pos:energy,...' control points over [0,1] on a 0-10 scale (default '{DEFAULT_SHAPE}': slow start, rise, plateau, rise).")
    ap.add_argument("--key-strict", action="store_true", help="Never allow an off-key ('other') transition; error out if no valid ordering exists instead of producing one.")
    ap.add_argument("--key-weight", type=float, default=1.0, help="Weight of key-compatibility vs energy-fit during refinement (default 1.0). Higher = prioritize in-key mixing more over the energy shape.")
    ap.add_argument("--key-energy-blend", type=float, default=0.5, help="How much a key transition's own energy direction (boost+/drop-/etc.) nudges the curve fit against your target shape, vs. trusting each track's own Energy tag (default 0.5). 0 = intrinsic tags only; 1 = pure cumulative key-driven walk (matches plot_set_energy.py's default mode).")
    ap.add_argument("--iterations", type=int, default=20000, help="Max local-search swap evaluations (default 20000).")
    ap.add_argument("--out-csv", default=None, help="Write the resulting ordered set to a CSV file.")
    ap.add_argument("--excluded-report", default=None, help="Write a text report of pool tracks NOT included in the built set (both untagged and eligible-but-not-selected).")
    ap.add_argument("--plot-out", default=None, help="Save a PNG comparing the set's energy to the target shape.")
    ap.add_argument("--show", action="store_true", help="Show the plot window.")
    ap.add_argument("--rekordbox-xml", default=None, help="Write a Rekordbox-importable XML playlist (File > Library > Import Library in Rekordbox).")
    ap.add_argument("--playlist-name", default=None, help="Name for the playlist node in --rekordbox-xml (default: input filename + current timestamp, so re-running doesn't collide with a stale playlist already in Rekordbox).")
    ap.add_argument("--library-base-dir", default="../SoundCloud-LQ", help="Base folder sc_sync.py downloads into, used to resolve tracks to real files for --rekordbox-xml / --playlist-folder (default: ../SoundCloud-LQ, alongside the anomaly/ project folder).")
    ap.add_argument("--playlist-folder", default=None, help="Create a folder of numbered symlinks to the real audio files, in set order - select-all and drag straight into a new empty Rekordbox playlist. Avoids Rekordbox's XML-import path-matching quirks entirely; use this if --rekordbox-xml silently drops tracks already in your library.")
    ap.add_argument("--phase-tags", default=None, help=f"Path to a JSON file mapping {{title_substring: phase_name}} to constrain each segment of the set to only tracks tagged with the matching phase ({', '.join(PHASES)}). CLI-only convenience for testing before the web app's tagging UI exists.")
    ap.add_argument("--phase-shape", default=None, help="Comma-separated proportions for the 5 phase segments in order (opening,first_boost,plateau,second_boost,closing), must sum to 1.0. Default: even split (0.2 each). Only used together with --phase-tags.")
    args = ap.parse_args()

    pool = load_pool(args.tsv)

    # Printed up front, before any validation that might abort the build, to
    # match the original (pre-build_set()) behavior where this NOTE appeared
    # even when a later step (e.g. a bad --pin) caused a hard error.
    missing_tags = [t for t in pool if t.key is None or t.energy is None]
    if missing_tags:
        print(f"NOTE: {len(missing_tags)} track(s) skipped (missing Key and/or 'Energy N' comment):")
        for t in missing_tags[:20]:
            print(f"  - {t.label}")
        if len(missing_tags) > 20:
            print(f"  ... and {len(missing_tags) - 20} more")

    phase_groups: Optional[Dict[int, str]] = None
    phase_segments: Optional[List[float]] = None
    if args.phase_tags:
        eligible_ids = {t.idx for t in pool if t.key is not None and t.energy is not None}
        try:
            phase_groups = parse_phase_tags(args.phase_tags, pool, eligible_ids)
        except BuildError as e:
            raise SystemExit(str(e))
        if args.phase_shape:
            phase_segments = [float(x) for x in args.phase_shape.split(",")]

    try:
        result = build_set(
            pool,
            num_tracks=args.num_tracks,
            target_minutes=args.target_minutes,
            pins_spec=args.pin,
            shape=args.shape,
            key_strict=args.key_strict,
            key_weight=args.key_weight,
            key_energy_blend=args.key_energy_blend,
            iterations=args.iterations,
            phase_groups=phase_groups,
            phase_segments=phase_segments,
        )
    except BuildError as e:
        raise SystemExit(str(e))

    if result.phase_untagged:
        print(f"NOTE: {len(result.phase_untagged)} eligible track(s) have no phase tag and are excluded from this phased build:")
        for t in result.phase_untagged[:20]:
            print(f"  - {t.label}")
        if len(result.phase_untagged) > 20:
            print(f"  ... and {len(result.phase_untagged) - 20} more")

    if result.shrink_warning:
        print(f"WARNING: {result.shrink_warning}")

    if args.key_strict and result.bad_transitions:
        print("\nCould not find a fully in-key ordering. Off-key transitions remain:")
        for i, j, a, b in result.bad_transitions:
            print(f"  #{i} {a.label} ({fmt_key(a.key)}) -> #{j} {b.label} ({fmt_key(b.key)})")
        raise SystemExit(2)

    print_result(result.order, result.targets, result.pins, result.key_energy_blend)
    if result.bad_transitions and not args.key_strict:
        print(f"\n{len(result.bad_transitions)} off-key transition(s) remain (soft mode). Use --key-strict to forbid them, or --key-weight to penalize them harder.")

    if args.out_csv:
        write_csv(result.order, result.targets, result.pins, result.key_energy_blend, args.out_csv)
        print(f"\nWrote {args.out_csv}")

    if args.excluded_report:
        all_untagged = result.missing_tags + result.phase_untagged
        write_excluded_report(result.not_selected, all_untagged, args.excluded_report)
        print(f"\nWrote {args.excluded_report} ({len(result.not_selected)} not selected, {len(all_untagged)} untagged)")

    if args.plot_out or args.show:
        plot_result(result.order, result.targets, result.key_energy_blend, args.plot_out, args.show)
        if args.plot_out:
            print(f"Wrote {args.plot_out}")

    if args.rekordbox_xml or args.playlist_folder:
        script_dir = Path(__file__).resolve().parent
        base_dir = (script_dir / args.library_base_dir).resolve()
        meta_dir = base_dir / ".meta"
        resolved = resolve_track_paths(result.order, base_dir, meta_dir)

        if args.rekordbox_xml:
            playlist_name = args.playlist_name or f"{Path(args.tsv).stem} {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            skipped = write_rekordbox_xml(result.order, resolved, playlist_name, args.rekordbox_xml)
            found = len(result.order) - len(skipped)
            print(f"\nWrote {args.rekordbox_xml} ({found}/{len(result.order)} tracks resolved to real files)")
            if skipped:
                print(f"Could not find a file on disk for {len(skipped)} track(s), left out of the XML:")
                for t in skipped:
                    print(f"  - {t.label}")

        if args.playlist_folder:
            skipped = write_playlist_folder(result.order, resolved, args.playlist_folder)
            found = len(result.order) - len(skipped)
            print(f"\nWrote {args.playlist_folder} ({found}/{len(result.order)} tracks) - select all in Finder and drag into a new empty Rekordbox playlist")
            if skipped:
                print(f"Could not find a file on disk for {len(skipped)} track(s), left out of the folder:")
                for t in skipped:
                    print(f"  - {t.label}")


if __name__ == "__main__":
    main()
