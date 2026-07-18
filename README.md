# anomaly

Tools for building and mixing DJ sets: syncing tracks from SoundCloud, analyzing harmonic-key energy transitions, and suggesting a track order for a set that balances an energy shape against in-key mixing.

## Components

- **`sc_sync.py`** — downloads tracks from a list of SoundCloud playlists via `yt-dlp`, deduplicates tracks that appear in multiple playlists via symlinks, and writes a report after every run.
- **`plot_set_energy.py`** — given a Rekordbox TSV/TXT export, classifies every Camelot-key transition in a set (perfect / boost / drop / mood / other, per a fixed harmonic-mixing chart) and plots the resulting cumulative energy curve.
- **`set_builder.py`** — given a Rekordbox TSV/TXT export, suggests a track order for a set: picks the best-fitting subset of tracks (if the pool is larger than the target set size) and orders them to follow a target energy shape (default: slow start → rise → plateau → rise again) while preferring harmonically compatible key transitions (reusing `plot_set_energy.py`'s classification rules). Supports pinning specific tracks to specific positions, a strict "never allow an off-key transition" mode, and exporting the result as a Rekordbox-importable XML or a folder of ordered symlinks for dragging into a new Rekordbox playlist.

## Roadmap

See [`docs/PLAN.md`](docs/PLAN.md) for the current plan: adding named energy-phase groups (opening / first peak / valley / second peak / closing) that constrain which tracks the algorithm can pick for each segment of the set, and a small local web app for browsing a Rekordbox library, tagging tracks into those phase groups visually, and building a set with one click.

## Layout

This project lives in a subfolder of a much larger personal working directory that also contains audio files and Rekordbox library exports — those are intentionally kept out of this repo. By default, `sc_sync.py` and `set_builder.py` expect a `SoundCloud-LQ/` folder as a sibling of this `anomaly/` folder (i.e. one level up), overridable via `--base-dir` / `--library-base-dir`.

## Requirements

See `requirements.txt`. Also requires [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) on `PATH` for `sc_sync.py`.

## Usage

```bash
# Run from the parent working directory (so relative TSV/session paths resolve as expected)
python3 anomaly/sc_sync.py

python3 anomaly/plot_set_energy.py "Session Notes/set.txt" --plot-out energy.png

python3 anomaly/set_builder.py "Session Notes/set.txt" --num-tracks 20 \
    --pin "Opening Track=first" --rekordbox-xml built.xml
```
