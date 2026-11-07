from __future__ import annotations

import argparse
import ctypes
import base64
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import uuid
import urllib.error
import urllib.request
import webbrowser
import zipfile
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlsplit


DEFAULT_GAME_DIR = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common"
    r"\Godzilla Destroy All Monsters Melee Remastered"
)
GAME_EXECUTABLE = Path("GZPro") / "Binaries" / "Win64" / "GZProSteam-Win64-Shipping.exe"
PAKS_DIRECTORY = Path("GZPro") / "Content" / "Paks"
LOGIC_MODS_DIRECTORY = "LogicMods"
MAX_MOD_SIZE = 8 * 1024 * 1024 * 1024
WEB_DIRECTORY = Path(__file__).resolve().parent / "web"
APP_VERSION = "1.12.2"
GITHUB_RELEASE_API = "https://api.github.com/repos/Chickenman456/gzpro-mod-manager/releases/latest"
MANAGER_VERSION = 2
MOD_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
MOD_MEMBER_SUFFIXES = frozenset({".pak", ".utoc", ".ucas"})
MOD_THUMBNAIL_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
MAX_INFO_SIZE = 16 * 1024
MAX_THUMBNAIL_SIZE = 1024 * 1024
REQUIRED_MEMBER_SUFFIXES = frozenset({".utoc", ".ucas"})
MOD_KIND_BLUEPRINT = "blueprint"
MOD_KIND_OVERRIDE = "override"
MOD_KINDS = frozenset({MOD_KIND_BLUEPRINT, MOD_KIND_OVERRIDE})
OVERRIDE_PREFIX = "GZPro-"
# The game's own container files; an override mod must never collide with them.
GAME_CONTAINER_STEMS = frozenset({"windows", "gzpro-windows", "global"})
UE4SS_RELATIVE_FILES = (
    Path("dwmapi.dll"),
    Path("ue4ss") / "UE4SS.dll",
)
GAME_CHECK_LOCK = threading.Lock()
STATUS_PROGRESS_LOCK = threading.Lock()
STATUS_PROGRESS: dict[str, object] = {"active": False, "stage": "Idle"}


def _set_status_progress(stage: str, active: bool = True) -> None:
    with STATUS_PROGRESS_LOCK:
        STATUS_PROGRESS.update({"active": active, "stage": stage})


def _get_status_progress() -> dict[str, object]:
    with STATUS_PROGRESS_LOCK:
        return dict(STATUS_PROGRESS)


def _run_check_with_timeout(check, timeout: float = 8.0) -> tuple[bool, str | None]:
    """Run a potentially blocking filesystem check without freezing the status API."""
    finished = threading.Event()
    outcome: list[tuple[bool, str | None]] = []

    def run() -> None:
        try:
            outcome.append((bool(check()), None))
        except Exception as error:
            outcome.append((False, str(error)))
        finally:
            finished.set()

    threading.Thread(target=run, daemon=True, name="GZProStatusPathCheck").start()
    if not finished.wait(timeout):
        return False, f"Timed out after {timeout:g} seconds while checking the game installation."
    return outcome[0]


# A minimal but valid .pak (486 bytes, produced by UnrealPak 5.8) holding one
# inert marker file. This game only mounts a .utoc/.ucas IoStore container when
# a same-named .pak sits next to it; the legacy .pak is parsed but never
# mounted, so its contents never reach the game. The manager deploys this stub
# as <ModName>.pak whenever a mod package has no companion .pak of its own.
_STUB_PAK = base64.b64decode(
    "AAAAAAAAAAAGAAAAAAAAAAYAAAAAAAAAAAAAAJTqMkPah4UlKZxM9dTlHJSc1sSnAAAAAABzdHViDQooAAAALi4vLi4vLi4vR1pQcm8vQ29udGVudC9Nb2RzL0daTW9kUHJvYmUvAAEAAAC8C0v6AAAAAAEAAADXAAAAAAAAABQAAAAAAAAA+WKOlGmc/VVkXrxM/PPJC7vK7gwBAAAA6wAAAAAAAAAeAAAAAAAAADNZzzWVyDSF9UqN0Tc6Bzo8d5VcDAAAAAAAAOAAAAAABgAAAAAAAAABAAAAJxDxI9Zl2ZMAAAAAAAAAAAEAAAACAAAALwABAAAACAAAAHN0dWIudHh0AAAAAAAAAAAAAAAAAAAAAAAAAAAA4RJvWgwAAAA7AAAAAAAAAJwAAAAAAAAALujUD6ZeiDXMMCmYrZYHocxSgQ0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
)


class ManagerError(Exception):
    status_code = 400


class ConflictError(ManagerError):
    status_code = 409


class StateError(ManagerError):
    status_code = 500


def _default_state_base() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "GZProModManager"
    return Path.home() / ".gzpro-mod-manager"


def _canonical_game_dir(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _safe_mod_stem(stem: str) -> str:
    if (
        not stem
        or stem != stem.strip()
        or len(stem) > 120
        or stem.startswith(".")
        or any(ord(character) < 32 for character in stem)
        or any(character in '<>:"|?*/\\' for character in stem)
        or stem.endswith((".", " "))
    ):
        raise ManagerError(
            "Mod file names must be plain names without folders or Windows-reserved characters."
        )
    if stem.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(
        r"(?:COM|LPT)[1-9]", stem, re.IGNORECASE
    ):
        raise ManagerError("That file name is reserved by Windows.")
    return stem


def _safe_mod_filename(filename: str) -> str:
    if (
        not filename
        or filename != filename.strip()
        or len(filename) > 128
        or filename.startswith(".")
        or "/" in filename
        or "\\" in filename
        or any(ord(character) < 32 for character in filename)
        or any(character in '<>:"|?*' for character in filename)
        or filename.endswith((".", " "))
    ):
        raise ManagerError("Use a plain file name without folders or Windows-reserved characters.")
    if Path(filename).suffix.casefold() != ".zip":
        raise ManagerError(
            "Mods install from a .zip package containing the cooked mod's "
            ".utoc and .ucas files (a companion .pak is optional). Loose WAV, "
            ".uasset, or single .pak files are not installable this way."
        )
    _safe_mod_stem(Path(filename).stem)
    return filename


def _is_package_document(name: str) -> bool:
    """Allow common documentation files alongside the cooked mod containers."""
    if Path(name).name != name or "/" in name or chr(92) in name:
        return False
    return re.fullmatch(
        r"(?:read[ _-]?me|install(?:ation)?|license|licence|changelog|changes|notice|credits)(?:\.[^.]+)?",
        name,
        flags=re.IGNORECASE,
    ) is not None

def _root_package_name(name: str) -> str | None:
    """Return a root-level ZIP member name, tolerating a leading ./ or .\\ prefix."""
    normalized = name.replace(chr(92), "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or "/" in normalized:
        return None
    return normalized


def _is_package_metadata(name: str) -> bool:
    """Identify optional root-level launcher metadata so it is never deployed as a game file."""
    root_name = _root_package_name(name)
    if root_name is None:
        return False
    return (
        root_name.casefold() == "info.txt"
        or (
            Path(root_name).stem.casefold() == "thumb"
            and Path(root_name).suffix.casefold() in MOD_THUMBNAIL_SUFFIXES
        )
    )


def _safe_member_name(name: str) -> str:
    if (
        not name
        or Path(name).name != name
        or "/" in name
        or "\\" in name
        or Path(name).suffix.casefold() not in MOD_MEMBER_SUFFIXES
    ):
        raise ManagerError(f"Invalid mod file name: {name}")
    return name


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class ModManager:
    def __init__(
        self,
        game_dir: Path | None = None,
        state_base: Path | None = None,
    ) -> None:
        self.state_base = (state_base or _default_state_base()).expanduser().resolve()
        self.state_base.mkdir(parents=True, exist_ok=True)
        self.config_path = self.state_base / "config.json"
        configured_game_dir = self._read_configured_game_dir()
        self.game_dir = _canonical_game_dir(
            game_dir or configured_game_dir or DEFAULT_GAME_DIR
        )
        self._lock = threading.RLock()

    def _read_configured_game_dir(self) -> Path | None:
        if not self.config_path.is_file():
            return None
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError(f"Could not read the manager settings: {error}") from error
        configured = config.get("game_dir") if isinstance(config, dict) else None
        if not isinstance(configured, str) or not configured:
            raise StateError("The saved manager settings do not contain a valid game folder.")
        return Path(configured)

    def _game_state_dir(self) -> Path:
        identity = os.path.normcase(str(self.game_dir)).casefold().encode("utf-8")
        key = hashlib.sha256(identity).hexdigest()[:20]
        return self.state_base / "games" / key

    @property
    def profiles_path(self) -> Path:
        return self._game_state_dir() / "profiles.json"

    @property
    def archive_dir(self) -> Path:
        return self._game_state_dir() / "mods"

    @property
    def manifest_path(self) -> Path:
        return self._game_state_dir() / "manifest.json"

    @property
    def unmanaged_cache_path(self) -> Path:
        return self._game_state_dir() / "unmanaged-cache.json"

    @property
    def paks_dir(self) -> Path:
        return self.game_dir / PAKS_DIRECTORY

    @property
    def logic_mods_dir(self) -> Path:
        return self.paks_dir / LOGIC_MODS_DIRECTORY

    @property
    def game_executable(self) -> Path:
        return self.game_dir / GAME_EXECUTABLE

    def _validate_game_dir(self, path: Path) -> Path:
        resolved = _canonical_game_dir(path)
        if not resolved.is_dir():
            raise ManagerError("That game folder does not exist.")
        if not (resolved / GAME_EXECUTABLE).is_file():
            raise ManagerError(
                f"Could not find {GAME_EXECUTABLE.as_posix()} in that game folder."
            )
        if not (resolved / PAKS_DIRECTORY).is_dir():
            raise ManagerError(f"Could not find {PAKS_DIRECTORY.as_posix()} in that game folder.")
        return resolved

    def set_game_dir(self, value: str) -> dict[str, object]:
        if not isinstance(value, str) or not value.strip():
            raise ManagerError("Choose the game's installation folder.")
        with self._lock:
            selected = self._validate_game_dir(Path(value.strip()))
            self.state_base.mkdir(parents=True, exist_ok=True)
            temporary = self.config_path.with_name(
                f".{self.config_path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump({"game_dir": str(selected)}, stream, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.config_path)
            finally:
                temporary.unlink(missing_ok=True)
            self.game_dir = selected
        return self.get_status()

    def _read_manifest(self) -> list[dict[str, object]]:
        if not self.manifest_path.exists():
            return []
        try:
            document = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError(f"Could not read the mod recovery manifest: {error}") from error
        if (
            not isinstance(document, dict)
            or document.get("version") != MANAGER_VERSION
            or not isinstance(document.get("mods"), list)
        ):
            raise StateError(
                "The mod recovery manifest is invalid or from an unsupported version. "
                "No files were changed."
            )
        mods = document["mods"]
        for mod in mods:
            if (
                not isinstance(mod, dict)
                or not isinstance(mod.get("id"), str)
                or not MOD_ID_PATTERN.fullmatch(mod["id"])
                or not isinstance(mod.get("filename"), str)
                or not isinstance(mod.get("stem"), str)
                or not isinstance(mod.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", mod["sha256"])
                or not isinstance(mod.get("size"), int)
                or not isinstance(mod.get("enabled"), bool)
                or mod.get("kind") not in MOD_KINDS
                or not isinstance(mod.get("members"), list)
                or not mod["members"]
            ):
                raise StateError(
                    "The mod recovery manifest contains an invalid entry. No files were changed."
                )
            _safe_mod_filename(mod["filename"])
            _safe_mod_stem(mod["stem"])
            for member in mod["members"]:
                if (
                    not isinstance(member, dict)
                    or not isinstance(member.get("name"), str)
                    or not isinstance(member.get("sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", member["sha256"])
                    or not isinstance(member.get("size"), int)
                    or member["size"] < 0
                    or ("preexisting" in member and not isinstance(member.get("preexisting"), bool))
                    or (
                        member["size"] == 0
                        and not (
                            isinstance(member.get("target"), str)
                            and member["target"].casefold().startswith("gzpro/binaries/win64/ue4ss/mods/")
                            and member["target"].casefold().endswith("/enabled.txt")
                        )
                    )
                ):
                    raise StateError(
                        "The mod recovery manifest contains an invalid entry. No files were changed."
                    )
                if "target" not in member:
                    _safe_member_name(member["name"])
                    continue
                target = member["target"]
                skin_target = target.casefold().startswith("gzpro/content/paks/~mods/") if isinstance(target, str) else False
                loader_target = target in {
                    "GZPro/Binaries/Win64/ue4ss/Mods/GZSkinLoader/Scripts/main.lua",
                    "GZPro/Binaries/Win64/ue4ss/Mods/GZSkinLoader/enabled.txt",
                } if isinstance(target, str) else False
                proxy_target = isinstance(target, str) and target.casefold() == "gzpro/binaries/win64/dwmapi.dll"
                binary_dll_target = (
                    isinstance(target, str)
                    and target.rsplit("/", 1)[0].casefold() == "gzpro/binaries/win64"
                    and target.casefold().endswith(".dll")
                )
                ue4ss_runtime_target = isinstance(target, str) and target.casefold().startswith("gzpro/binaries/win64/ue4ss/")
                if (
                    not isinstance(target, str)
                    or not (skin_target or loader_target or proxy_target or binary_dll_target or ue4ss_runtime_target)
                    or "\\" in target
                    or any(part in {"", ".", ".."} for part in target.split("/"))
                    or Path(target).name != member["name"]
                ):
                    raise StateError("The mod recovery manifest contains an invalid target path.")
                if skin_target:
                    if Path(member["name"]).suffix.casefold() not in MOD_MEMBER_SUFFIXES:
                        raise StateError("The mod recovery manifest contains an invalid skin file.")
                    _safe_mod_stem(Path(member["name"]).stem)
                elif not (loader_target or proxy_target or binary_dll_target or ue4ss_runtime_target):
                    raise StateError("The mod recovery manifest contains an invalid loader file.")
        return mods

    def _write_manifest(self, mods: list[dict[str, object]]) -> None:
        self._game_state_dir().mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_name(
            f".{self.manifest_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(
                    {"version": MANAGER_VERSION, "mods": mods},
                    stream,
                    indent=2,
                    ensure_ascii=False,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.manifest_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_profiles(self) -> dict[str, object]:
        if not self.profiles_path.exists():
            return {"version": 1, "active_profile_id": None, "profiles": []}
        try:
            data = json.loads(self.profiles_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError(f"Could not read saved mod profiles: {error}") from error
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("profiles"), list):
            raise StateError("The saved mod profiles are invalid. No game files were changed.")
        seen = set()
        for profile in data["profiles"]:
            if (not isinstance(profile, dict) or not isinstance(profile.get("id"), str)
                    or not MOD_ID_PATTERN.fullmatch(profile["id"])
                    or not isinstance(profile.get("name"), str)
                    or not profile["name"].strip() or len(profile["name"]) > 48
                    or not isinstance(profile.get("mod_ids"), list)
                    or any(not isinstance(item, str) or not MOD_ID_PATTERN.fullmatch(item) for item in profile["mod_ids"])
                    or profile["id"] in seen):
                raise StateError("The saved mod profiles contain an invalid entry.")
            seen.add(profile["id"])
        active = data.get("active_profile_id")
        if active is not None and (not isinstance(active, str) or active not in seen):
            data["active_profile_id"] = None
        return data

    def _write_profiles(self, data: dict[str, object]) -> None:
        self._game_state_dir().mkdir(parents=True, exist_ok=True)
        temporary = self.profiles_path.with_name(f".{self.profiles_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.profiles_path)
        finally:
            temporary.unlink(missing_ok=True)

    def save_profile(self, name: str) -> dict[str, object]:
        if not isinstance(name, str):
            raise ManagerError("Enter a name for this profile.")
        name = name.strip()
        if not name or len(name) > 48 or any(ord(ch) < 32 for ch in name):
            raise ManagerError("Profile names must be 1–48 characters.")
        with self._lock:
            mods = self._read_manifest()
            data = self._read_profiles()
            if any(item["name"].casefold() == name.casefold() for item in data["profiles"]):
                raise ConflictError("A profile with that name already exists.")
            profile = {"id": uuid.uuid4().hex, "name": name,
                       "mod_ids": [str(mod["id"]) for mod in mods if mod["enabled"]]}
            data["profiles"].append(profile)
            data["active_profile_id"] = profile["id"]
            self._write_profiles(data)
            return {"profiles": data["profiles"], "active_profile_id": data["active_profile_id"]}

    def apply_profile(self, profile_id: str) -> dict[str, object]:
        if not isinstance(profile_id, str) or not MOD_ID_PATTERN.fullmatch(profile_id):
            raise ManagerError("Choose a valid profile.")
        with self._lock:
            self._validate_game_dir(self.game_dir)
            self._require_game_closed()
            data = self._read_profiles()
            profile = next((item for item in data["profiles"] if item["id"] == profile_id), None)
            if profile is None:
                raise ManagerError("That profile no longer exists.")
            mods = self._read_manifest()
            wanted = set(profile["mod_ids"])
            # Verify every backup and existing target before changing any mod.
            for record in mods:
                archive = self._archive_path(record)
                if not archive.is_file():
                    raise StateError(f"The backup for {record['filename']} is missing. The profile was not applied.")
                digest, size = _hash_file(archive)
                if digest != record["sha256"] or size != record["size"]:
                    raise StateError(f"The backup for {record['filename']} failed its integrity check.")
                target_dir = self._mod_target_dir(record)
                for member in record["members"]:
                    destination = self._member_path(member, target_dir)
                    installed = self._existing_named_file(destination.parent, destination.name)
                    if installed is not None:
                        actual_hash, actual_size = _hash_file(installed)
                        if actual_hash != member["sha256"] or actual_size != member["size"]:
                            raise ConflictError(f"{installed.name} was changed outside the manager. The profile was not applied.")
            for record in mods:
                self.set_mod_enabled(str(record["id"]), str(record["id"]) in wanted)
            data["active_profile_id"] = profile_id
            self._write_profiles(data)
            return {"profiles": data["profiles"], "active_profile_id": profile_id, "mods": self.get_status()["mods"]}

    def delete_profile(self, profile_id: str) -> dict[str, object]:
        if not isinstance(profile_id, str) or not MOD_ID_PATTERN.fullmatch(profile_id):
            raise ManagerError("Choose a valid profile.")
        with self._lock:
            data = self._read_profiles()
            remaining = [item for item in data["profiles"] if item["id"] != profile_id]
            if len(remaining) == len(data["profiles"]):
                raise ManagerError("That profile no longer exists.")
            data["profiles"] = remaining
            if data.get("active_profile_id") == profile_id:
                data["active_profile_id"] = None
            self._write_profiles(data)
            return {"profiles": remaining, "active_profile_id": data["active_profile_id"]}

    def _find_mod(self, mods: list[dict[str, object]], mod_id: str) -> dict[str, object]:
        if not MOD_ID_PATTERN.fullmatch(mod_id):
            raise ManagerError("That mod entry is invalid.")
        for mod in mods:
            if mod["id"] == mod_id:
                return mod
        raise ManagerError("That mod is not in this manager's library.")

    def _archive_path(self, mod: dict[str, object]) -> Path:
        return self.archive_dir / f"{mod['id']}.zip"

    def _existing_named_file(self, directory: Path, filename: str) -> Path | None:
        if not directory.is_dir():
            return None
        wanted = filename.casefold()
        for entry in directory.iterdir():
            if entry.name.casefold() == wanted:
                return entry
        return None

    def _check_logic_mods_path(self, create: bool) -> None:
        if not self.paks_dir.is_dir():
            raise ManagerError(f"The game's Paks folder is missing: {self.paks_dir}")
        if self.logic_mods_dir.is_symlink() or (
            hasattr(self.logic_mods_dir, "is_junction")
            and self.logic_mods_dir.is_junction()
        ):
            raise ConflictError(
                "LogicMods is a link or junction, so the manager will not write through it."
            )
        if create:
            self.logic_mods_dir.mkdir(exist_ok=True)
        if self.logic_mods_dir.exists() and not self.logic_mods_dir.is_dir():
            raise ConflictError("The LogicMods path exists but is not a folder.")
        if self.logic_mods_dir.exists():
            if self.logic_mods_dir.resolve().parent != self.paks_dir.resolve():
                raise ConflictError("LogicMods resolves outside the game's Paks folder.")

    def _is_game_running(self) -> bool:
        if sys.platform != "win32":
            raise StateError("Game process detection is supported only on Windows.")
        with GAME_CHECK_LOCK:
            try:
                result = subprocess.run(
                    [
                        "tasklist.exe",
                        "/FI",
                        f"IMAGENAME eq {GAME_EXECUTABLE.name}",
                        "/FO",
                        "CSV",
                        "/NH",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise StateError(f"Could not check whether the game is running: {error}") from error
        try:
            rows = csv.reader(result.stdout.splitlines())
            return any(
                row and row[0].strip('"').casefold() == GAME_EXECUTABLE.name.casefold()
                for row in rows
            )
        except csv.Error as error:
            raise StateError(f"Could not parse the Windows process list: {error}") from error

    def _require_game_closed(self) -> None:
        if self._is_game_running():
            raise ConflictError("Close the game before enabling or disabling a mod.")

    def open_mod_location(self) -> dict[str, object]:
        """Open the game's Paks directory, which contains LogicMods and ~mods."""
        if not self.paks_dir.is_dir():
            raise ManagerError("The game's Paks folder was not found. Check the selected game installation.")
        if sys.platform != "win32":
            raise StateError("Opening the mod install folder from the manager is supported on Windows only.")
        try:
            subprocess.Popen(
                ["explorer.exe", str(self.paks_dir)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as error:
            raise ManagerError(f"Could not open the mod install folder: {error}") from error
        return {"opened": True, "path": str(self.paks_dir)}

    def launch_game(self) -> dict[str, object]:
        if sys.platform != "win32":
            raise StateError("Launching the game from the manager is supported on Windows only.")
        if not self.game_executable.is_file():
            raise ManagerError("The game executable was not found. Check the selected game folder.")
        if self._is_game_running():
            raise ConflictError("Godzilla: Destroy All Monsters Melee Remastered is already running.")
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            subprocess.Popen(
                [str(self.game_executable)],
                cwd=str(self.game_executable.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=flags,
            )
        except OSError as error:
            raise ManagerError(f"Could not launch the game: {error}") from error
        return {"launched": True}

    def _copy_stream_to_game(
        self,
        source: BinaryIO,
        destination: Path,
        expected_hash: str,
        expected_size: int,
    ) -> None:
        temporary = destination.with_name(
            f".{destination.name}.{uuid.uuid4().hex}.tmp"
        )
        digest = hashlib.sha256()
        copied = 0
        try:
            with temporary.open("xb") as output_stream:
                while chunk := source.read(1024 * 1024):
                    output_stream.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            if copied != expected_size or digest.hexdigest() != expected_hash:
                raise StateError("The manager's saved mod copy failed its integrity check.")
            if self._existing_named_file(destination.parent, destination.name) is not None:
                raise ConflictError(
                    f"{destination.name} already exists in LogicMods; it was not overwritten."
                )
            temporary.rename(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_package_members(
        self, archive: Path, skip_unsupported: bool = False, install_dlls: bool = False
    ) -> tuple[str, list[dict[str, object]], str, str | None]:
        """Read cooked mods, skin-loader packages, and game-root UE4SS layouts."""
        try:
            with zipfile.ZipFile(archive) as package:
                infos = [info for info in package.infolist() if not info.is_dir()]
                info_entries = [
                    info for info in infos
                    if (_root_package_name(info.filename) or "").casefold() == "info.txt"
                ]
                thumb_entries = [
                    info for info in infos
                    if (root_name := _root_package_name(info.filename)) is not None
                    and Path(root_name).stem.casefold() == "thumb"
                    and Path(root_name).suffix.casefold() in MOD_THUMBNAIL_SUFFIXES
                ]
                if len(info_entries) > 1 or len(thumb_entries) > 1:
                    raise ManagerError("Include only one root-level INFO.txt and one THUMB image.")

                description = ""
                if info_entries:
                    info = info_entries[0]
                    if _root_package_name(info.filename) is None:
                        raise ManagerError("INFO.txt must be at the root of the mod zip.")
                    if info.file_size > MAX_INFO_SIZE:
                        raise ManagerError("INFO.txt must be 16 KB or smaller.")
                    try:
                        with package.open(info) as stream:
                            description = stream.read(MAX_INFO_SIZE + 1).decode("utf-8-sig").strip()
                    except UnicodeDecodeError as error:
                        raise ManagerError("INFO.txt must be saved as UTF-8 text.") from error
                    if len(description.encode("utf-8")) > MAX_INFO_SIZE:
                        raise ManagerError("INFO.txt must be 16 KB or smaller.")

                thumbnail = None
                if thumb_entries:
                    thumb = thumb_entries[0]
                    if _root_package_name(thumb.filename) is None:
                        raise ManagerError("The THUMB image must be at the root of the mod zip.")
                    if thumb.file_size <= 0 or thumb.file_size > MAX_THUMBNAIL_SIZE:
                        raise ManagerError("The THUMB image must be between 1 byte and 1 MB.")
                    with package.open(thumb) as stream:
                        image_bytes = stream.read(MAX_THUMBNAIL_SIZE + 1)
                    signatures = (
                        (".png", b"\x89PNG\r\n\x1a\n", "image/png"),
                        (".jpg", b"\xff\xd8\xff", "image/jpeg"),
                        (".jpeg", b"\xff\xd8\xff", "image/jpeg"),
                        (".webp", b"RIFF", "image/webp"),
                    )
                    suffix = Path(_root_package_name(thumb.filename) or "").suffix.casefold()
                    match = next((item for item in signatures if item[0] == suffix), None)
                    valid_signature = bool(match and image_bytes.startswith(match[1]))
                    if suffix == ".webp":
                        valid_signature = valid_signature and image_bytes[8:12] == b"WEBP"
                    if not valid_signature:
                        raise ManagerError("THUMB must be a valid PNG, JPG, or WebP image with a matching file extension.")
                    thumbnail = f"data:{match[2]};base64," + base64.b64encode(image_bytes).decode("ascii")

                cooked_infos = [
                    info for info in infos
                    if not _is_package_metadata(info.filename)
                    and not _is_package_document(info.filename)
                ]
                if not cooked_infos:
                    raise ManagerError("The package contains no supported mod files.")

                normalized = []
                for info in cooked_infos:
                    name = info.filename.replace(chr(92), "/")
                    while name.startswith("./"):
                        name = name[2:]
                    if not name or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/")):
                        raise ManagerError(f"Unsafe path in the package: {info.filename}")
                    normalized.append((info, name))

                has_skin_loader = any(name.casefold().startswith("gzskinloader/") for _, name in normalized)
                skin_infos = []
                loader_infos = []
                runtime_infos = []
                for info, name in normalized:
                    parts = name.split("/")
                    folded = name.casefold()
                    if len(parts) == 1 and Path(name).suffix.casefold() in MOD_MEMBER_SUFFIXES:
                        skin_infos.append((info, name, None))
                    elif len(parts) == 2 and Path(parts[1]).suffix.casefold() in MOD_MEMBER_SUFFIXES:
                        skin_infos.append((info, name, parts[0]))
                    elif folded == "gzpro/binaries/win64/dwmapi.dll":
                        runtime_infos.append((info, name, "GZPro/Binaries/Win64/dwmapi.dll"))
                    elif (
                        Path(name).suffix.casefold() == ".dll"
                        and (
                            len(parts) == 1
                            or (len(parts) == 2 and parts[0].casefold() == "win64")
                            or (len(parts) == 3 and parts[0].casefold() == "binaries" and parts[1].casefold() == "win64")
                            or (len(parts) == 4 and [part.casefold() for part in parts[:3]] == ["gzpro", "binaries", "win64"])
                        )
                    ):
                        if not install_dlls:
                            raise ManagerError(f"DLL_INSTALL_CONFIRM_REQUIRED:{name}")
                        dll_name = parts[-1]
                        _safe_mod_stem(Path(dll_name).stem)
                        runtime_infos.append((info, name, f"GZPro/Binaries/Win64/{dll_name}"))
                    elif folded.startswith("gzpro/binaries/win64/ue4ss/"):
                        relative = name[len("GZPro/Binaries/Win64/ue4ss/"):]
                        if not relative or any(part in {"", ".", ".."} for part in relative.split("/")):
                            raise ManagerError(f"Unsafe UE4SS file path in the package: {name}")
                        runtime_infos.append((info, name, "GZPro/Binaries/Win64/ue4ss/" + relative))
                    elif folded.startswith("gzpro/content/paks/~mods/"):
                        relative = name[len("GZPro/Content/Paks/~mods/"):]
                        if "/" in relative or Path(relative).suffix.casefold() not in MOD_MEMBER_SUFFIXES:
                            raise ManagerError(f"Unsupported game Paks path in the package: {name}")
                        skin_infos.append((info, name, None))
                    elif (
                        len(parts) == 3
                        and parts[0].casefold() == "gzskinloader"
                        and parts[1].casefold() == "scripts"
                        and parts[2].casefold() == "main.lua"
                    ) or (
                        len(parts) == 2
                        and parts[0].casefold() == "gzskinloader"
                        and parts[1].casefold() == "enabled.txt"
                    ):
                        loader_infos.append((info, name))
                    elif skip_unsupported:
                        continue
                    else:
                        raise ManagerError(f"Unsupported file or folder in the package: {name}")

                if has_skin_loader and not any(name.casefold() == "gzskinloader/scripts/main.lua" for _, name in loader_infos):
                    raise ManagerError("GZSkinLoader must include Scripts/main.lua.")

                composite_package = has_skin_loader or bool(runtime_infos) or any(
                    folder is not None or entry.casefold().startswith("gzpro/content/paks/~mods/")
                    for _, entry, folder in skin_infos
                )
                utoc_entry = next(
                    (entry for _, entry, _ in skin_infos if Path(entry).suffix.casefold() == ".utoc"),
                    None,
                )
                skin_stem = _safe_mod_stem(Path(utoc_entry).stem) if utoc_entry else None
                skin_folder = None
                suffixes: set[str] = set()
                members: list[dict[str, object]] = []
                total_size = 0

                def add_member(info, entry, deployed_name, target=None):
                    nonlocal total_size
                    is_empty_loader_flag = entry.casefold().endswith("/enabled.txt") or entry.casefold() == "gzskinloader/enabled.txt"
                    if info.file_size <= 0 and not is_empty_loader_flag:
                        raise ManagerError(f"{entry} in the package is empty.")
                    if info.file_size > MAX_MOD_SIZE:
                        raise ManagerError(f"{entry} exceeds the 8 GiB size limit.")
                    total_size += info.file_size
                    if total_size > MAX_MOD_SIZE:
                        raise ManagerError("The package's contents exceed the 8 GiB size limit.")
                    digest = hashlib.sha256()
                    with package.open(info) as member_stream:
                        while chunk := member_stream.read(1024 * 1024):
                            digest.update(chunk)
                    item = {"entry": entry, "name": deployed_name, "sha256": digest.hexdigest(), "size": info.file_size}
                    if target:
                        item["target"] = target
                    members.append(item)

                for info, entry, folder in skin_infos:
                    suffix = Path(entry).suffix.casefold()
                    if suffix not in MOD_MEMBER_SUFFIXES:
                        raise ManagerError(f"Unsupported file in the package: {entry}")
                    current_stem = _safe_mod_stem(Path(entry).stem)
                    if folder:
                        if skin_folder is None:
                            skin_folder = folder
                        elif folder.casefold() != skin_folder.casefold():
                            raise ManagerError("The skin container files must be in the same mod folder.")
                    if skin_stem is None:
                        skin_stem = current_stem

                    if suffix in suffixes and not (suffix == ".pak" and composite_package):
                        raise ManagerError(f"The package contains more than one {suffix} file.")
                    suffixes.add(suffix)
                    target = (
                        f"GZPro/Content/Paks/~mods/{current_stem}{suffix}"
                        if composite_package else None
                    )
                    add_member(info, entry, current_stem + suffix, target)

                if skin_stem:
                    allowed_folder_names = {skin_stem.casefold(), (skin_stem + "_P").casefold()}
                    if skin_stem.casefold().endswith("_p"):
                        allowed_folder_names.add(skin_stem[:-2].casefold())
                    if skin_folder and skin_folder.casefold() not in allowed_folder_names:
                        raise ManagerError("The skin container folder must match the .utoc mod name.")
                if skin_infos and not REQUIRED_MEMBER_SUFFIXES.issubset(suffixes):
                    raise ManagerError("The package must contain the cooked mod's .utoc and .ucas files.")
                if has_skin_loader and not skin_infos:
                    raise ManagerError("A GZSkinLoader package must also include its matching cooked .utoc and .ucas files.")
                if not skin_infos and not loader_infos and not runtime_infos:
                    raise ManagerError("The package contains no supported mod files.")

                if skin_stem and ".pak" not in suffixes:
                    stub_member = {
                        "entry": None, "suffix": ".pak",
                        "name": skin_stem + ".pak",
                        "sha256": hashlib.sha256(_STUB_PAK).hexdigest(),
                        "size": len(_STUB_PAK), "stub": True,
                    }
                    if composite_package:
                        stub_member["target"] = f"GZPro/Content/Paks/~mods/{skin_stem}.pak"
                    members.append(stub_member)

                for info, entry in loader_infos:
                    relative = entry.split("/", 1)[1]
                    target = "GZPro/Binaries/Win64/ue4ss/Mods/GZSkinLoader/" + relative
                    add_member(info, entry, Path(relative).name, target)

                for info, entry, target in runtime_infos:
                    add_member(info, entry, Path(entry).name, target)

                stem = skin_stem
                if not stem:
                    mod_folder = next(
                        (
                            name.split("/")[5]
                            for _, name, _ in runtime_infos
                            if name.casefold().startswith("gzpro/binaries/win64/ue4ss/mods/")
                            and len(name.split("/")) > 5
                        ),
                        None,
                    )
                    stem = mod_folder or Path(archive.name).stem
                stem = _safe_mod_stem(stem)
                if not members:
                    raise ManagerError("The package contains no installable mod files.")
                destinations = [
                    str(member.get("target") or member["name"]).casefold()
                    for member in members
                ]
                if len(destinations) != len(set(destinations)):
                    raise ManagerError("The package contains duplicate files targeting the same game location.")
                return stem, members, description, thumbnail
        except zipfile.BadZipFile as error:
            raise ManagerError("The uploaded file is not a valid .zip package.") from error

    def _member_path(self, member: dict[str, object], fallback_dir: Path) -> Path:
        target = member.get("target")
        if not target:
            return fallback_dir / str(member["name"])
        parts = str(target).split("/")
        destination = self.game_dir.joinpath(*parts)
        current = self.game_dir
        for part in parts[:-1]:
            current = current / part
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise ConflictError(f"{current} is a link or junction; the manager will not write through it.")
            if current.exists() and not current.is_dir():
                raise ConflictError(f"{current} exists but is not a folder.")
        return destination

    def _mod_target_dir(self, record: dict[str, object]) -> Path:
        if record["kind"] == MOD_KIND_OVERRIDE:
            return self.paks_dir
        return self.logic_mods_dir

    @staticmethod
    def _deployed_stem(stem: str, kind: str) -> str:
        if kind == MOD_KIND_OVERRIDE and not stem.casefold().startswith(
            OVERRIDE_PREFIX.casefold()
        ):
            return OVERRIDE_PREFIX + stem
        return stem

    def _deploy_members(
        self,
        archive: Path,
        members: list[dict[str, object]],
        target_dir: Path,
    ) -> None:
        for member in members:
            destination = self._member_path(member, target_dir)
            installed = self._existing_named_file(destination.parent, destination.name)
            if installed is not None:
                if member.get("preexisting"):
                    actual_hash, actual_size = _hash_file(installed)
                    if actual_hash == member["sha256"] and actual_size == member["size"]:
                        continue
                raise ConflictError(
                    f"{destination.name} already exists in {destination.parent}; it was not overwritten. "
                    "If this is an existing UE4SS install, the bundled loader file must match it exactly."
                )
        with zipfile.ZipFile(archive) as package:
            for member in members:
                destination = self._member_path(member, target_dir)
                installed = self._existing_named_file(destination.parent, destination.name)
                if installed is not None and member.get("preexisting"):
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                if member.get("stub"):
                    self._copy_stream_to_game(
                        io.BytesIO(_STUB_PAK),
                        destination,
                        str(member["sha256"]),
                        int(member["size"]),
                    )
                else:
                    with package.open(str(member["entry"])) as member_stream:
                        self._copy_stream_to_game(
                            member_stream,
                            destination,
                            str(member["sha256"]),
                            int(member["size"]),
                        )

    def install_mod(
        self,
        stream: BinaryIO,
        filename: str,
        expected_size: int,
        kind: str = MOD_KIND_OVERRIDE,
        replace_mod_id: str | None = None,
        skip_unsupported: bool = False,
        install_dlls: bool = False,
    ) -> dict[str, object]:
        filename = _safe_mod_filename(filename)
        if kind not in MOD_KINDS:
            raise ManagerError("Choose whether this mod replaces game assets or adds a Blueprint mod.")
        if expected_size <= 0:
            raise ManagerError("The selected .zip package is empty.")
        if expected_size > MAX_MOD_SIZE:
            raise ManagerError("That package exceeds the 8 GiB upload limit.")
        with self._lock:
            self._validate_game_dir(self.game_dir)
            self._require_game_closed()
            self._check_logic_mods_path(create=False)
            mods = self._read_manifest()

            self.archive_dir.mkdir(parents=True, exist_ok=True)
            mod_id = uuid.uuid4().hex
            staged_archive = self.archive_dir / f".{mod_id}.upload"
            digest = hashlib.sha256()
            received = 0
            archive_path = self.archive_dir / f"{mod_id}.zip"
            try:
                with staged_archive.open("xb") as output_stream:
                    while received < expected_size:
                        chunk = stream.read(min(1024 * 1024, expected_size - received))
                        if not chunk:
                            raise ManagerError("The upload ended before the complete package was received.")
                        output_stream.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
                staged_archive.rename(archive_path)

                stem, members, description, thumbnail = self._read_package_members(archive_path, skip_unsupported=skip_unsupported, install_dlls=install_dlls)
                if kind == MOD_KIND_OVERRIDE and stem.casefold() in GAME_CONTAINER_STEMS:
                    raise ManagerError(
                        f"The mod name {stem} is reserved by the game itself."
                    )
                existing = next(
                    (mod for mod in mods if str(mod["stem"]).casefold() == stem.casefold()),
                    None,
                )
                if existing and not replace_mod_id:
                    raise ConflictError(f"MOD_REPLACE_REQUIRED:{existing['id']}:{stem}")
                if replace_mod_id and (not existing or str(existing["id"]) != replace_mod_id):
                    raise ConflictError("The mod selected for replacement has changed. Refresh the mod list and try again.")
                if existing and existing["kind"] != kind:
                    raise ManagerError("The existing mod uses a different install type. Remove it before changing its type.")

                deployed_stem = self._deployed_stem(stem, kind)
                for member in members:
                    if "target" in member:
                        continue
                    suffix = (
                        str(member.get("suffix"))
                        if member.get("entry") is None
                        else Path(str(member["entry"])).suffix
                    )
                    member["name"] = deployed_stem + suffix.lower()

                if kind == MOD_KIND_BLUEPRINT and (not existing or bool(existing["enabled"])):
                    self._check_logic_mods_path(create=True)
                self._require_game_closed()
                if existing:
                    old_archive = self._archive_path(existing)
                    old_hash, old_size = _hash_file(old_archive) if old_archive.is_file() else ("", -1)
                    if old_hash != existing["sha256"] or old_size != existing["size"]:
                        raise StateError("The existing mod backup failed its integrity check. It was not replaced.")
                    old_target = self._mod_target_dir(existing)
                    old_installed = []
                    old_paths = {}
                    old_members_by_path = {}
                    for old_member in existing["members"]:
                        old_path = self._member_path(old_member, old_target)
                        old_key = str(old_path.resolve()).casefold()
                        old_members_by_path[old_key] = old_member
                        installed = self._existing_named_file(old_path.parent, old_path.name)
                        if installed is not None:
                            actual_hash, actual_size = _hash_file(installed)
                            if (
                                (existing["enabled"] and (actual_hash != old_member["sha256"] or actual_size != old_member["size"]))
                                or (not existing["enabled"] and not old_member.get("preexisting"))
                            ):
                                raise ConflictError(f"{installed.name} does not match the existing managed mod. It was left untouched.")
                            if not old_member.get("preexisting"):
                                old_installed.append(installed)
                            old_paths[old_key] = installed
                    if existing["enabled"]:
                        for member in members:
                            destination = self._member_path(member, self._mod_target_dir({"kind": kind}))
                            installed = self._existing_named_file(destination.parent, destination.name)
                            if installed is not None:
                                key = str(destination.resolve()).casefold()
                                if key in old_paths and old_members_by_path.get(key, {}).get("preexisting"):
                                    actual_hash, actual_size = _hash_file(installed)
                                    if actual_hash == member["sha256"] and actual_size == member["size"]:
                                        member["preexisting"] = True
                                        continue
                                if key not in old_paths:
                                    actual_hash, actual_size = _hash_file(installed)
                                    if actual_hash != member["sha256"] or actual_size != member["size"]:
                                        raise ConflictError(
                                            f"{installed.name} already exists and differs from this mod. "
                                            "The manager will not overwrite it."
                                        )
                                    member["preexisting"] = True
                        for member in members:
                            destination = self._member_path(member, self._mod_target_dir({"kind": kind}))
                            installed = self._existing_named_file(destination.parent, destination.name)
                            if (
                                installed is not None
                                and str(destination.resolve()).casefold() not in old_paths
                                and not member.get("preexisting")
                            ):
                                raise ConflictError(f"{installed.name} already exists outside the mod being replaced. It was not overwritten.")
                        for installed in old_installed:
                            installed.unlink()
                        try:
                            self._deploy_members(archive_path, members, self._mod_target_dir({"kind": kind}))
                        except Exception:
                            for member in members:
                                destination = self._member_path(member, self._mod_target_dir({"kind": kind}))
                                installed = self._existing_named_file(destination.parent, destination.name)
                                if installed is not None and not member.get("preexisting"):
                                    actual_hash, actual_size = _hash_file(installed)
                                    if actual_hash == member["sha256"] and actual_size == member["size"]:
                                        installed.unlink()
                            self._deploy_members(old_archive, existing["members"], old_target)
                            raise
                    previous_archive = self.archive_dir / f".{existing['id']}.previous"
                    previous_archive.unlink(missing_ok=True)
                    old_archive.rename(previous_archive)
                    try:
                        archive_path.rename(old_archive)
                        new_record = {
                            **existing,
                            "filename": filename,
                            "stem": stem,
                            "kind": kind,
                            "sha256": digest.hexdigest(),
                            "size": received,
                            "description": description,
                            "thumbnail": thumbnail,
                            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "members": members,
                        }
                        mods = [new_record if item["id"] == existing["id"] else item for item in mods]
                        self._write_manifest(mods)
                    except Exception:
                        if old_archive.exists():
                            old_archive.unlink()
                        previous_archive.rename(old_archive)
                        if existing["enabled"]:
                            for member in members:
                                destination = self._member_path(member, self._mod_target_dir({"kind": kind}))
                                installed = self._existing_named_file(destination.parent, destination.name)
                                if installed is not None and not member.get("preexisting"):
                                    actual_hash, actual_size = _hash_file(installed)
                                    if actual_hash == member["sha256"] and actual_size == member["size"]:
                                        installed.unlink()
                            self._deploy_members(old_archive, existing["members"], old_target)
                        raise
                    previous_archive.unlink(missing_ok=True)
                    return self._describe_mod(new_record, old_archive)
                if kind == MOD_KIND_BLUEPRINT:
                    self._check_logic_mods_path(create=True)
                target_dir = self._mod_target_dir({"kind": kind})
                for member in members:
                    destination = self._member_path(member, target_dir)
                    installed = self._existing_named_file(destination.parent, destination.name)
                    if installed is None:
                        continue
                    actual_hash, actual_size = _hash_file(installed)
                    if actual_hash != member["sha256"] or actual_size != member["size"]:
                        raise ConflictError(
                            f"{installed.name} already exists and differs from this mod. "
                            "The manager will not overwrite it. If it is a UE4SS file, keep the existing loader version."
                        )
                    member["preexisting"] = True
                self._deploy_members(archive_path, members, target_dir)
            except Exception:
                archive_path.unlink(missing_ok=True)
                raise
            finally:
                staged_archive.unlink(missing_ok=True)

            record: dict[str, object] = {
                "id": mod_id,
                "filename": filename,
                "stem": stem,
                "kind": kind,
                "sha256": digest.hexdigest(),
                "size": received,
                "enabled": True,
                "description": description,
                "thumbnail": thumbnail,
                "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "members": members,
            }
            mods.append(record)
            self._write_manifest(mods)
            return self._describe_mod(record, archive_path)

    def set_mod_enabled(self, mod_id: str, enabled: bool) -> dict[str, object]:
        if not isinstance(enabled, bool):
            raise ManagerError("The requested mod state is invalid.")
        with self._lock:
            self._validate_game_dir(self.game_dir)
            self._require_game_closed()
            mods = self._read_manifest()
            record = self._find_mod(mods, mod_id)
            archive = self._archive_path(record)
            if not archive.is_file():
                raise StateError(
                    f"The manager's backup for {record['filename']} is missing; "
                    "no game files were changed."
                )
            actual_hash, actual_size = _hash_file(archive)
            if actual_hash != record["sha256"] or actual_size != record["size"]:
                raise StateError(
                    f"The manager's backup for {record['filename']} failed its integrity check."
                )

            if record["kind"] == MOD_KIND_BLUEPRINT:
                self._check_logic_mods_path(create=enabled)
            target_dir = self._mod_target_dir(record)
            members = record["members"]
            present: dict[str, Path] = {}
            for member in members:
                destination = self._member_path(member, target_dir)
                existing = self._existing_named_file(destination.parent, destination.name)
                if existing is None:
                    continue
                game_hash, game_size = _hash_file(existing)
                if game_hash != member["sha256"] or game_size != member["size"]:
                    raise ConflictError(
                        f"{existing.name} is already present but differs from this mod. "
                        "It was not overwritten."
                    )
                present[str(member.get("target") or member["name"])] = existing

            if enabled:
                missing = [
                    member for member in members if str(member.get("target") or member["name"]) not in present
                ]
                if missing:
                    for member in missing:
                        member.pop("preexisting", None)
                    with zipfile.ZipFile(archive) as package:
                        for member in missing:
                            destination = self._member_path(member, target_dir)
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            if member.get("stub"):
                                self._copy_stream_to_game(
                                    io.BytesIO(_STUB_PAK),
                                    destination,
                                    str(member["sha256"]),
                                    int(member["size"]),
                                )
                            else:
                                with package.open(str(member["entry"])) as member_stream:
                                    self._copy_stream_to_game(
                                        member_stream,
                                        destination,
                                        str(member["sha256"]),
                                        int(member["size"]),
                                    )
                record["enabled"] = True
            else:
                for member in members:
                    installed = present.get(str(member.get("target") or member["name"]))
                    if installed is not None:
                        if not member.get("preexisting"):
                            installed.unlink()
                record["enabled"] = False

            self._write_manifest(mods)
            if self.profiles_path.exists():
                profile_data = self._read_profiles()
                profile_data["active_profile_id"] = None
                self._write_profiles(profile_data)
            return self._describe_mod(record, archive)

    def delete_mod(self, mod_id: str) -> dict[str, object]:
        """Remove a managed mod and its backup, but only remove verified game files."""
        with self._lock:
            self._validate_game_dir(self.game_dir)
            self._require_game_closed()
            mods = self._read_manifest()
            record = self._find_mod(mods, mod_id)
            archive = self._archive_path(record)
            if not archive.is_file():
                raise StateError(
                    f"The manager's backup for {record['filename']} is missing; "
                    "the mod was not deleted."
                )
            actual_hash, actual_size = _hash_file(archive)
            if actual_hash != record["sha256"] or actual_size != record["size"]:
                raise StateError(
                    f"The manager's backup for {record['filename']} failed its integrity check; "
                    "the mod was not deleted."
                )

            if record["kind"] == MOD_KIND_BLUEPRINT:
                self._check_logic_mods_path(create=False)
            target_dir = self._mod_target_dir(record)
            installed_files: list[Path] = []
            for member in record["members"]:
                destination = self._member_path(member, target_dir)
                installed = self._existing_named_file(destination.parent, destination.name)
                if installed is None:
                    continue
                game_hash, game_size = _hash_file(installed)
                if game_hash != member["sha256"] or game_size != member["size"]:
                    raise ConflictError(
                        f"{installed.name} was changed outside the manager. "
                        "It was left untouched, and the mod was not deleted."
                    )
                if not member.get("preexisting"):
                    installed_files.append(installed)

            for installed in installed_files:
                installed.unlink()

            remaining_mods = [item for item in mods if item["id"] != mod_id]
            self._write_manifest(remaining_mods)
            archive.unlink(missing_ok=True)
            return {"deleted": True, "stem": record["stem"]}

    def _describe_mod(
        self,
        record: dict[str, object],
        archive: Path,
    ) -> dict[str, object]:
        members = record["members"]
        target_dir = self._mod_target_dir(record)
        installed_count = 0
        conflict = False
        for member in members:
            destination = self._member_path(member, target_dir)
            installed = self._existing_named_file(destination.parent, destination.name)
            if installed is None:
                continue
            installed_count += 1
            if installed.stat().st_size != member["size"]:
                conflict = True
        return {
            "id": record["id"],
            "filename": record["filename"],
            "stem": record["stem"],
            "display_name": str(record.get("display_name") or record["stem"]),
            "description": str(record.get("description", "")),
            "thumbnail": record.get("thumbnail"),
            "kind": record["kind"],
            "size": record["size"],
            "enabled": bool(record["enabled"]),
            "sha256": record["sha256"],
            "backup_available": (
                archive.is_file() and archive.stat().st_size == record["size"]
            ),
            "file_count": len(members),
            "requires_ue4ss": any(
                str(member.get("target", "")).casefold() == "gzpro/binaries/win64/dwmapi.dll"
                or str(member.get("target", "")).casefold().startswith("gzpro/binaries/win64/ue4ss/")
                for member in members
            ),
            "conflict": conflict,
            "partial": 0 < installed_count < len(members),
        }

    def rename_mod(self, mod_id: str, display_name: str) -> dict[str, object]:
        if not isinstance(display_name, str):
            raise ManagerError("Enter a name for this mod.")
        display_name = display_name.strip()
        if not display_name or len(display_name) > 64 or any(ord(ch) < 32 for ch in display_name):
            raise ManagerError("Mod names must be 1–64 characters.")
        with self._lock:
            mods = self._read_manifest()
            record = self._find_mod(mods, mod_id)
            record["display_name"] = display_name
            self._write_manifest(mods)
            return {"id": mod_id, "display_name": display_name}

    def adopt_unmanaged_mod(self, location: str, stem: str) -> dict[str, object]:
        """Back up and register an existing loose mod without changing its game files."""
        if location not in {"Paks", LOGIC_MODS_DIRECTORY}:
            raise ManagerError("Choose a valid mod location.")
        stem = _safe_mod_stem(stem)
        if len(stem) > 124:
            raise ManagerError("That mod name is too long to manage safely.")

        with self._lock:
            self._validate_game_dir(self.game_dir)
            self._require_game_closed()
            if location == LOGIC_MODS_DIRECTORY:
                self._check_logic_mods_path(create=False)
                target_dir = self.logic_mods_dir
                kind = MOD_KIND_BLUEPRINT
            else:
                target_dir = self.paks_dir
                kind = MOD_KIND_OVERRIDE
                if stem.casefold() in GAME_CONTAINER_STEMS:
                    raise ManagerError("The game's own container files cannot be adopted.")

            mods = self._read_manifest()
            managed_names = {
                str(member["name"]).casefold()
                for record in mods
                for member in record["members"]
            }
            sources = [
                path for path in sorted(target_dir.iterdir(), key=lambda item: item.name.casefold())
                if path.is_file()
                and not path.is_symlink()
                and path.suffix.casefold() in MOD_MEMBER_SUFFIXES
                and path.stem.casefold() == stem.casefold()
            ]
            if not sources:
                raise ManagerError("No matching loose mod files were found. Refresh the list and try again.")
            if any(path.name.casefold() in managed_names for path in sources):
                raise ConflictError("One or more files are already managed.")
            if any(str(record["stem"]).casefold() == stem.casefold() and record["kind"] == kind for record in mods):
                raise ConflictError("A mod with this name is already in the manager.")

            members: list[dict[str, object]] = []
            total_size = 0
            for source in sources:
                digest, size = _hash_file(source)
                if size <= 0:
                    raise ManagerError(f"{source.name} is empty and cannot be adopted.")
                total_size += size
                if total_size > MAX_MOD_SIZE:
                    raise ManagerError("These files exceed the manager's 8 GiB backup limit.")
                members.append({
                    "entry": source.name,
                    "name": source.name,
                    "sha256": digest,
                    "size": size,
                })

            self.archive_dir.mkdir(parents=True, exist_ok=True)
            mod_id = uuid.uuid4().hex
            archive_path = self.archive_dir / f"{mod_id}.zip"
            try:
                with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as package:
                    for source, member in zip(sources, members):
                        package.write(source, arcname=str(member["entry"]))
                        current_hash, current_size = _hash_file(source)
                        if current_hash != member["sha256"] or current_size != member["size"]:
                            raise ConflictError(
                                f"{source.name} changed while it was being backed up. No game files were changed."
                            )
                archive_hash, archive_size = _hash_file(archive_path)
                record: dict[str, object] = {
                    "id": mod_id,
                    "filename": f"{stem}.zip",
                    "stem": stem,
                    "kind": kind,
                    "sha256": archive_hash,
                    "size": archive_size,
                    "enabled": True,
                    "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "members": members,
                }
                mods.append(record)
                self._write_manifest(mods)
            except Exception:
                archive_path.unlink(missing_ok=True)
                raise

            return self._describe_mod(record, archive_path)

    def _unmanaged_cache_signature(self) -> dict[str, str | None]:
        """Return cheap directory/manifest metadata used to invalidate the startup scan cache."""
        signature: dict[str, str | None] = {}
        for key, path in (("paks", self.paks_dir), ("logic_mods", self.logic_mods_dir), ("manifest", self.manifest_path)):
            try:
                stat = path.stat()
                signature[key] = f"{stat.st_mtime_ns}:{stat.st_size}"
            except OSError:
                signature[key] = None
        return signature

    def _read_unmanaged_cache(self, signature: dict[str, str | None]) -> list[dict[str, object]] | None:
        try:
            data = json.loads(self.unmanaged_cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("version") != 1 or data.get("signature") != signature:
            return None
        items = data.get("mods")
        if not isinstance(items, list):
            return None
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get("stem"), str)
                    or item.get("location") not in {"Paks", LOGIC_MODS_DIRECTORY}
                    or not isinstance(item.get("files"), list)
                    or any(not isinstance(name, str) for name in item["files"])
                    or not isinstance(item.get("size"), int)):
                return None
        return items

    def _write_unmanaged_cache(
        self, signature: dict[str, str | None], items: list[dict[str, object]]
    ) -> None:
        self.unmanaged_cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.unmanaged_cache_path.with_name(
            f".{self.unmanaged_cache_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump({"version": 1, "signature": signature, "mods": items}, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.unmanaged_cache_path)
        except OSError:
            # The cache is an optimization only; status must remain available if it cannot be saved.
            pass
        finally:
            temporary.unlink(missing_ok=True)

    def _scan_unmanaged_mods(self, managed_names: set[str]) -> list[dict[str, object]]:
        unmanaged_files = []
        for directory, location in ((self.logic_mods_dir, LOGIC_MODS_DIRECTORY), (self.paks_dir, "Paks")):
            if not directory.is_dir() or directory.is_symlink():
                continue
            unmanaged_files.extend(
                (path, location)
                for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold())
                if path.is_file()
                and not path.is_symlink()
                and path.suffix.casefold() in MOD_MEMBER_SUFFIXES
                and path.name.casefold() not in managed_names
                and not (location == "Paks" and path.stem.casefold() in GAME_CONTAINER_STEMS)
            )
        grouped: dict[tuple[str, str], dict[str, object]] = {}
        for path, location in unmanaged_files:
            key = (location, path.stem.casefold())
            group = grouped.setdefault(key, {"stem": path.stem, "location": location, "files": [], "size": 0})
            group["files"].append(path.name)
            group["size"] += path.stat().st_size
        return sorted(grouped.values(), key=lambda group: (str(group["location"]).casefold(), str(group["stem"]).casefold()))

    def get_status(self) -> dict[str, object]:
        _set_status_progress("Waiting for the manager status check…")
        with self._lock:
            try:
                game_valid = True
                game_check_error = None
                path_checks = (
                    ("Checking the selected game folder…", lambda: self.game_dir.is_dir()),
                    ("Checking the GZPro executable…", lambda: self.game_executable.is_file()),
                    ("Checking the game's Paks folder…", lambda: self.paks_dir.is_dir()),
                )
                for stage, check in path_checks:
                    _set_status_progress(stage)
                    valid, error = _run_check_with_timeout(check)
                    if error:
                        game_check_error = f"{stage} {error}"
                        game_valid = False
                        break
                    if not valid:
                        game_valid = False
                        break

                game_running: bool | None = None
                if game_valid:
                    _set_status_progress("Checking whether GZPro is running…")
                    try:
                        game_running = self._is_game_running()
                    except StateError as error:
                        game_check_error = str(error)

                _set_status_progress("Reading your managed mod list…")
                mods = self._read_manifest()
                listed_mods = []
                managed_names: set[str] = set()
                if game_check_error and not game_valid:
                    # A timed-out filesystem probe must not be followed by more
                    # potentially blocking reads from the same unavailable drive.
                    unmanaged = []
                else:
                    total_mods = len(mods)
                    for index, record in enumerate(mods, start=1):
                        _set_status_progress(f"Checking installed files for mod {index} of {total_mods}…")
                        for member in record["members"]:
                            managed_names.add(str(member["name"]).casefold())
                        listed_mods.append(
                            self._describe_mod(record, self._archive_path(record))
                        )

                    _set_status_progress("Checking the mod-folder scan cache…")
                    cache_signature = self._unmanaged_cache_signature()
                    unmanaged = self._read_unmanaged_cache(cache_signature)
                    if unmanaged is None:
                        _set_status_progress("Scanning Paks and LogicMods for manually installed mods…")
                        unmanaged = self._scan_unmanaged_mods(managed_names)
                        self._write_unmanaged_cache(cache_signature, unmanaged)
                    else:
                        _set_status_progress("Using the saved manual-mod scan…")

                _set_status_progress("Checking UE4SS files…")
                binaries_dir = self.game_executable.parent
                ue4ss_installed = False
                if game_valid:
                    ue4ss_checks = []
                    for relative in UE4SS_RELATIVE_FILES:
                        valid, error = _run_check_with_timeout(
                            lambda relative=relative: (binaries_dir / relative).is_file()
                        )
                        if error:
                            game_check_error = error
                            break
                        ue4ss_checks.append(valid)
                    ue4ss_installed = len(ue4ss_checks) == len(UE4SS_RELATIVE_FILES) and all(ue4ss_checks)

                _set_status_progress("Checking saved mod profiles…")
                profiles = self._read_profiles()
                logic_mods_exists = False
                if game_valid:
                    logic_mods_exists, logic_error = _run_check_with_timeout(
                        lambda: self.logic_mods_dir.is_dir()
                    )
                    if logic_error and not game_check_error:
                        game_check_error = logic_error

                result = {
                    "game_dir": str(self.game_dir),
                    "game_valid": game_valid,
                    "game_executable": str(self.game_executable),
                    "paks_dir": str(self.paks_dir),
                    "logic_mods_dir": str(self.logic_mods_dir),
                    "logic_mods_exists": logic_mods_exists,
                    "game_running": game_running,
                    "game_check_error": game_check_error,
                    "mods": listed_mods,
                    "profiles": profiles["profiles"],
                    "active_profile_id": profiles["active_profile_id"],
                    "unmanaged_mods": unmanaged,
                    "loader_status": "verified" if game_valid else "unknown",
                    "ue4ss_installed": ue4ss_installed,
                    "warning": (
                        "Mods load through UE4SS's Blueprint mod loader; that path is "
                        "verified working on this game build. The manager deploys cooked "
                        "IoStore mod packages (.utoc/.ucas plus a companion .pak) into "
                        "LogicMods and never edits the game's original archives. It does "
                        "not install UE4SS for you."
                        if ue4ss_installed
                        else "UE4SS is not detected in the game's Win64 folder. Mods are "
                        "staged safely, but nothing will load until UE4SS (with "
                        "BPModLoaderMod) is installed there. The manager never edits the "
                        "game's original archives."
                    ),
                }
                _set_status_progress("Ready", active=False)
                return result
            except Exception:
                _set_status_progress("Status check failed", active=False)
                raise


def get_latest_release() -> dict[str, object]:
    request = urllib.request.Request(
        GITHUB_RELEASE_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "GZPro-Mod-Manager"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            release = json.loads(response.read(2 * 1024 * 1024))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise ManagerError(f"Could not check GitHub for updates: {error}") from error
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        raise ManagerError("GitHub did not return a published stable release.")
    tag = release.get("tag_name")
    assets = release.get("assets")
    if not isinstance(tag, str) or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ManagerError("The latest GitHub release has an invalid version tag.")
    if not isinstance(assets, list):
        raise ManagerError("The latest GitHub release has no valid asset list.")
    asset = next((item for item in assets if isinstance(item, dict) and item.get("name") == "GZProModManager.exe"), None)
    if not asset or not isinstance(asset.get("browser_download_url"), str):
        raise ManagerError("The latest release does not include GZProModManager.exe.")
    return {
        "current_version": APP_VERSION,
        "can_install": sys.platform == "win32" and getattr(sys, "frozen", False),
        "latest_version": tag.lstrip("v"),
        "tag": tag,
        "update_available": tuple(int(part) for part in tag[1:].split(".")) > tuple(int(part) for part in APP_VERSION.split(".")),
        "download_url": asset["browser_download_url"],
        "digest": asset.get("digest"),
        "release_url": release.get("html_url", "https://github.com/Chickenman456/gzpro-mod-manager/releases"),
        "published_at": release.get("published_at", ""),
    }


def schedule_self_update(release: dict[str, object]) -> None:
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        raise ManagerError("In-app installation is available in the Windows executable, not the Python source version.")
    current_exe = Path(sys.executable).resolve()
    update_dir = _default_state_base() / "updates"
    update_dir.mkdir(parents=True, exist_ok=True)
    # Stage beside the running executable so replacement stays on the same volume.
    write_probe = current_exe.with_name(f".{current_exe.stem}-{uuid.uuid4().hex}.write-test")
    try:
        with write_probe.open("xb") as probe:
            probe.write(b"update permission check")
        write_probe.unlink()
    except OSError as error:
        write_probe.unlink(missing_ok=True)
        raise ManagerError(
            "Windows does not allow the updater to write beside GZProModManager.exe. "
            "Move the manager to a folder your Windows account can modify, then try again."
        ) from error

    downloaded = update_dir / f"GZProModManager-{uuid.uuid4().hex}.exe"
    request = urllib.request.Request(
        str(release["download_url"]),
        headers={"User-Agent": "GZPro-Mod-Manager", "Accept": "application/octet-stream"},
    )
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response, downloaded.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 100 * 1024 * 1024:
                    raise ManagerError("The downloaded update is larger than the 100 MB safety limit.")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        with downloaded.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise ManagerError("The downloaded update is not a valid Windows executable.")
        expected_digest = release.get("digest")
        if isinstance(expected_digest, str) and expected_digest.startswith("sha256:"):
            if digest.hexdigest().casefold() != expected_digest[7:].casefold():
                raise ManagerError("The downloaded update failed GitHub's SHA-256 integrity check.")
        script = update_dir / f"install-{uuid.uuid4().hex}.ps1"
        log_file = update_dir / "update-install.log"
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$target = {quote(current_exe)}\n"
            f"$source = {quote(downloaded)}\n"
            f"$logPath = {quote(log_file)}\n"
            f"$managerProcessId = {os.getpid()}\n"
            "$staged = $target + '.update'\n"
            "$backup = $target + '.previous'\n"
            "$started = $null\n"
            "try {\n"
            "  Add-Content -LiteralPath $logPath -Value ((Get-Date -Format o) + ' Waiting for manager to close')\n"
            "  $deadline = (Get-Date).AddSeconds(120)\n"
            "  while (Get-Process -Id $managerProcessId -ErrorAction SilentlyContinue) {\n"
            "    if ((Get-Date) -gt $deadline) { throw 'Timed out waiting for the manager process to close.' }\n"
            "    Start-Sleep -Milliseconds 500\n"
            "  }\n"
            "  Copy-Item -LiteralPath $source -Destination $staged -Force\n"
            "  if (!(Test-Path -LiteralPath $staged) -or (Get-Item -LiteralPath $staged).Length -lt 1024) { throw 'The staged update file is missing or incomplete.' }\n"
            "  $header = ([System.IO.File]::ReadAllBytes($staged))[0..1]\n"
            "  if ($header[0] -ne 77 -or $header[1] -ne 90) { throw 'The staged update is not a Windows executable.' }\n"
            "  if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Force }\n"
            "  Copy-Item -LiteralPath $target -Destination $backup -Force\n"
            "  Move-Item -LiteralPath $staged -Destination $target -Force\n"
            "  Add-Content -LiteralPath $logPath -Value ((Get-Date -Format o) + ' Replaced executable; starting updated manager')\n"
            "  $started = Start-Process -FilePath $target -WorkingDirectory (Split-Path -Parent $target) -PassThru\n"
            "  Add-Content -LiteralPath $logPath -Value ((Get-Date -Format o) + ' Started updated manager process: ' + $started.Id)\n"
            "  $ready = $started.WaitForInputIdle(30000)\n"
            "  if (!$ready) { throw 'Updated manager did not become ready within 30 seconds.' }\n"
            "  Start-Sleep -Seconds 3\n"
            "  $started.Refresh()\n"
            "  if ($started.HasExited) { throw ('Updated manager exited during startup (exit code ' + $started.ExitCode + ').') }\n"
            "  Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue\n"
            "  Add-Content -LiteralPath $logPath -Value ((Get-Date -Format o) + ' Update succeeded; new manager process is running')\n"
            "} catch {\n"
            "  Add-Content -LiteralPath $logPath -Value ((Get-Date -Format o) + ' UPDATE ERROR: ' + $_.Exception.Message)\n"
            "  if (Test-Path -LiteralPath $backup) {\n"
            "    try {\n"
            "      if ($started -and !$started.HasExited) { Stop-Process -Id $started.Id -Force -ErrorAction SilentlyContinue }\n"
            "      Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue\n"
            "      Move-Item -LiteralPath $backup -Destination $target -Force\n"
            "      Start-Process -FilePath $target -WorkingDirectory (Split-Path -Parent $target)\n"
            "      Add-Content -LiteralPath $logPath -Value ((Get-Date -Format o) + ' Restored previous manager and restarted it')\n"
            "    } catch { Add-Content -LiteralPath $logPath -Value ((Get-Date -Format o) + ' ROLLBACK ERROR: ' + $_.Exception.Message) }\n"
            "  } elseif (Test-Path -LiteralPath $target) { Start-Process -FilePath $target }\n"
            "} finally {\n"
            "  Remove-Item -LiteralPath $staged -Force -ErrorAction SilentlyContinue\n"
            "  Remove-Item -LiteralPath $source -Force -ErrorAction SilentlyContinue\n"
            "  Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue\n"
            "}\n",
            encoding="utf-8",
        )
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags, close_fds=True,
        )
    except ManagerError:
        downloaded.unlink(missing_ok=True)
        raise
    except (OSError, urllib.error.URLError, subprocess.SubprocessError) as error:
        downloaded.unlink(missing_ok=True)
        raise ManagerError(f"Could not prepare the update: {error}") from error



class ManagerRequestHandler(SimpleHTTPRequestHandler):
    server_version = "GZProModManager/0.1"
    manager: ModManager

    def __init__(self, *args: object, manager: ModManager, **kwargs: object) -> None:
        self.manager = manager
        super().__init__(*args, directory=str(WEB_DIRECTORY), **kwargs)

    def translate_path(self, path: str) -> str:
        # The logo is stored at the project root, alongside manager.py, rather
        # than in web/. This also works in the PyInstaller one-file bundle.
        requested_path = unquote(urlsplit(path).path)
        if requested_path in {"/gzprologo.png", "/gzpicon.png"}:
            return str(Path(__file__).resolve().parent / requested_path.lstrip("/"))
        return super().translate_path(path)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self' https://api.github.com; img-src 'self' data: https:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _send_json(self, value: object, status: int = 200) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_manager_error(self, error: ManagerError) -> None:
        self._send_json({"error": str(error)}, error.status_code)

    def _local_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return False
        try:
            parsed = urlsplit(origin)
            origin_port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and origin_port == self.server.server_port
        )

    def _handle_request_error(self, error: Exception) -> None:
        if isinstance(error, ManagerError):
            self._send_manager_error(error)
            return
        self.log_error("Unhandled request error: %s", error)
        self._send_json({"error": "The request failed. Restart the manager and try again."}, 500)

    def do_GET(self) -> None:
        if self.path == "/api/update/check":
            try:
                self._send_json(get_latest_release())
            except ManagerError as error:
                self._send_manager_error(error)
            return
        if self.path == "/api/status/progress":
            self._send_json(_get_status_progress())
            return
        if self.path == "/api/status":
            try:
                self._send_json(self.manager.get_status())
            except (ManagerError, OSError) as error:
                self._handle_request_error(error)
            return
        if self.path.startswith("/api/"):
            self._send_json({"error": "Unknown API endpoint."}, 404)
            return
        super().do_GET()

    def do_POST(self) -> None:
        if not self._local_origin():
            self.close_connection = True
            self._send_json({"error": "Requests must come from this local manager page."}, 403)
            return
        try:
            if self.path == "/api/update/install":
                release = get_latest_release()
                if not release["update_available"]:
                    raise ManagerError("The manager is already up to date.")
                schedule_self_update(release)
                self._send_json({"scheduled": True, "version": release["latest_version"]})
                threading.Timer(2.5, lambda: os._exit(0)).start()
                return
            if self.path == "/api/game/launch":
                self._send_json(self.manager.launch_game())
                return
            if self.path == "/api/game/open-mod-location":
                self._send_json(self.manager.open_mod_location())
                return
            if self.path == "/api/game-dir":
                self._handle_game_dir()
                return
            if self.path == "/api/mods":
                self._handle_mod_upload()
                return
            rename_match = re.fullmatch(r"/api/mods/([0-9a-f]{32})/rename", self.path)
            if rename_match:
                body = self._read_json_body()
                self._send_json(self.manager.rename_mod(rename_match.group(1), body.get("display_name")))
                return
            if self.path == "/api/mods/adopt":
                self._handle_mod_adopt()
                return
            if self.path == "/api/profiles":
                body = self._read_json_body()
                self._send_json(self.manager.save_profile(body.get("name")), 201)
                return
            profile_match = re.fullmatch(r"/api/profiles/([0-9a-f]{32})/(apply|delete)", self.path)
            if profile_match:
                profile_id, action = profile_match.groups()
                result = self.manager.apply_profile(profile_id) if action == "apply" else self.manager.delete_profile(profile_id)
                self._send_json(result)
                return
            delete_match = re.fullmatch(r"/api/mods/([0-9a-f]{32})/delete", self.path)
            if delete_match:
                self._send_json(self.manager.delete_mod(delete_match.group(1)))
                return
            match = re.fullmatch(r"/api/mods/([0-9a-f]{32})/(enable|disable)", self.path)
            if match:
                result = self.manager.set_mod_enabled(
                    match.group(1),
                    enabled=match.group(2) == "enable",
                )
                self._send_json(result)
                return
            self._send_json({"error": "Unknown API endpoint."}, 404)
        except (ManagerError, OSError, ValueError) as error:
            self.close_connection = True
            self._handle_request_error(error)

    def _read_json_body(self) -> dict[str, object]:
        length = self._content_length()
        if length > 16 * 1024:
            raise ManagerError("The settings request is too large.")
        try:
            result = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as error:
            raise ManagerError("The settings request is not valid JSON.") from error
        if not isinstance(result, dict):
            raise ManagerError("The settings request is invalid.")
        return result

    def _content_length(self) -> int:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ManagerError("The request is missing its content length.")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise ManagerError("The request content length is invalid.") from error
        if length < 0:
            raise ManagerError("The request content length is invalid.")
        return length

    def _handle_game_dir(self) -> None:
        if self.headers.get_content_type() != "application/json":
            raise ManagerError("The settings request must use JSON.")
        body = self._read_json_body()
        game_dir = body.get("game_dir")
        if not isinstance(game_dir, str):
            raise ManagerError("Choose the game's installation folder.")
        self._send_json(self.manager.set_game_dir(game_dir))

    def _handle_mod_adopt(self) -> None:
        if self.headers.get_content_type() != "application/json":
            raise ManagerError("The adopt request must use JSON.")
        body = self._read_json_body()
        location = body.get("location")
        stem = body.get("stem")
        if not isinstance(location, str) or not isinstance(stem, str):
            raise ManagerError("Choose a detected mod group to adopt.")
        self._send_json(self.manager.adopt_unmanaged_mod(location, stem), 201)

    def _handle_mod_upload(self) -> None:
        if self.headers.get_content_type() != "application/octet-stream":
            raise ManagerError("Upload a single .zip mod package.")
        length = self._content_length()
        if length > MAX_MOD_SIZE:
            raise ManagerError("That package exceeds the 8 GiB upload limit.")
        filename = unquote(self.headers.get("X-Mod-Name", ""))
        kind = unquote(self.headers.get("X-Mod-Kind", MOD_KIND_OVERRIDE))
        replace_mod_id = self.headers.get("X-Replace-Mod-Id")
        try:
            skip_unsupported = self.headers.get("X-Skip-Unsupported", "").strip().casefold() == "true"
            install_dlls = self.headers.get("X-Install-Dll-Anyway", "").strip().casefold() == "true"
            result = self.manager.install_mod(
                self.rfile, filename, length, kind, replace_mod_id, skip_unsupported, install_dlls
            )
        except ManagerError:
            self.close_connection = True
            raise
        self._send_json(result, 201)

    def log_message(self, format_string: str, *args: object) -> None:
        stream = getattr(sys, "stderr", None)
        if stream is not None:
            stream.write(
                f"[{self.log_date_time_string()}] {self.address_string()} "
                f"{format_string % args}\n"
            )


def create_server(manager: ModManager, port: int = 8766) -> ThreadingHTTPServer:
    def handler_factory(*args: object, **kwargs: object) -> ManagerRequestHandler:
        return ManagerRequestHandler(*args, manager=manager, **kwargs)

    server = ThreadingHTTPServer(("127.0.0.1", port), handler_factory)
    server.daemon_threads = True
    return server


class DesktopWindowApi:
    """Native window controls exposed to the custom Steam-style title bar."""

    def __init__(self) -> None:
        self.window = None
        self.webview_module = None
        self._maximized = False

    def open_nexus(self, url: str) -> bool:
        """Open only the game's Nexus Mods pages in a separate app browser window."""
        if not isinstance(url, str):
            return False
        parsed = urlsplit(url)
        if (parsed.scheme != "https"
                or parsed.hostname not in {"www.nexusmods.com", "nexusmods.com"}
                or not parsed.path.startswith("/godzilladestroyallmonstersmeleeremastered/")):
            return False
        if self.webview_module is None:
            webbrowser.open(url)
            return True
        self.webview_module.create_window(
            "Nexus Mods — Godzilla Remastered", url,
            width=1180, height=850, min_size=(760, 600), resizable=True,
        )
        return True

    def minimize(self) -> None:
        if self.window:
            self.window.minimize()

    def toggle_maximize(self) -> bool:
        if not self.window:
            return False
        if self._maximized:
            self.window.restore()
        else:
            self.window.maximize()
        self._maximized = not self._maximized
        return self._maximized

    def close(self) -> None:
        if self.window:
            self.window.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(description="Local, reversible GZPro mod staging tool.")
    parser.add_argument(
        "--game-dir",
        type=Path,
        help="Godzilla Remastered install folder (defaults to the standard Steam path).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Local port to use (default: let Windows choose an available port).",
    )
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    try:
        manager = ModManager(game_dir=args.game_dir)
        server = create_server(manager, args.port)
    except (OSError, ManagerError) as error:
        message = f"Could not start the mod manager: {error}"
        if sys.platform == "win32" and getattr(sys, "frozen", False):
            ctypes.windll.user32.MessageBoxW(None, message, "GZPro Mod Manager", 0x10)
        elif getattr(sys, "stderr", None) is not None:
            print(message, file=sys.stderr)
        return 1

    address = f"http://127.0.0.1:{server.server_port}/"
    if getattr(sys, "stdout", None) is not None:
        print(f"GZPro Mod Manager v{APP_VERSION} is running at {address}", flush=True)
        print("Close the app window to stop the manager.", flush=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        if not args.no_browser:
            try:
                import webview
                api = DesktopWindowApi()
                api.webview_module = webview
                window = webview.create_window(
                    f"GZPro Mod Manager v{APP_VERSION}", address + "?desktop=1",
                    width=1180, height=850, min_size=(760, 600), resizable=True,
                    frameless=False, js_api=api,
                    background_color="#13191b", shadow=True,
                )
                api.window = window
                webview.start()
                return 0
            except Exception as error:
                if getattr(sys, "stdout", None) is not None:
                    print(f"Desktop window unavailable ({error}); opening the browser instead.", flush=True)
                webbrowser.open(address)
        server_thread.join()
    except KeyboardInterrupt:
        print("\nStopping GZPro Mod Manager.")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
