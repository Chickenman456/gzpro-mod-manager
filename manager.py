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
APP_VERSION = "1.8.0"
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
        r"(?:readme|install(?:ation)?|license|licence|changelog|changes|notice|credits)(?:\.[^.]+)?",
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
    def archive_dir(self) -> Path:
        return self._game_state_dir() / "mods"

    @property
    def manifest_path(self) -> Path:
        return self._game_state_dir() / "manifest.json"

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
                    or (
                        member["size"] == 0
                        and member.get("target") != "GZPro/Binaries/Win64/ue4ss/Mods/GZSkinLoader/enabled.txt"
                    )
                ):
                    raise StateError(
                        "The mod recovery manifest contains an invalid entry. No files were changed."
                    )
                if "target" not in member:
                    _safe_member_name(member["name"])
                    continue
                target = member["target"]
                skin_target = target.startswith("GZPro/Content/Paks/~mods/") if isinstance(target, str) else False
                loader_target = target in {
                    "GZPro/Binaries/Win64/ue4ss/Mods/GZSkinLoader/Scripts/main.lua",
                    "GZPro/Binaries/Win64/ue4ss/Mods/GZSkinLoader/enabled.txt",
                } if isinstance(target, str) else False
                if (
                    not isinstance(target, str)
                    or not (skin_target or loader_target)
                    or "\\" in target
                    or any(part in {"", ".", ".."} for part in target.split("/"))
                    or Path(target).name != member["name"]
                ):
                    raise StateError("The mod recovery manifest contains an invalid target path.")
                if skin_target:
                    if Path(member["name"]).suffix.casefold() not in MOD_MEMBER_SUFFIXES:
                        raise StateError("The mod recovery manifest contains an invalid skin file.")
                    _safe_mod_stem(Path(member["name"]).stem)
                elif Path(member["name"]).suffix.casefold() not in {".lua", ".txt"}:
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
        self, archive: Path
    ) -> tuple[str, list[dict[str, object]], str, str | None]:
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

                # Combined skin packages may contain a single skin-data folder and
                # the GZSkinLoader script tree. All other folder layouts are rejected.
                normalized = []
                for info in cooked_infos:
                    name = info.filename.replace(chr(92), "/")
                    while name.startswith("./"):
                        name = name[2:]
                    if not name or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/")):
                        raise ManagerError(f"Unsafe path in the package: {info.filename}")
                    normalized.append((info, name))
                has_loader = any(name.startswith("GZSkinLoader/") for _, name in normalized)
                skin_infos = []
                loader_infos = []
                for info, name in normalized:
                    parts = name.split("/")
                    if len(parts) == 1 and Path(name).suffix.casefold() in MOD_MEMBER_SUFFIXES:
                        skin_infos.append((info, name, None))
                    elif len(parts) == 2 and Path(parts[1]).suffix.casefold() in MOD_MEMBER_SUFFIXES:
                        skin_infos.append((info, name, parts[0]))
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
                    else:
                        raise ManagerError(f"Unsupported file or folder in the package: {name}")
                if has_loader and not loader_infos:
                    raise ManagerError("GZSkinLoader must include Scripts/main.lua or enabled.txt.")
                if has_loader and not any(name.casefold() == "gzskinloader/scripts/main.lua" for _, name in loader_infos):
                    raise ManagerError("GZSkinLoader must include Scripts/main.lua.")

                composite_package = has_loader or any(folder is not None for _, _, folder in skin_infos)
                skin_stem = None
                suffixes: set[str] = set()
                members: list[dict[str, object]] = []
                total_size = 0

                def add_member(info, entry, deployed_name, target=None):
                    nonlocal total_size
                    is_empty_loader_flag = entry.casefold() == "gzskinloader/enabled.txt"
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
                    if folder and current_stem.casefold() not in {
                        folder.casefold(), (folder + "_P").casefold()
                    }:
                        raise ManagerError("The skin container files must match their containing folder name.")
                    if skin_stem is None:
                        skin_stem = current_stem
                    elif current_stem != skin_stem:
                        raise ManagerError("Every skin container file must use the same mod name.")
                    if suffix in suffixes:
                        raise ManagerError(f"The package contains more than one {suffix} file.")
                    suffixes.add(suffix)
                    add_member(
                        info, entry, skin_stem + suffix,
                        f"GZPro/Content/Paks/~mods/{skin_stem}{suffix}" if composite_package else None,
                    )
                if not REQUIRED_MEMBER_SUFFIXES.issubset(suffixes):
                    raise ManagerError("The package must contain the cooked mod's .utoc and .ucas files.")
                if ".pak" not in suffixes:
                    stub_member = {
                        "entry": None, "suffix": ".pak",
                        "name": (skin_stem or "GZSkin") + ".pak",
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

                stem = skin_stem or "GZSkinLoader"
                if not skin_stem:
                    raise ManagerError("The package must include the skin's .utoc and .ucas files.")
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
            if self._existing_named_file(destination.parent, destination.name) is not None:
                raise ConflictError(f"{destination.name} already exists in {destination.parent}; it was not overwritten.")
        with zipfile.ZipFile(archive) as package:
            for member in members:
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

    def install_mod(
        self,
        stream: BinaryIO,
        filename: str,
        expected_size: int,
        kind: str = MOD_KIND_OVERRIDE,
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

                stem, members, description, thumbnail = self._read_package_members(archive_path)
                if kind == MOD_KIND_OVERRIDE and stem.casefold() in GAME_CONTAINER_STEMS:
                    raise ManagerError(
                        f"The mod name {stem} is reserved by the game itself."
                    )
                if any(str(mod["stem"]).casefold() == stem.casefold() for mod in mods):
                    raise ConflictError(
                        f"{stem} is already in this manager's library. Disable it before importing again."
                    )

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

                if kind == MOD_KIND_BLUEPRINT:
                    self._check_logic_mods_path(create=True)
                self._require_game_closed()
                self._deploy_members(archive_path, members, self._mod_target_dir({"kind": kind}))
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
                        installed.unlink()
                record["enabled"] = False

            self._write_manifest(mods)
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
            "conflict": conflict,
            "partial": 0 < installed_count < len(members),
        }

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

    def get_status(self) -> dict[str, object]:
        with self._lock:
            game_valid = (
                self.game_dir.is_dir()
                and self.game_executable.is_file()
                and self.paks_dir.is_dir()
            )
            game_running: bool | None = None
            game_check_error = None
            if game_valid:
                try:
                    game_running = self._is_game_running()
                except StateError as error:
                    game_check_error = str(error)

            mods = self._read_manifest()
            listed_mods = []
            managed_names: set[str] = set()
            for record in mods:
                for member in record["members"]:
                    managed_names.add(str(member["name"]).casefold())
                listed_mods.append(
                    self._describe_mod(record, self._archive_path(record))
                )

            unmanaged_files = []
            if self.logic_mods_dir.is_dir() and not self.logic_mods_dir.is_symlink():
                unmanaged_files.extend(
                    (path, LOGIC_MODS_DIRECTORY)
                    for path in sorted(self.logic_mods_dir.iterdir(), key=lambda item: item.name.casefold())
                    if path.is_file()
                    and not path.is_symlink()
                    and path.suffix.casefold() in MOD_MEMBER_SUFFIXES
                    and path.name.casefold() not in managed_names
                )
            if self.paks_dir.is_dir():
                unmanaged_files.extend(
                    (path, "Paks")
                    for path in sorted(self.paks_dir.iterdir(), key=lambda item: item.name.casefold())
                    if path.is_file()
                    and not path.is_symlink()
                    and path.suffix.casefold() in MOD_MEMBER_SUFFIXES
                    and path.stem.casefold() not in GAME_CONTAINER_STEMS
                    and path.name.casefold() not in managed_names
                )

            unmanaged_by_group: dict[tuple[str, str], dict[str, object]] = {}
            for path, location in unmanaged_files:
                key = (location, path.stem.casefold())
                group = unmanaged_by_group.setdefault(
                    key,
                    {
                        "stem": path.stem,
                        "location": location,
                        "files": [],
                        "size": 0,
                    },
                )
                group["files"].append(path.name)
                group["size"] += path.stat().st_size
            unmanaged = sorted(
                unmanaged_by_group.values(),
                key=lambda group: (str(group["location"]).casefold(), str(group["stem"]).casefold()),
            )

            binaries_dir = self.game_executable.parent
            ue4ss_installed = game_valid and all(
                (binaries_dir / relative).is_file() for relative in UE4SS_RELATIVE_FILES
            )

            return {
                "game_dir": str(self.game_dir),
                "game_valid": game_valid,
                "game_executable": str(self.game_executable),
                "paks_dir": str(self.paks_dir),
                "logic_mods_dir": str(self.logic_mods_dir),
                "logic_mods_exists": self.logic_mods_dir.is_dir(),
                "game_running": game_running,
                "game_check_error": game_check_error,
                "mods": listed_mods,
                "unmanaged_mods": unmanaged,
                "loader_status": (
                    "verified"
                    if game_valid
                    else "unknown"
                ),
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
            "try {\n"
            "  while (Get-Process -Id $managerProcessId -ErrorAction SilentlyContinue) { Start-Sleep -Seconds 1 }\n"
            "  Copy-Item -LiteralPath $source -Destination $staged -Force\n"
            "  if (!(Test-Path -LiteralPath $staged) -or (Get-Item -LiteralPath $staged).Length -lt 1024) { throw 'The staged update file is missing or incomplete.' }\n"
            "  try { [System.IO.File]::Replace($staged, $target, $null) } catch { Move-Item -LiteralPath $staged -Destination $target -Force }\n"
            "  Start-Process -FilePath $target\n"
            "  Add-Content -LiteralPath $logPath -Value ('Installed ' + (Get-Date -Format o))\n"
            "} catch {\n"
            "  Add-Content -LiteralPath $logPath -Value ((Get-Date -Format o) + ' ' + $_.Exception.Message)\n"
            "  Remove-Item -LiteralPath $staged -Force -ErrorAction SilentlyContinue\n"
            "  if (Test-Path -LiteralPath $target) { Start-Process -FilePath $target }\n"
            "} finally {\n"
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
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self' https://api.github.com; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
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
            if self.path == "/api/game-dir":
                self._handle_game_dir()
                return
            if self.path == "/api/mods":
                self._handle_mod_upload()
                return
            if self.path == "/api/mods/adopt":
                self._handle_mod_adopt()
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
        try:
            result = self.manager.install_mod(self.rfile, filename, length, kind)
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
        self._maximized = False
        self._wndproc_callback = None

    def enable_resize(self) -> None:
        """Add Windows hit testing so the custom title bar can move and resize."""
        if not self.window or sys.platform != "win32" or self._wndproc_callback:
            return
        try:
            user32 = ctypes.windll.user32
            raw_handle = self.window.native.Handle
            hwnd = int(raw_handle.ToInt64()) if hasattr(raw_handle, "ToInt64") else int(raw_handle)
            get_long = user32.GetWindowLongPtrW
            get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
            get_long.restype = ctypes.c_ssize_t
            set_long = user32.SetWindowLongPtrW
            set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
            set_long.restype = ctypes.c_ssize_t
            call_proc = user32.CallWindowProcW
            call_proc.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
            call_proc.restype = ctypes.c_ssize_t
            get_rect = user32.GetWindowRect
            get_rect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            get_rect.restype = ctypes.c_int

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            old_proc = get_long(hwnd, -4)
            # WS_THICKFRAME lets Windows complete the resize system command
            # after our WM_NCHITTEST returns an edge or corner code.
            style = get_long(hwnd, -16)
            set_long(hwnd, -16, style | 0x00040000)
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0027)
            edge = 8
            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)

            def wndproc(window_handle, message, wparam, lparam):
                if message == 0x0084:
                    rect = RECT()
                    if get_rect(window_handle, ctypes.byref(rect)):
                        packed = ctypes.c_uint32(lparam).value
                        x = ctypes.c_short(packed & 0xFFFF).value
                        y = ctypes.c_short((packed >> 16) & 0xFFFF).value
                        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
                        if not self._maximized:
                            at_left, at_right = x < left + edge, x >= right - edge
                            at_top, at_bottom = y < top + edge, y >= bottom - edge
                            if at_top and at_left: return 13
                            if at_top and at_right: return 14
                            if at_bottom and at_left: return 16
                            if at_bottom and at_right: return 17
                            if at_left: return 10
                            if at_right: return 11
                            if at_top: return 12
                            if at_bottom: return 15
                        if top <= y < top + 38 and x < right - 138:
                            return 2
                return call_proc(old_proc, window_handle, message, wparam, lparam)

            self._wndproc_callback = WNDPROC(wndproc)
            set_long(hwnd, -4, ctypes.cast(self._wndproc_callback, ctypes.c_void_p).value)
        except Exception:
            self._wndproc_callback = None

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
                window = webview.create_window(
                    f"GZPro Mod Manager v{APP_VERSION}", address + "?desktop=1",
                    width=1180, height=850, min_size=(760, 600), resizable=True,
                    frameless=True, easy_drag=False, js_api=api,
                    background_color="#13191b", shadow=True,
                )
                api.window = window
                window.events.before_show += api.enable_resize
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
