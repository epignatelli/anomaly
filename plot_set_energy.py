#!/usr/bin/env python3
"""
dj_energy_plot.py
-----------------
Compute and plot *energy variation* across a DJ set based on Camelot-key transitions,
using the chart you provided (A/B sections with Perfect / Energy boost / Energy drop / Mood change).

Input: a Rekordbox TSV export (tab-separated) with columns including "Track Title" and "Key".
Output: a CSV table of transitions (optional) and a PNG (or an on-screen plot) of the cumulative energy.

Energy mapping (default):
- perfect      ->  0
- boost+       -> +1
- boost++      -> +2
- boost+++     -> +3
- drop-        -> -1
- drop--       -> -2
- drop---      -> -3
- mood         ->  0   (mood change, energy neutral by default)
- other        ->  0   (unclassified transitions)

Usage
=====
python dj_energy_plot.py path/to/set.tsv --plot-out energy.png --csv-out energy_profile.csv

Optional flags:
--start-energy INT           starting energy (default 0)
--mood-weight INT            energy assigned to 'mood' transitions (default 0)
--other-weight INT           energy for 'other' (default 0)
--show                       show the matplotlib window instead of saving only
--no-mood-markers            disable special markers for mood-change transitions
"""
from __future__ import annotations

import argparse
from typing import Dict, List, Optional, Tuple
import re

import pandas as pd
import matplotlib.pyplot as plt


# ---------- Parsing helpers ----------


def read_rekordbox_tsv(path: str) -> pd.DataFrame:
    """Read a tab-separated Rekordbox export. Normalizes columns a bit."""
    df = pd.read_csv(path, sep="\t", encoding="utf-16", engine="python")
    df.columns = [c.strip().replace("#", "Index") for c in df.columns]
    return df


def parse_key(comment: str) -> Optional[Tuple[int, str]]:
    """Parse Camelot key like '8A' or '9B' into (n, mode). Returns None if invalid."""
    if not isinstance(comment, str) or len(comment) < 2:
        return None

    comment = comment.strip().lower()
    match = re.search(r"(\d+[ab])", comment)
    if match:
        key = match.group(1)
    else:
        return None

    s = key.strip().upper()
    if s[-1] not in ("A", "B"):
        return None
    try:
        n = int(s[:-1])
    except ValueError:
        return None
    if n < 1 or n > 12:
        return None
    return n, s[-1]


def mod12(n: int) -> int:
    """1..12 wrap-around."""
    return ((n - 1) % 12) + 1


def parse_comment(comment: str) -> int:
    """Parse a comment string and returns the baseline energy level of the track.
    Comments are in the form `8A - Energy 8`, where 8A is a key, and 8 is the energy level.
    Returns -1 of no energy level is found.
    """
    comment = comment.strip().lower()
    if "energy" in comment:
        # use regex to get energy level (0 to 10)
        match = re.search(r"energy (\d+)", comment)
        if match:
            energy_level = int(match.group(1))
            if 0 <= energy_level <= 10:
                return energy_level
    return -1


# ---------- Chart logic ----------


def classify_transition(src: Tuple[int, str], dst: Tuple[int, str]) -> str:
    """
    Classify the energy category of the transition src -> dst according to the chart.

    Categories: 'perfect', 'boost+', 'boost++', 'boost+++', 'drop-', 'drop--', 'drop---',
    'mood', 'other'.
    """
    n, m = src
    p, q = dst

    if m == "A":
        if q == "A":
            if p == n:
                return "perfect"
            if p == mod12(n + 1):
                return "boost+"
            if p == mod12(n - 3):
                return "boost++"
            if p in (mod12(n + 2), mod12(n + 7)):
                return "boost+++"
            if p == mod12(n - 1):
                return "drop-"
            if p == mod12(n + 3):
                return "drop--"
            if p in (mod12(n - 2), mod12(n + 5)):
                return "drop---"
        else:  # q == "B"
            if p == mod12(n - 1):
                return "perfect"
            if p == n:
                return "boost+"
            if p == mod12(n + 3):
                return "mood"
            return "other"
    else:  # m == "B"
        if q == "B":
            if p == n:
                return "perfect"
            if p == mod12(n + 1):
                return "boost+"
            if p == mod12(n - 3):
                return "boost++"
            if p in (mod12(n + 2), mod12(n + 7)):
                return "boost+++"
            if p == mod12(n - 1):
                return "drop-"
            if p == mod12(n + 3):
                return "drop--"
            if p in (mod12(n - 2), mod12(n + 5)):
                return "drop---"
        else:  # q == "A"
            if p == mod12(n + 1):
                return "perfect"
            if p == n:
                return "drop-"
            if p == mod12(n - 3):
                return "mood"
            return "other"

    return "other"


# ---------- Energy computation ----------


def compute_energy_profile(
    df: pd.DataFrame,
    weight_map: Dict[str, int],
    start_energy: int = 0,
    use_baseline_energy: bool = False,
) -> pd.DataFrame:
    """Compute per-transition deltas and cumulative energy for a Rekordbox dataframe."""
    if "Key" not in df.columns or "Track Title" not in df.columns:
        raise ValueError("Input must include 'Key' and 'Track Title' columns.")

    titles: List[str] = df["Track Title"].astype(str).tolist()
    keys = [parse_key(comment) for comment in df["Comments"].astype(str).tolist()]
    energies = [parse_comment(c) for c in df["Comments"].astype(str).tolist()]

    rows: List[Dict] = []
    cumulative = start_energy

    # First track (no transition)
    rows.append(
        {
            "Index": 1,
            "Title": titles[0],
            "Key": keys[0],
            "From->To": None,
            "Category": None,
            "Delta": 0,
            "CumulativeEnergy": cumulative,
        }
    )

    for i in range(1, len(keys)):
        src = keys[i - 1]
        dst = keys[i]
        cat = "other"
        if src and dst:
            cat = classify_transition(src, dst)
        delta = weight_map.get(cat, 0)
        baseline_delta = energies[i] - energies[i - 1] if use_baseline_energy else 0
        cumulative += delta + baseline_delta

        rows.append(
            {
                "Index": i + 1,
                "Title": titles[i],
                "Key": keys[i],
                "From->To": f"{keys[i-1]}→{keys[i]}",
                "Category": cat,
                "Delta": delta,
                "BaselineDelta": baseline_delta,
                "CumulativeEnergy": cumulative,
            }
        )

    return pd.DataFrame(rows)


def plot_energy(
    profile: pd.DataFrame,
    title: str,
    show: bool,
    out_path: Optional[str] = None,
    mark_mood: bool = True,
) -> None:
    plt.figure()
    plt.plot(profile["Index"], profile["CumulativeEnergy"], marker="o", label="Energy")

    # Mood-change markers (existing)
    if mark_mood:
        mask_mood = profile["Category"] == "mood"
        if mask_mood.any():
            plt.scatter(
                profile.loc[mask_mood, "Index"],
                profile.loc[mask_mood, "CumulativeEnergy"],
                marker="X",
                c="red",
                s=80,
                label="Mood change",
                zorder=5,
            )

    # NEW: off-key ("other") markers as a red cross
    mask_other = profile["Category"] == "other"
    if mask_other.any():
        plt.scatter(
            profile.loc[mask_other, "Index"],
            profile.loc[mask_other, "CumulativeEnergy"],
            marker="x",  # red cross
            c="red",
            s=70,
            linewidths=1.5,
            label="Not in key",
            zorder=6,
        )

    plt.title(title)
    plt.xlabel("Track #")
    plt.ylabel("Cumulative energy (arbitrary units)")
    plt.yticks(
        range(min(profile["CumulativeEnergy"]), max(profile["CumulativeEnergy"]) + 1)
    )
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    if out_path:
        plt.savefig(out_path, dpi=150, bbox_inches="tight")
    if show or not out_path:
        plt.show()
    plt.close()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Plot energy variation across a DJ set based on Camelot keys."
    )
    ap.add_argument("tsv", help="Path to Rekordbox TSV export.")
    ap.add_argument(
        "--plot-out",
        default=None,
        help="Path to save PNG figure (e.g., energy.png). If omitted, only shows.",
    )
    ap.add_argument(
        "--csv-out",
        default=None,
        help="Path to save the energy profile CSV (optional).",
    )
    ap.add_argument(
        "--start-energy", type=int, default=0, help="Starting energy value (default 0)."
    )
    ap.add_argument(
        "--mood-weight",
        type=int,
        default=0,
        help="Energy assigned to 'mood' transitions (default 0).",
    )
    ap.add_argument(
        "--other-weight",
        type=int,
        default=0,
        help="Energy assigned to 'other' transitions (default 0).",
    )
    ap.add_argument("--show", action="store_true", help="Show the matplotlib window.")
    ap.add_argument(
        "--no-mood-markers",
        action="store_true",
        help="Disable special markers for mood-change transitions.",
    )
    ap.add_argument(
        "--use-baseline-energy",
        action="store_true",
        help="Use baseline energy for transitions.",
    )

    args = ap.parse_args()

    WEIGHTS = {
        "perfect": 0,
        "boost+": 1,
        "boost++": 2,
        "boost+++": 3,
        "drop-": -1,
        "drop--": -2,
        "drop---": -3,
        "mood": args.mood_weight,
        "other": args.other_weight,
    }

    df = read_rekordbox_tsv(args.tsv)
    profile = compute_energy_profile(
        df,
        WEIGHTS,
        start_energy=args.start_energy,
        use_baseline_energy=args.use_baseline_energy,
    )

    if args.csv_out:
        profile.to_csv(args.csv_out, index=False)

    title = "Energy variation across the set (Camelot-key based)"
    plot_energy(
        profile,
        title,
        show=args.show,
        out_path=args.plot_out,
        mark_mood=not args.no_mood_markers,
    )


if __name__ == "__main__":
    main()
