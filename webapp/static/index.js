function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

async function loadPlaylists() {
  const status = document.getElementById("status");
  const table = document.getElementById("playlist-table");
  const body = document.getElementById("playlist-body");

  status.textContent = "Loading playlists…";
  let playlists;
  try {
    const res = await fetch("/api/playlists");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    playlists = await res.json();
  } catch (err) {
    status.innerHTML = `<div class="note error">Could not load playlists: ${escapeHtml(err.message)}. Is the Rekordbox XML export path configured correctly?</div>`;
    return;
  }

  status.textContent = "";
  table.style.display = "";
  body.innerHTML = playlists
    .map(
      (p) => `
    <tr class="playlist-row" data-path="${escapeHtml(p.path)}">
      <td>${escapeHtml(p.path)}</td>
      <td class="playlist-count">${p.count}</td>
    </tr>`
    )
    .join("");

  body.querySelectorAll(".playlist-row").forEach((row) => {
    row.addEventListener("click", () => {
      const path = row.dataset.path;
      window.location.href = `/tagging.html?playlist=${encodeURIComponent(path)}`;
    });
  });
}

loadPlaylists();
