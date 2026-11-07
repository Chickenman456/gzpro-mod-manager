const state = { status: null, busy: false, changelogLoaded: false };

const byId = (id) => document.getElementById(id);

 
function selectTab(name) {
  for (const page of ["mods", "changelog", "troubleshooting"]) {
    const active = name === page;
    byId("tab-" + page).classList.toggle("active", active);
    byId("tab-" + page).setAttribute("aria-selected", String(active));
    byId("page-" + page).hidden = !active;
  }
  if (name === "changelog" && !state.changelogLoaded) loadChangelog();
}

function formatCommitDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

async function loadChangelog() {
  const status = byId("changelog-status");
  const list = byId("changelog-list");
  status.textContent = "Loading recent commits from GitHub…";
  status.classList.remove("error");
  byId("refresh-changelog").disabled = true;
  try {
    const response = await fetch("https://api.github.com/repos/Chickenman456/gzpro-mod-manager/commits?per_page=25", {
      headers: { "Accept": "application/vnd.github+json" }
    });
    if (!response.ok) throw new Error(response.status === 403 ? "GitHub API rate limit reached. Try again later." : `GitHub returned HTTP ${response.status}.`);
    const commits = await response.json();
    if (!Array.isArray(commits)) throw new Error("GitHub returned an unexpected response.");
    list.innerHTML = commits.map((commit) => {
      const message = String(commit.commit?.message || "Commit").split("\n")[0];
      const author = commit.commit?.author?.name || commit.author?.login || "Unknown author";
      const date = formatCommitDate(commit.commit?.author?.date || commit.commit?.committer?.date || "");
      const shortHash = String(commit.sha || "").slice(0, 7);
      const url = commit.html_url || "https://github.com/Chickenman456/gzpro-mod-manager/commits/master";
      return `<article class="changelog-entry">
        <div class="changelog-entry-heading"><strong>${escapeHtml(message)}</strong><a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(shortHash)}</a></div>
        <span>${escapeHtml(date)} · ${escapeHtml(author)}</span>
      </article>`;
    }).join("");
    status.textContent = `Showing ${commits.length} recent commits from master.`;
    state.changelogLoaded = true;
  } catch (error) {
    status.textContent = `Could not load the GitHub changelog: ${error.message}`;
    status.classList.add("error");
    list.innerHTML = "";
  } finally {
    byId("refresh-changelog").disabled = false;
  }
}


let latestRelease = null;
async function checkForUpdates() {
  const button = byId("check-updates");
  const panel = byId("update-panel");
  const title = byId("update-title");
  const details = byId("update-details");
  const install = byId("install-update");
  button.disabled = true;
  panel.hidden = false;
  install.hidden = true;
  byId("update-spinner").hidden = false;
  title.textContent = "Checking GitHub for updates…";
  details.textContent = "";
  try {
    latestRelease = await requestJson("/api/update/check");
    byId("release-link").href = latestRelease.release_url;
    if (!latestRelease.update_available) {
      title.textContent = `You are up to date (v${latestRelease.current_version}).`;
      details.textContent = "No update is currently available.";
    } else {
      title.textContent = `Version ${latestRelease.latest_version} is available.`;
      details.textContent = latestRelease.can_install
        ? `You have v${latestRelease.current_version}. The updater downloads the official GitHub release, verifies its SHA-256 digest when provided, then restarts the manager. Your mod library and settings are stored separately and will be preserved.`
        : `You have v${latestRelease.current_version}. Download the latest Windows executable from the release page; in-app installation is available in the packaged Windows app.`;
      install.hidden = !latestRelease.can_install;
    }
  } catch (error) {
    title.textContent = "Could not check for updates.";
    details.textContent = error.message;
  } finally {
    byId("update-spinner").hidden = true;
    button.disabled = false;
  }
}

async function installUpdate() {
  if (!latestRelease?.update_available) return;
  if (!window.confirm(`Download and install GZPro Mod Manager v${latestRelease.latest_version}? The application will close and reopen when the update is ready.`)) return;
  byId("install-update").disabled = true;
  byId("update-spinner").hidden = false;
  byId("update-title").textContent = "Downloading and preparing update…";
  byId("update-details").textContent = "Keep this window open until the download has been prepared.";
  try {
    const result = await requestJson("/api/update/install", { method: "POST" });
    byId("update-title").textContent = `Installing v${result.version}…`;
    byId("update-details").textContent = "The manager will close, install the update, and reopen. If the new version cannot start, the updater will restore the previous executable. The update log is saved in %LOCALAPPDATA%\\GZProModManager\\updates\\update-install.log.";
  } catch (error) {
    byId("update-spinner").hidden = true;
    byId("update-title").textContent = "Update could not be installed.";
    byId("update-details").textContent = error.message;
    byId("install-update").disabled = false;
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[character]);
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function explainError(message) {
  const value = String(message || "").toLowerCase();
  const guidance = [
    [/unsupported file or folder|unsupported file in the package|unsupported game paks path/, "The ZIP contains a file or folder layout the manager does not recognize.", "Use the mod's original ZIP. UE4SS packages must keep the GZPro/Binaries/Win64/dwmapi.dll and GZPro/Binaries/Win64/ue4ss folder paths. Do not rename or flatten the folders."],
    [/not a valid \.zip|upload a single \.zip|selected \.zip package is empty/, "The selected file is not a complete ZIP archive.", "Download the ZIP again from the mod page. Do not extract it before importing."],
    [/must contain the cooked mod's \.utoc and \.ucas|without a container|no supported mod files/, "The package is missing the cooked game files or a recognized UE4SS mod folder.", "Check the mod's installation instructions. A cooked asset mod needs matching .utoc and .ucas files; a UE4SS mod needs its files under the ue4ss folder."],
    [/already exists in|already present but differs|not overwritten|does not match/, "A file with that name is already in the game folder, and its contents do not match this package.", "The manager stopped to protect the existing file. Check whether another mod or UE4SS installation already uses it. Do not overwrite DLLs manually unless you know which version is required."],
    [/close the game before|game must be closed|game is running/, "The game is currently running. Windows can lock files while the game is open.", "Exit the game completely, wait a few seconds, then retry the import or action."],
    [/game folder does not exist|could not find .* in that game folder|paks folder is missing|game installation/, "The selected folder is not the game's valid installation directory.", "Select the folder that contains the GZPro folder, not GZPro itself or the Binaries folder."],
    [/permission|access is denied|winerror 5/, "Windows is preventing the manager from writing to the game folder or its backup folder.", "Close the game and other mod tools. Check that the game folder and %LOCALAPPDATA%\\GZProModManager are writable. Avoid installing the manager inside a protected Windows folder."],
    [/backup.*missing|backup.*integrity|recovery manifest/, "The manager cannot verify the saved backup it needs to safely change this mod.", "Do not delete the remaining files manually. Keep the current game files in place and use Report a bug with the full error details."],
    [/ue4ss.*not detected|ue4ss is not installed/, "The game does not have the UE4SS loader files in the expected Win64 locations.", "This game needs a UE5.8-compatible experimental UE4SS build. Its dwmapi.dll belongs beside GZProSteam-Win64-Shipping.exe, and its ue4ss folder belongs beside that DLL."],
    [/could not check github|update.*failed|download.*failed/, "The updater could not complete its connection or file replacement.", "Check your internet connection, close other manager instances, and retry. If it still fails, include update-install.log when reporting the issue."]
  ];
  const match = guidance.find(([pattern]) => pattern.test(value));
  return match ? { cause: match[1], fix: match[2] } : {
    cause: "The manager stopped because an operation could not be completed safely.",
    fix: "Refresh the mod list and retry once. If the same error returns, use Copy error details and Report a bug so the exact cause can be identified."
  };
}

function setMessage(message, isError = false) {
  const element = byId("action-message");
  element.textContent = message;
  element.classList.toggle("error", isError);
  const help = byId("error-help");
  if (!isError || !help) {
    if (help) help.hidden = true;
    return;
  }
  const guidance = explainError(message);
  byId("error-cause").textContent = guidance.cause;
  byId("error-fix").textContent = guidance.fix;
  help.hidden = false;
  byId("copy-error").onclick = () => {
    const details = message + "\n\nCause: " + guidance.cause + "\nSuggested fix: " + guidance.fix;
    navigator.clipboard?.writeText(details).then(
      () => { byId("copy-error").textContent = "Copied"; },
      () => { byId("copy-error").textContent = "Copy unavailable"; }
    );
  };
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status}).`);
  return result;
}

function renderStatus(status) {
  state.status = status;
  byId("game-dir").value = status.game_dir;
  byId("compatibility-warning").innerHTML = status.ue4ss_installed
    ? "UE4SS is installed. Blueprint mods can load through BPModLoaderMod. Asset override mods do not need UE4SS."
    : 'UE4SS is only required for Blueprint mods. Asset override mods (music, textures, models) work without it. <a href="https://github.com/UE4SS-RE/RE-UE4SS/releases" target="_blank" rel="noopener noreferrer">Download UE4SS</a> for Blueprint mods.';

  const installStatus = byId("install-status");
  if (!status.game_valid) {
    installStatus.textContent = "Game install not found. Enter the folder that contains GZPro\\Binaries and GZPro\\Content.";
    installStatus.classList.add("error");
  } else if (status.game_check_error) {
    installStatus.textContent = status.game_check_error;
    installStatus.classList.add("error");
  } else if (status.game_running) {
    installStatus.textContent = "The game is running. Close it before enabling, disabling, or importing a mod.";
    installStatus.classList.add("error");
  } else {
    const loader = status.ue4ss_installed ? "UE4SS detected" : "UE4SS not installed · asset overrides still work";
    installStatus.textContent = `Ready · ${loader} · ${status.logic_mods_exists ? "LogicMods folder found" : "LogicMods folder will be created on import"}`;
    installStatus.classList.remove("error");
  }

  const profiles = status.profiles || [];
  const profileSelect = byId("profile-select");
  const selectedProfile = profileSelect.value;
  profileSelect.innerHTML = profiles.length
    ? profiles.map((profile) => `<option value="${escapeHtml(profile.id)}">${escapeHtml(profile.name)}${profile.id === status.active_profile_id ? " · Active" : ""}</option>`).join("")
    : '<option value="">No profiles saved</option>';
  if (profiles.some((profile) => profile.id === selectedProfile)) profileSelect.value = selectedProfile;
  else if (status.active_profile_id) profileSelect.value = status.active_profile_id;
  byId("apply-profile").disabled = state.busy || !profileSelect.value || !status.game_valid || status.game_running || Boolean(status.game_check_error);
  byId("delete-profile").disabled = state.busy || !profileSelect.value;
  byId("open-mod-location").disabled = !status.game_valid;
  byId("launch-game").disabled = !status.game_valid || status.game_running || Boolean(status.game_check_error);
  byId("profile-status").textContent = status.active_profile_id
    ? `Active profile: ${profiles.find((profile) => profile.id === status.active_profile_id)?.name || "Saved profile"}`
    : "No profile is currently active. Saving captures the mods that are enabled right now.";
  const modsElement = byId("mods");
  if (!status.mods.length) {
    modsElement.innerHTML = `<p class="empty-state">No mods are managed yet.</p>`;
  } else {
    modsElement.innerHTML = status.mods.map((mod) => {
      const stateLabel = mod.conflict ? "File changed outside the manager" : (mod.enabled ? "Enabled" : "Disabled");
      const disabled = state.busy || !status.game_valid || status.game_running || status.game_check_error || mod.conflict || !mod.backup_available;
      const deleteDisabled = state.busy || !status.game_valid || status.game_running || status.game_check_error || !mod.backup_available;
      const kindLabel = mod.requires_ue4ss ? "UE4SS · Character / skin mod" : (mod.kind === "blueprint" ? "Blueprint · LogicMods" : "Asset override · Paks");
      const thumbnail = mod.thumbnail ? `<img class="mod-thumbnail" src="${escapeHtml(mod.thumbnail)}" alt="${escapeHtml(mod.stem || "Mod")} thumbnail">` : "";
      const description = mod.description ? `<p class="mod-description">${escapeHtml(mod.description)}</p>` : "";
      return `<article class="mod-card ${mod.enabled ? "" : "mod-card-disabled"}">
        <label class="mod-toggle" title="${mod.enabled ? "Disable" : "Enable"} ${escapeHtml(mod.stem || mod.filename)}"><input type="checkbox" data-mod-toggle="${escapeHtml(mod.id)}" ${mod.enabled ? "checked" : ""} ${disabled ? "disabled" : ""} aria-label="${mod.enabled ? "Disable" : "Enable"} ${escapeHtml(mod.stem || mod.filename)}"><span class="mod-toggle-mark" aria-hidden="true"></span></label>
        ${thumbnail}
        <div class="mod-info">
          <button class="mod-title-button" type="button" data-rename-mod="${escapeHtml(mod.id)}" title="Click to rename this mod">${escapeHtml(mod.display_name || mod.stem || mod.filename)}</button>
          <span>${formatSize(mod.size)} · ${mod.file_count || 1} files · ${kindLabel} · ${escapeHtml(stateLabel)}</span>
          ${description}
          <small>SHA-256 ${escapeHtml(mod.sha256.slice(0, 16))}… · backup ${mod.backup_available ? "available" : "missing"}</small>
        </div>
        <div class="mod-actions">
          <button class="button button-danger" data-delete-mod="${escapeHtml(mod.id)}" ${deleteDisabled ? "disabled" : ""}>Delete</button>
        </div>
      </article>`;
    }).join("");
    modsElement.querySelectorAll("input[data-mod-toggle]").forEach((checkbox) => {
      checkbox.addEventListener("change", () => changeModState(checkbox.dataset.modToggle, checkbox.checked ? "enable" : "disable"));
    });
    modsElement.querySelectorAll("button[data-rename-mod]").forEach((button) => {
      button.addEventListener("click", async () => {
        const mod = state.status.mods.find((item) => item.id === button.dataset.renameMod);
        if (!mod) return;
        const currentName = mod.display_name || mod.stem || mod.filename;
        const nextName = window.prompt("Rename this mod:", currentName);
        if (nextName === null || nextName.trim() === "" || nextName.trim() === currentName) return;
        try {
          await requestJson("/api/mods/" + button.dataset.renameMod + "/rename", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ display_name: nextName.trim() }) });
          mod.display_name = nextName.trim();
          renderStatus(state.status);
          setMessage("Mod renamed to \"" + nextName.trim() + "\".");
        } catch (error) { setMessage("Could not rename mod: " + error.message, true); }
      });
    });
    modsElement.querySelectorAll("button[data-delete-mod]").forEach((button) => {
      button.addEventListener("click", () => deleteManagedMod(button.dataset.deleteMod));
    });
  }

  const unmanaged = byId("unmanaged");
  unmanaged.innerHTML = status.unmanaged_mods.length
    ? `<h3>Manually installed mods</h3><p class="muted">Adopt a detected mod to create a verified backup and manage its enabled state here. Adoption does not move or alter the installed files.</p>
      <div class="mod-list">${status.unmanaged_mods.map((mod) => {
        const fileNames = mod.files.map(escapeHtml).join(", ");
        const disabled = state.busy || !status.game_valid || status.game_running || status.game_check_error;
        return `<article class="mod-card">
          <div class="mod-info">
            <strong>${escapeHtml(mod.stem)}</strong>
            <span>${escapeHtml(mod.location)} · ${formatSize(mod.size)} · ${mod.files.length} files</span>
            <small>${fileNames}</small>
          </div>
          <button class="button button-primary" data-adopt-location="${escapeHtml(mod.location)}" data-adopt-stem="${escapeHtml(mod.stem)}" ${disabled ? "disabled" : ""}>Adopt</button>
        </article>`;
      }).join("")}</div>`
    : "";
  unmanaged.querySelectorAll("button[data-adopt-location]").forEach((button) => {
    button.addEventListener("click", () => adoptUnmanagedMod(button.dataset.adoptLocation, button.dataset.adoptStem));
  });
  byId("mod-file").disabled = state.busy || !status.game_valid || Boolean(status.game_running) || Boolean(status.game_check_error);
  byId("refresh").disabled = state.busy;
}

async function refreshStatus() {
  try {
    renderStatus(await requestJson("/api/status"));
  } catch (error) {
    setMessage(`Could not read manager status: ${error.message}`, true);
  }
}

function uploadFile(file, kind, replaceModId = null) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", "/api/mods");
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.setRequestHeader("X-Mod-Name", encodeURIComponent(file.name));
    request.setRequestHeader("X-Mod-Kind", kind);
    if (replaceModId) request.setRequestHeader("X-Replace-Mod-Id", replaceModId);
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        byId("upload-progress").value = Math.round((event.loaded / event.total) * 100);
      }
    };
    request.onload = () => {
      let result;
      try {
        result = JSON.parse(request.responseText);
      } catch {
        reject(new Error("The manager returned an invalid response."));
        return;
      }
      if (request.status < 200 || request.status >= 300) {
        const error = new Error(result.error || `Upload failed (${request.status}).`);
        const match = /^MOD_REPLACE_REQUIRED:([0-9a-f]{32}):(.+)$/i.exec(error.message);
        if (match) { error.replaceModId = match[1]; error.replaceStem = match[2]; error.message = match[2] + " is already installed."; }
        reject(error);
        return;
      }
      resolve(result);
    };
    request.onerror = () => reject(new Error("Upload failed. Check that the manager is still running."));
    request.send(file);
  });
}

async function importFile(file) {
  if (!file) return;
  if (!file.name.toLocaleLowerCase().endsWith(".zip")) {
    setMessage("Mods install from a .zip package holding the cooked .utoc and .ucas files. A raw WAV or loose Unreal asset cannot be dropped in directly.", true);
    return;
  }
  if (!state.status?.game_valid || state.status.game_running || state.status.game_check_error) {
    setMessage("The game install must be valid and the game must be closed before importing.", true);
    return;
  }
  const kind = document.querySelector('input[name="mod-kind"]:checked')?.value || "override";
  const target = kind === "blueprint" ? "the game's LogicMods folder" : "the game's Paks folder as GZPro-" + file.name.replace(/\.zip$/i, "") + ".*";
  if (!window.confirm(
    `Deploy ${file.name} into ${target}? The manager keeps a hash-checked backup and never overwrites existing files.${kind === "blueprint" ? " Loading it in-game requires UE4SS." : ""}`
  )) return;
  state.busy = true;
  byId("upload-progress").hidden = false;
  byId("upload-progress").value = 0;
  setMessage(`Importing ${file.name}…`);
  renderStatus(state.status);
  try {
    let mod;
    try { mod = await uploadFile(file, kind); }
    catch (error) {
      if (!error.replaceModId) throw error;
      const replace = window.confirm("A mod named \"" + error.replaceStem + "\" is already in your library. Replace its installed version with this ZIP? The existing mod and profile references will be kept. If replacement fails, the previous version will be restored.");
      if (!replace) { setMessage("Existing mod was left unchanged."); return; }
      mod = await uploadFile(file, kind, error.replaceModId);
    }
    const note = mod.requires_ue4ss
      ? "UE4SS files were included or detected. The mod will load the next time the game starts."
      : (mod.kind === "blueprint"
        ? "UE4SS loads it the next time the game starts."
        : "It overrides the game's original assets the next time the game starts.");
    setMessage(`${mod.stem || mod.filename} was deployed (${mod.file_count} files). ${note}`);
  } catch (error) {
    setMessage(`Import failed: ${error.message}`, true);
  } finally {
    state.busy = false;
    byId("upload-progress").hidden = true;
    byId("mod-file").value = "";
    await refreshStatus();
  }
}

async function adoptUnmanagedMod(location, stem) {
  if (!location || !stem) return;
  if (!window.confirm(
    `Adopt ${stem} from ${location}? The manager will create a backup and register the existing files without moving or changing them. You can then disable or re-enable the mod here.`
  )) return;
  state.busy = true;
  setMessage(`Backing up ${stem}…`);
  renderStatus(state.status);
  try {
    await requestJson("/api/mods/adopt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ location, stem })
    });
    setMessage(`${stem} was adopted. Its original files are still installed and can now be managed here.`);
  } catch (error) {
    setMessage(`Could not adopt ${stem}: ${error.message}`, true);
  } finally {
    state.busy = false;
    await refreshStatus();
  }
}

async function deleteManagedMod(modId) {
  const mod = state.status?.mods.find((item) => item.id === modId);
  if (!mod) return;
  if (!window.confirm(`Permanently delete ${mod.stem || mod.filename}? This removes its verified installed files, its manager backup, and its library entry. Files changed outside the manager will be left untouched and will block deletion.`)) return;
  state.busy = true;
  setMessage(`Deleting ${mod.stem || mod.filename}…`);
  renderStatus(state.status);
  try {
    await requestJson(`/api/mods/${encodeURIComponent(modId)}/delete`, { method: "POST" });
    setMessage(`${mod.stem || mod.filename} was deleted from the manager.`);
  } catch (error) {
    setMessage(`Could not delete mod: ${error.message}`, true);
  } finally {
    state.busy = false;
    await refreshStatus();
  }
}
async function changeModState(modId, action) {
  if (!modId || !["enable", "disable"].includes(action)) return;
  const verb = action === "disable" ? "disable" : "enable";
  state.busy = true;
  const selectedMod = state.status?.mods?.find((mod) => mod.id === modId);
  if (selectedMod) selectedMod.enabled = action === "enable";
  setMessage(`${verb === "disable" ? "Disabling" : "Enabling"} mod…`);
  renderStatus(state.status);
  try {
    await requestJson(`/api/mods/${encodeURIComponent(modId)}/${action}`, { method: "POST" });
    setMessage(`Mod ${verb}d. The saved backup was kept.`);
  } catch (error) {
    setMessage(`Could not ${verb} mod: ${error.message}`, true);
  } finally {
    state.busy = false;
    await refreshStatus();
  }
}

byId("profile-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = byId("profile-name");
  const name = input.value.trim();
  if (!name) return;
  try {
    await requestJson("/api/profiles", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
    input.value = "";
    setMessage(`Saved profile "${name}" from the current enabled mods.`);
    await refreshStatus();
  } catch (error) { setMessage(`Could not save profile: ${error.message}`, true); }
});
byId("apply-profile").addEventListener("click", async () => {
  const id = byId("profile-select").value;
  if (!id) return;
  if (!window.confirm("Apply this profile? All managed mods not included in it will be disabled, and its selected mods will be enabled.")) return;
  state.busy = true;
  renderStatus(state.status);
  setMessage("Applying mod profile…");
  try {
    await requestJson(`/api/profiles/${encodeURIComponent(id)}/apply`, { method: "POST" });
    setMessage("Profile applied. Mod backups were preserved.");
  } catch (error) { setMessage(`Could not apply profile: ${error.message}`, true); }
  finally { state.busy = false; await refreshStatus(); }
});
byId("delete-profile").addEventListener("click", async () => {
  const id = byId("profile-select").value;
  const profile = state.status?.profiles?.find((item) => item.id === id);
  if (!id || !profile || !window.confirm(`Delete the saved profile "${profile.name}"? This does not delete or change any mods.`)) return;
  try {
    await requestJson(`/api/profiles/${encodeURIComponent(id)}/delete`, { method: "POST" });
    setMessage(`Deleted profile "${profile.name}".`);
    await refreshStatus();
  } catch (error) { setMessage(`Could not delete profile: ${error.message}`, true); }
});
byId("profile-select").addEventListener("change", () => {
  byId("apply-profile").disabled = !byId("profile-select").value || state.busy || !state.status?.game_valid || state.status?.game_running || Boolean(state.status?.game_check_error);
  byId("delete-profile").disabled = !byId("profile-select").value || state.busy;
});
byId("open-mod-location").addEventListener("click", async () => {
  const button = byId("open-mod-location");
  button.disabled = true;
  const originalText = button.textContent;
  button.textContent = "Opening…";
  try {
    const result = await requestJson("/api/game/open-mod-location", { method: "POST" });
    setMessage("Opened mod install location: " + result.path);
  } catch (error) {
    setMessage("Could not open mod install location: " + error.message, true);
  } finally {
    button.textContent = originalText;
    button.disabled = !state.status?.game_valid;
  }
});
byId("launch-game").addEventListener("click", async () => {
  const button = byId("launch-game");
  button.disabled = true;
  button.textContent = "Launching…";
  try {
    await requestJson("/api/game/launch", { method: "POST" });
    button.textContent = "Game launched";
    setTimeout(() => { button.textContent = "Launch Game"; refreshStatus(); }, 2500);
  } catch (error) {
    setMessage(`Could not launch the game: ${error.message}`, true);
    button.textContent = "Launch Game";
    refreshStatus();
  }
});
byId("check-updates").addEventListener("click", checkForUpdates);
byId("install-update").addEventListener("click", installUpdate);
byId("tab-mods").addEventListener("click", () => selectTab("mods"));
byId("tab-changelog").addEventListener("click", () => selectTab("changelog"));
byId("tab-troubleshooting").addEventListener("click", () => selectTab("troubleshooting"));
byId("refresh-changelog").addEventListener("click", loadChangelog);

byId("mod-file").addEventListener("change", (event) => importFile(event.target.files[0]));
byId("refresh").addEventListener("click", refreshStatus);
byId("game-dir-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setMessage("Checking the selected game folder…");
  try {
    renderStatus(await requestJson("/api/game-dir", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ game_dir: byId("game-dir").value })
    }));
    setMessage("Game folder selected. No game files were changed.");
  } catch (error) {
    setMessage(`Could not select game folder: ${error.message}`, true);
  }
});

const dropZone = byId("drop-zone");
let dragDepth = 0;
window.addEventListener("dragenter", (event) => {
  if (!event.dataTransfer?.types?.includes("Files")) return;
  event.preventDefault();
  dragDepth += 1;
  document.body.classList.add("file-dragging");
  dropZone.classList.add("dragging");
});
window.addEventListener("dragover", (event) => {
  if (!event.dataTransfer?.types?.includes("Files")) return;
  event.preventDefault();
});
window.addEventListener("dragleave", (event) => {
  if (!event.dataTransfer?.types?.includes("Files")) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) {
    document.body.classList.remove("file-dragging");
    dropZone.classList.remove("dragging");
  }
});
window.addEventListener("drop", (event) => {
  if (!event.dataTransfer?.files?.length) return;
  event.preventDefault();
  dragDepth = 0;
  document.body.classList.remove("file-dragging");
  dropZone.classList.remove("dragging");
  if (event.dataTransfer.files.length > 1) {
    setMessage("Drop one mod ZIP at a time so each installation can be checked and backed up separately.", true);
    return;
  }
  importFile(event.dataTransfer.files[0]);
});

refreshStatus();
