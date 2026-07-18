const PHASES = ["opening", "first_boost", "plateau", "second_boost", "closing"];
const PHASE_LABELS = {
  opening: "Opening",
  first_boost: "First Boost",
  plateau: "Plateau",
  second_boost: "Second Boost",
  closing: "Closing",
};

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

function getPlaylistPath() {
  return new URLSearchParams(window.location.search).get("playlist") || "";
}

function fmtDuration(seconds) {
  if (!seconds) return "";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function cardHtml(track) {
  const title = escapeHtml(track.title || "");
  const artist = track.artist ? escapeHtml(track.artist) : "";
  const options = ["", ...PHASES]
    .map((p) => {
      const label = p ? PHASE_LABELS[p] : "— unassigned —";
      const selected = (track.phase || "") === p ? "selected" : "";
      return `<option value="${p}" ${selected}>${label}</option>`;
    })
    .join("");

  return `
    <div class="card" draggable="true" data-track-id="${track.track_id}">
      <div class="title">${title}</div>
      ${artist ? `<div class="artist">${artist}</div>` : ""}
      <div class="meta">
        <span>${track.key || "?"}</span>
        <span>En ${track.energy != null ? track.energy : "?"}</span>
        <span>${track.bpm ? Math.round(track.bpm) : "?"} bpm</span>
        <span>${fmtDuration(track.duration_s)}</span>
      </div>
      <select data-track-id="${track.track_id}">${options}</select>
    </div>`;
}

async function setPhase(playlistPath, trackId, phase) {
  const res = await fetch(
    `/api/playlists/${encodeURIComponent(playlistPath)}/tags/${encodeURIComponent(trackId)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phase: phase || null }),
    }
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
}

async function loadTagging() {
  const playlistPath = getPlaylistPath();
  const status = document.getElementById("status");
  document.getElementById("playlist-name").textContent = playlistPath;
  document.getElementById("build-btn").addEventListener("click", () => {
    window.location.href = `/build.html?playlist=${encodeURIComponent(playlistPath)}`;
  });

  if (!playlistPath) {
    status.innerHTML = `<div class="note error">No playlist selected. Go back to the <a href="/index.html">playlist list</a>.</div>`;
    return;
  }

  status.textContent = "Loading tracks…";
  let tracks;
  try {
    const res = await fetch(`/api/playlists/${encodeURIComponent(playlistPath)}/tracks`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    tracks = await res.json();
  } catch (err) {
    status.innerHTML = `<div class="note error">Could not load tracks: ${escapeHtml(err.message)}</div>`;
    return;
  }
  status.textContent = "";

  const board = document.getElementById("board");
  board.innerHTML = PHASES.map(
    (p) => `<div class="column" data-phase="${p}"><h3>${PHASE_LABELS[p]} <span class="playlist-count" data-count="${p}"></span></h3><div class="cards"></div></div>`
  ).join("");

  function render() {
    const unassignedCards = document.getElementById("unassigned-cards");
    unassignedCards.innerHTML = tracks.filter((t) => !t.phase).map(cardHtml).join("");

    for (const phase of PHASES) {
      const col = board.querySelector(`.column[data-phase="${phase}"] .cards`);
      const inPhase = tracks.filter((t) => t.phase === phase);
      col.innerHTML = inPhase.map(cardHtml).join("");
      board.querySelector(`.column[data-phase="${phase}"] [data-count="${phase}"]`).textContent = `(${inPhase.length})`;
    }

    attachInteractions();
  }

  async function updateTrackPhase(trackId, phase) {
    const track = tracks.find((t) => t.track_id === trackId);
    if (!track) return;
    const previous = track.phase;
    track.phase = phase || null;
    render();
    try {
      await setPhase(playlistPath, trackId, phase);
    } catch (err) {
      track.phase = previous;
      render();
      status.innerHTML = `<div class="note error">Could not save tag: ${escapeHtml(err.message)}</div>`;
    }
  }

  function attachInteractions() {
    document.querySelectorAll(".card").forEach((card) => {
      card.addEventListener("dragstart", () => card.classList.add("dragging"));
      card.addEventListener("dragend", () => card.classList.remove("dragging"));
    });

    document.querySelectorAll("select[data-track-id]").forEach((sel) => {
      sel.addEventListener("click", (e) => e.stopPropagation());
      sel.addEventListener("change", () => updateTrackPhase(sel.dataset.trackId, sel.value));
    });

    document.querySelectorAll(".column .cards, .unassigned-tray .cards").forEach((dropZone) => {
      dropZone.addEventListener("dragover", (e) => {
        e.preventDefault();
        dropZone.closest(".column")?.classList.add("dragover");
      });
      dropZone.addEventListener("dragleave", () => {
        dropZone.closest(".column")?.classList.remove("dragover");
      });
      dropZone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropZone.closest(".column")?.classList.remove("dragover");
        const dragging = document.querySelector(".card.dragging");
        if (!dragging) return;
        const trackId = dragging.dataset.trackId;
        const column = dropZone.closest(".column");
        const phase = column ? column.dataset.phase : null;
        updateTrackPhase(trackId, phase);
      });
    });
  }

  render();
}

loadTagging();
