<p align="center">
  <img src="gzprologo.png" alt="GZPro Mod Manager" width="420">
</p>

# GZPro Mod Manager

A small, local-only Windows tool for safely staging pre-cooked `.pak` mods for **Godzilla: Destroy All Monsters Melee Remastered**.

## Run

1. Install Python 3.10 or newer.
2. Double-click `run.bat`, or run `.\run.bat` from PowerShell.
3. Keep the terminal open while using the browser page. Press Ctrl+C in the terminal to stop it.
4. If the game is installed elsewhere, either enter its install folder in the page or run:

   ```powershell
   .\run.bat --game-dir "D:\SteamLibrary\steamapps\common\Godzilla Destroy All Monsters Melee Remastered"
   ```

The Windows executable opens in a frameless desktop window with a custom title bar and window controls (using WebView2 when available); if that runtime is unavailable, it falls back to the default browser. The header's **Check for updates** button checks the latest stable GitHub release and can download, verify when a release digest is published, install, and restart the executable. Mod backups and settings remain in `%LOCALAPPDATA%\\GZProModManager` and are not replaced by an update.

The launcher includes a **Changelog** tab that reads the latest commit history from this GitHub repository. An internet connection is required to load it.

The manager binds only to `127.0.0.1` and asks Windows for an available local port by default. The packaged desktop version includes pywebview; running from source can use the browser fallback.

## Mod package format (verified working)

Mods install from a single `.zip` containing the cooked IoStore container:

- `<ModName>.utoc` + `<ModName>.ucas` — required; the cooked container holding the mod's assets.
- `<ModName>.pak` — optional companion. If the zip has no `.pak`, the manager deploys a tiny verified stub `.pak` instead: this game only mounts a container when a same-named `.pak` sits next to it. The stub is parsed but never mounted, so its contents never reach the game.

Standard cooked container files must share the same mod name and sit at the zip root. Combined skin-loader packages are also supported: one skin-data folder containing the matching .utoc/.ucas (and optional .pak), plus a GZSkinLoader folder containing Scripts/main.lua and enabled.txt. The skin containers are installed under Paks\\~mods and the loader files under GZPro\\Binaries\\Win64\\ue4ss\\Mods\\GZSkinLoader. The manager enables, disables, and deletes both components together. Root-level README, LICENSE, CHANGELOG, NOTICE, and CREDITS files are ignored; other unexpected paths are rejected.

### Optional launcher metadata

- Add a root-level `INFO.txt` (UTF-8, up to 16 KB) to show its text as the mod description in the launcher.
- Add one root-level `THUMB.png`, `THUMB.jpg`, `THUMB.jpeg`, or `THUMB.webp` (up to 1 MB) to show a thumbnail beside the mod. The extension must match the image format.
- These files are read by the manager and are not deployed into the game. Both are optional. The launcher also includes a **Report a bug** link to this repository's GitHub issue form.

### Two kinds of mods

**Asset override** (default): for mods that replace existing game assets — music, textures, models. The cooked assets must live at the *same package path* as the originals (e.g. `/Game/Audio/GZ1/Streaming/MENU`). The manager deploys them as `GZPro-<ModName>.pak/.utoc/.ucas` directly in `GZPro\Content\Paks`, which gives the container the same mount priority as the game's own container so the replacement wins. **No UE4SS needed.** Verified working: the menu music was replaced with a cooked SoundWave and plays in-game.

**Blueprint mod**: for UE4SS BPModLoaderMod logic mods (a `ModActor` Blueprint at `/Game/Mods/<ModName>/ModActor`). The manager deploys them as `<ModName>.pak/.utoc/.ucas` in `GZPro\Content\Paks\LogicMods`. **Requires UE4SS** installed separately into `GZPro\Binaries\Win64` (the `dwmapi.dll` + `ue4ss` folder from an official UE4SS release); the manager detects it and warns when it is missing. [Download UE4SS from its official releases page](https://github.com/UE4SS-RE/RE-UE4SS/releases). Choose the regular non-dev release, then follow the [official installation guide](https://docs.ue4ss.com/installation-guide). Verified working: a cooked Blueprint actor mod was deployed by this manager and spawned in-game.

## Current scope and safety

- Accepts only `.zip` mod packages in the format above, ignoring common root-level documentation files; it does not inspect the cooked assets or confirm they were cooked for a compatible engine. A raw WAV or loose `.uasset` cannot be dropped in directly — cook it into a container first.
- Keeps a SHA-256-checked backup of every package under `%LOCALAPPDATA%\GZProModManager`.
- Can disable a managed mod only when every deployed file still matches its saved hash. Re-enabling refuses to overwrite a same-named file that differs.
- Can permanently delete a managed mod and its backup. Deletion first verifies the backup and every installed file; if a file was changed outside the manager, it is left untouched and deletion is blocked.
- Refuses enable/disable/import operations while `GZProSteam-Win64-Shipping.exe` is running.
- Never edits or replaces the game's original `GZPro-Windows.*` or `global.*` archives, and refuses mod names that would collide with them. It does not install or enable UE4SS.
- Detects loose `.pak`, `.utoc`, and `.ucas` files in `Paks` and `LogicMods`, groups matching names, and lets you adopt them. Adoption creates a verified backup without changing the installed files; adopted mods can then be disabled and re-enabled. Disable refuses to remove any file whose hash no longer matches the backup.

**Important:** only install mods you trust. The manager verifies file integrity, not mod behavior.

## Tests

Run the standard-library tests with:

```powershell
python -m unittest discover -s tests
```
