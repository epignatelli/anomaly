function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

async function loadSettings() {
  const input = document.getElementById("xml-path");
  try {
    const res = await fetch("/api/settings");
    const data = await res.json();
    input.value = data.rekordbox_xml_path;
    if (!data.exists) {
      document.getElementById("settings-status").innerHTML =
        `<div class="note error">This path does not exist on disk.</div>`;
    }
  } catch (err) {
    // Non-fatal - the input just stays blank if settings can't be loaded.
  }
}

async function saveSettings() {
  const input = document.getElementById("xml-path");
  const status = document.getElementById("settings-status");
  status.textContent = "Saving…";
  try {
    const res = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rekordbox_xml_path: input.value.trim() }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    status.innerHTML = `<div class="note">Saved. Reloading playlists…</div>`;
    await loadPlaylists();
    status.textContent = "";
  } catch (err) {
    status.innerHTML = `<div class="note error">${escapeHtml(err.message)}</div>`;
  }
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

document.getElementById("save-settings").addEventListener("click", saveSettings);
loadSettings();
loadPlaylists();
