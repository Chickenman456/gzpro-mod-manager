"""Audio replacer for the Wwise .wem sounds stored in the game's .pak files.

Pipeline:
  scan     list .wem/.bnk files in the game's own .pak files (repak)
  preview  extract a .wem (repak) and decode it to WAV (vgmstream)
  replace  decode the user's audio to WAV (vgmstream), then encode a .wem in
           the same codec as the original: Wwise PCM is written directly,
           anything else (usually Vorbis) goes through Audiokinetic's
           WwiseConsole, because Wwise picks the decoder from the sound bank,
           not from the .wem header.
  build    pack every staged replacement into <Name>_P.pak (repak) with the
           original paths, zip it with a marker file, and hand the zip to the
           regular mod installer.

The external tools are bundled in the Windows build under tools/. For
development, set GZPRO_REPAK, GZPRO_VGMSTREAM or GZPRO_WWISECONSOLE, or put
the tools on PATH.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import uuid
import wave
import zipfile
from pathlib import Path

RESOURCE_DIR = Path(__file__).resolve().parent
AUDIO_MOD_MARKER = "gzpro-audio-mod.json"
INPUT_SUFFIXES = frozenset({".wav", ".mp3", ".ogg", ".flac", ".wem"})
MAX_INPUT_SIZE = 200 * 1024 * 1024
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,47}")
WWISE_DOWNLOAD_URL = "https://www.audiokinetic.com/en/download/"


class AudioError(Exception):
    status_code = 400


def _windows() -> bool:
    return sys.platform == "win32"


def _find_tool(env_name: str, bundled: list[str], path_names: list[str]) -> Path | None:
    override = os.environ.get(env_name)
    if override and Path(override).is_file():
        return Path(override)
    for relative in bundled:
        candidate = RESOURCE_DIR / relative
        if candidate.is_file():
            return candidate
    for name in path_names:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def find_repak() -> Path | None:
    return _find_tool("GZPRO_REPAK", ["tools/repak.exe", "tools/repak"], ["repak"])


def find_retoc() -> Path | None:
    return _find_tool("GZPRO_RETOC", ["tools/retoc.exe", "tools/retoc"], ["retoc"])


AUDIO_HINTS = ("wwise", "audio", "sound", "music", "bgm", "voice", "sfx", "akaudio", ".wem", ".bnk")


def _summarize_utoc(retoc: Path, utoc: Path) -> dict[str, object]:
    """List a .utoc's directory index (no decompression) and summarize it."""
    code, out, err = _run([retoc, "list", "--size", "--path", utoc], timeout=300)
    if code != 0:
        return {"name": utoc.name, "error": (err or out).strip()[:300] or "could not be listed"}
    chunk_types: dict[str, int] = {}
    extensions: dict[str, int] = {}
    audio_paths: list[str] = []
    audio_count = 0
    total = 0
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        _, _, chunk_type, size, path = parts
        total += 1
        chunk_types[chunk_type] = chunk_types.get(chunk_type, 0) + 1
        if path == "-":
            continue
        suffix = Path(path).suffix.casefold() or "(no extension)"
        extensions[suffix] = extensions.get(suffix, 0) + 1
        if any(hint in path.casefold() for hint in AUDIO_HINTS):
            audio_count += 1
            if len(audio_paths) < 40:
                audio_paths.append(f"{path} ({chunk_type}, {size.strip()} bytes)")
    top = lambda values: sorted(values.items(), key=lambda item: -item[1])[:10]
    return {
        "name": utoc.name,
        "chunks": total,
        "chunk_types": top(chunk_types),
        "types": top(extensions),
        "audio_like": audio_count,
        "audio_samples": audio_paths,
    }


def find_vgmstream() -> Path | None:
    return _find_tool(
        "GZPRO_VGMSTREAM",
        ["tools/vgmstream/vgmstream-cli.exe", "tools/vgmstream/vgmstream-cli"],
        ["vgmstream-cli"],
    )


def find_wwise_console() -> Path | None:
    override = os.environ.get("GZPRO_WWISECONSOLE")
    if override and Path(override).is_file():
        return Path(override)
    candidates: list[str] = []
    root = os.environ.get("WWISEROOT")
    if root:
        candidates.append(os.path.join(root, "Authoring", "x64", "Release", "bin", "WwiseConsole.exe"))
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            candidates.extend(
                sorted(
                    glob.glob(os.path.join(base, "Audiokinetic", "*", "Authoring", "x64", "Release", "bin", "WwiseConsole.exe")),
                    reverse=True,
                )
            )
    for candidate in candidates:
        if Path(candidate).is_file():
            return Path(candidate)
    found = shutil.which("WwiseConsole")
    return Path(found) if found else None


def _run(args: list[str], timeout: float, stdout_path: Path | None = None) -> tuple[int, str, str]:
    """Run a tool with file-backed output so a stuck pipe can never hang the app."""
    with tempfile.TemporaryFile() as err_file:
        out_handle = open(stdout_path, "wb") if stdout_path else tempfile.TemporaryFile()
        try:
            try:
                process = subprocess.Popen(
                    [str(arg) for arg in args],
                    stdin=subprocess.DEVNULL,
                    stdout=out_handle,
                    stderr=err_file,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as error:
                raise AudioError(f"Could not start {Path(args[0]).name}: {error}") from error
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                raise AudioError(f"{Path(args[0]).name} took too long and was stopped.")
            out_text = ""
            if not stdout_path:
                out_handle.seek(0)
                out_text = out_handle.read().decode("utf-8", errors="replace")
            err_file.seek(0)
            return code, out_text, err_file.read().decode("utf-8", errors="replace")
        finally:
            out_handle.close()


def _check_id(entry_id: object) -> str:
    if not isinstance(entry_id, str) or not re.fullmatch(r"[0-9a-f]{20}", entry_id):
        raise AudioError("Choose a sound from the list.")
    return entry_id


def _entry_id(pak_name: str, path: str) -> str:
    return hashlib.sha1(f"{pak_name.casefold()}\0{path.casefold()}".encode("utf-8")).hexdigest()[:20]


def write_pcm_wem(wav_path: Path, wem_path: Path) -> None:
    """Write a 16-bit Wwise PCM .wem (fmt 0xFFFE with Wwise's channel config)."""
    with wave.open(str(wav_path), "rb") as source:
        channels = source.getnchannels()
        rate = source.getframerate()
        width = source.getsampwidth()
        frames = source.readframes(source.getnframes())
    if width != 2:
        raise AudioError("Only 16-bit audio can be written as Wwise PCM.")
    if channels not in (1, 2):
        raise AudioError("Use a mono or stereo audio file.")
    channel_mask = 0x4 if channels == 1 else 0x3
    channel_config = channels | (1 << 8) | (channel_mask << 12)
    block_align = channels * 2
    fmt = struct.pack(
        "<HHIIHHHHI", 0xFFFE, channels, rate, rate * block_align, block_align, 16, 6, 0, channel_config
    )
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(frames)) + frames
    if len(frames) % 2:
        body += b"\0"
    wem_path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


class AudioWorkspace:
    def __init__(self, state_dir: Path) -> None:
        self.root = state_dir / "audio"
        self.cache_dir = self.root / "cache"
        self.draft_dir = self.root / "draft"
        self.wwise_dir = self.root / "wwise-project"
        self.index_file = self.root / "scan-index.json"
        self.draft_file = self.draft_dir / "draft.json"
        self._lock = threading.RLock()
        self._entries: dict[str, dict[str, object]] = {}

    # ---------- tools ----------
    def tool_status(self) -> dict[str, object]:
        wwise = find_wwise_console()
        return {
            "repak": bool(find_repak()),
            "vgmstream": bool(find_vgmstream()),
            "retoc": bool(find_retoc()),
            "wwise": bool(wwise),
            "wwise_path": str(wwise) if wwise else "",
            "wwise_download_url": WWISE_DOWNLOAD_URL,
        }

    def _require(self, tool: Path | None, name: str) -> Path:
        if tool is None:
            raise AudioError(f"{name} is missing from this build of the manager. Reinstall the latest release.")
        return tool

    # ---------- scan ----------
    def scan(self, paks_dir: Path, refresh: bool = False, exclude: set[str] | None = None) -> dict[str, object]:
        """Scan game archives. ``exclude`` holds casefolded file names of installed mods."""
        exclude = exclude or set()
        repak = self._require(find_repak(), "repak")
        if not paks_dir.is_dir():
            raise AudioError(f"The game's Paks folder is missing: {paks_dir}")
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            cache: dict[str, object] = {}
            if not refresh and self.index_file.is_file():
                try:
                    cache = json.loads(self.index_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    cache = {}
            new_cache: dict[str, object] = {}
            entries: dict[str, dict[str, object]] = {}
            errors: list[str] = []
            banks = 0
            def game_archive(path: Path) -> bool:
                relative = path.relative_to(paks_dir)
                return (
                    not any(part.casefold() in {"~mods", "logicmods"} for part in relative.parts[:-1])
                    and path.name.casefold() not in exclude
                )

            paks = sorted(path for path in paks_dir.rglob("*.pak") if game_archive(path))
            for pak in paks:
                stat = pak.stat()
                key = str(pak.relative_to(paks_dir))
                signature = [stat.st_size, stat.st_mtime_ns]
                cached = cache.get(key) if isinstance(cache, dict) else None
                if isinstance(cached, dict) and cached.get("signature") == signature and cached.get("v") == 2:
                    record = cached
                else:
                    record = self._scan_pak(repak, pak, signature, errors)
                new_cache[key] = record
                for path in record.get("files", []):
                    suffix = Path(path).suffix.casefold()
                    if suffix == ".bnk":
                        banks += 1
                        continue
                    entry_id = _entry_id(key, path)
                    entries[entry_id] = {
                        "id": entry_id,
                        "pak": key,
                        "path": path,
                        "name": Path(path).name,
                        "folder": str(Path(path).parent).replace("\\", "/"),
                    }
            self._entries = entries
            self._paks_dir = paks_dir
            pak_summary = [
                {"name": key, "files": record.get("count", 0), "types": record.get("types", [])}
                for key, record in new_cache.items()
                if isinstance(record, dict) and not key.startswith("utoc:")
            ]
            utocs = [path for path in sorted(paks_dir.rglob("*.utoc")) if game_archive(path)]
            containers = [str(path.relative_to(paks_dir)) for path in utocs]
            retoc = find_retoc()
            utoc_summary = []
            if retoc is not None:
                for utoc in utocs:
                    if utoc.stem.casefold() == "global":
                        continue
                    stat = utoc.stat()
                    key = "utoc:" + str(utoc.relative_to(paks_dir))
                    signature = [stat.st_size, stat.st_mtime_ns]
                    cached = cache.get(key) if isinstance(cache, dict) else None
                    if isinstance(cached, dict) and cached.get("signature") == signature and cached.get("v") == 2:
                        summary = cached["summary"]
                    else:
                        summary = _summarize_utoc(retoc, utoc)
                    new_cache[key] = {"v": 2, "signature": signature, "summary": summary}
                    utoc_summary.append(summary)
            try:
                self.index_file.write_text(json.dumps(new_cache), encoding="utf-8")
            except OSError:
                pass
            return {
                "pak_summary": pak_summary,
                "utoc_containers": containers,
                "utoc_summary": utoc_summary,
                "retoc": retoc is not None,
                "files": sorted(entries.values(), key=lambda item: (str(item["folder"]).casefold(), str(item["name"]).casefold())),
                "banks": banks,
                "paks_scanned": len(paks),
                "errors": errors,
                "tools": self.tool_status(),
                "draft": self.draft(),
            }

    def _scan_pak(self, repak: Path, pak: Path, signature: list[int], errors: list[str]) -> dict[str, object]:
        code, out, err = _run([repak, "info", pak], timeout=60)
        if code != 0:
            errors.append(f"{pak.name}: {(err or out).strip()[:200] or 'could not be read'}")
            return {"v": 2, "signature": signature, "files": []}
        if re.search(r"encrypted index:\s*true", out, re.IGNORECASE):
            errors.append(f"{pak.name}: encrypted, skipped")
            return {"v": 2, "signature": signature, "files": []}
        code, out, err = _run([repak, "list", pak], timeout=300)
        if code != 0:
            errors.append(f"{pak.name}: {(err or out).strip()[:200] or 'could not be listed'}")
            return {"v": 2, "signature": signature, "files": []}
        listed = [line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()]
        files = [line for line in listed if line.casefold().endswith((".wem", ".bnk"))]
        types: dict[str, int] = {}
        for line in listed:
            suffix = Path(line).suffix.casefold() or "(no extension)"
            types[suffix] = types.get(suffix, 0) + 1
        top = sorted(types.items(), key=lambda item: -item[1])[:8]
        return {"v": 2, "signature": signature, "files": files, "count": len(listed), "types": top}

    def _entry(self, entry_id: str) -> dict[str, object]:
        entry = self._entries.get(_check_id(entry_id))
        if entry is None:
            raise AudioError("That sound is no longer in the list. Scan the game's audio again.")
        return entry

    # ---------- extract / decode ----------
    def _original_wem(self, entry: dict[str, object]) -> Path:
        repak = self._require(find_repak(), "repak")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self.cache_dir / f"{entry['id']}.wem"
        if target.is_file() and target.stat().st_size > 0:
            return target
        partial = target.with_suffix(".part")
        code, _, err = _run([repak, "get", self._paks_dir / str(entry["pak"]), entry["path"]], timeout=120, stdout_path=partial)
        if code != 0 or not partial.is_file() or partial.stat().st_size == 0:
            partial.unlink(missing_ok=True)
            raise AudioError(f"Could not extract {entry['name']}: {err.strip()[:200]}")
        partial.replace(target)
        return target

    def _decode(self, source: Path, wav_path: Path, ignore_loops: bool = True) -> Path:
        vgmstream = self._require(find_vgmstream(), "vgmstream")
        args = [vgmstream, "-o", wav_path]
        if ignore_loops:
            args.append("-i")
        args.append(source)
        code, out, err = _run(args, timeout=180)
        if code != 0 or not wav_path.is_file():
            wav_path.unlink(missing_ok=True)
            raise AudioError(f"Could not decode {source.name}: {(err or out).strip()[:200] or 'unsupported format'}")
        return wav_path

    def _info(self, source: Path) -> dict[str, object]:
        vgmstream = self._require(find_vgmstream(), "vgmstream")
        code, out, err = _run([vgmstream, "-I", "-m", source], timeout=60)
        if code != 0:
            raise AudioError(f"Could not read {source.name}: {(err or out).strip()[:200]}")
        start = out.find("{")
        try:
            return json.loads(out[start:]) if start >= 0 else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _is_pcm(info: dict[str, object]) -> bool:
        encoding = str(info.get("encoding", "")).casefold()
        return "pcm" in encoding and "adpcm" not in encoding

    def preview(self, entry_id: str) -> Path:
        with self._lock:
            entry = self._entry(entry_id)
            wav = self.cache_dir / f"{entry_id}.wav"
            if wav.is_file():
                return wav
            return self._decode(self._original_wem(entry), wav)

    def details(self, entry_id: str) -> dict[str, object]:
        with self._lock:
            entry = self._entry(entry_id)
            info = self._info(self._original_wem(entry))
            sample_rate = info.get("sampleRate") or 0
            samples = info.get("numberOfSamples") or 0
            return {
                **entry,
                "encoding": info.get("encoding", "unknown"),
                "channels": info.get("channels"),
                "sample_rate": sample_rate,
                "seconds": round(samples / sample_rate, 2) if sample_rate else None,
                "needs_wwise": not self._is_pcm(info),
            }

    def export(self, entry_id: str, export_dir: Path) -> Path:
        with self._lock:
            entry = self._entry(entry_id)
            export_dir.mkdir(parents=True, exist_ok=True)
            wav = self.preview(entry_id)
            target = export_dir / (Path(str(entry["name"])).stem + ".wav")
            shutil.copyfile(wav, target)
            shutil.copyfile(self._original_wem(entry), export_dir / str(entry["name"]))
            return target

    # ---------- replace ----------
    def replace(self, entry_id: str, stream, length: int, filename: str) -> dict[str, object]:
        suffix = Path(filename or "").suffix.casefold()
        if suffix not in INPUT_SUFFIXES:
            raise AudioError("Use a WAV, MP3, OGG, FLAC or WEM file.")
        if length <= 0 or length > MAX_INPUT_SIZE:
            raise AudioError("The audio file must be between 1 byte and 200 MB.")
        with self._lock:
            entry = self._entry(entry_id)
            original = self._original_wem(entry)
            original_info = self._info(original)
            work = Path(tempfile.mkdtemp(prefix="gzpro-audio-"))
            try:
                upload = work / ("input" + suffix)
                remaining = length
                with open(upload, "wb") as handle:
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise AudioError("The upload was interrupted.")
                        handle.write(chunk)
                        remaining -= len(chunk)
                self.draft_dir.mkdir(parents=True, exist_ok=True)
                result = self.draft_dir / f"{entry_id}.wem"
                if suffix == ".wem":
                    if upload.read_bytes()[:4] != b"RIFF":
                        raise AudioError("That .wem file is not a valid Wwise audio file.")
                    self._info(upload)
                    shutil.copyfile(upload, result)
                    method = "your .wem"
                else:
                    wav = self._decode(upload, work / "decoded.wav")
                    if self._is_pcm(original_info):
                        write_pcm_wem(wav, result)
                        method = "Wwise PCM"
                    else:
                        self._wwise_convert(wav, result)
                        method = "Wwise Vorbis"
                self._info(result)
                draft = self._read_draft()
                draft = [item for item in draft if item["id"] != entry_id]
                draft.append({**entry, "source": Path(filename).name, "method": method})
                self._write_draft(draft)
                return {"draft": draft}
            finally:
                shutil.rmtree(work, ignore_errors=True)

    def _wwise_convert(self, wav: Path, target: Path) -> None:
        console = find_wwise_console()
        if console is None:
            raise AudioError(
                "WWISE_REQUIRED:This sound uses Wwise's compressed format, so converting it needs "
                "Audiokinetic Wwise (free) installed on this PC. Install it, then try again. "
                "You can also convert the file to .wem yourself and choose that file instead."
            )
        project = self.wwise_dir / "GZProAudio" / "GZProAudio.wproj"
        if not project.is_file():
            self.wwise_dir.mkdir(parents=True, exist_ok=True)
            code, out, err = _run([console, "create-new-project", project, "--platform", "Windows", "--quiet"], timeout=300)
            if code != 0 or not project.is_file():
                raise AudioError(f"Wwise could not create its conversion project: {(err or out).strip()[:300]}")
        work = Path(tempfile.mkdtemp(prefix="gzpro-wwise-"))
        try:
            source = work / "source.wav"
            shutil.copyfile(wav, source)
            sources = work / "list.wsources"
            sources.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<ExternalSourcesList SchemaVersion="1" Root="{work}">\n'
                '  <Source Path="source.wav" Conversion="Vorbis Quality High" Destination="converted"/>\n'
                "</ExternalSourcesList>\n",
                encoding="utf-8",
            )
            output = work / "out"
            code, out, err = _run(
                [console, "convert-external-source", project, "--platform", "Windows",
                 "--source-file", sources, "--output", output, "--quiet"],
                timeout=600,
            )
            converted = sorted(output.rglob("*.wem")) if output.is_dir() else []
            if code != 0 or not converted:
                raise AudioError(f"Wwise could not convert the audio: {(err or out).strip()[:300] or 'no .wem was produced'}")
            shutil.copyfile(converted[0], target)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ---------- draft ----------
    def _read_draft(self) -> list[dict[str, object]]:
        try:
            data = json.loads(self.draft_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [item for item in data if isinstance(item, dict) and (self.draft_dir / f"{item.get('id')}.wem").is_file()] if isinstance(data, list) else []

    def _write_draft(self, draft: list[dict[str, object]]) -> None:
        self.draft_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.draft_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(draft, indent=2), encoding="utf-8")
        temporary.replace(self.draft_file)

    def draft(self) -> list[dict[str, object]]:
        with self._lock:
            return self._read_draft()

    def remove_from_draft(self, entry_id: str) -> dict[str, object]:
        _check_id(entry_id)
        with self._lock:
            draft = [item for item in self._read_draft() if item["id"] != entry_id]
            (self.draft_dir / f"{entry_id}.wem").unlink(missing_ok=True)
            (self.draft_dir / f"{entry_id}.wav").unlink(missing_ok=True)
            self._write_draft(draft)
            return {"draft": draft}

    def draft_preview(self, entry_id: str) -> Path:
        _check_id(entry_id)
        with self._lock:
            if not any(item["id"] == entry_id for item in self._read_draft()):
                raise AudioError("That replacement is no longer staged.")
            wav = self.draft_dir / f"{entry_id}.wav"
            if not wav.is_file():
                self._decode(self.draft_dir / f"{entry_id}.wem", wav)
            return wav

    def build_mod_zip(self, name: str, output_dir: Path) -> Path:
        """Pack every staged replacement into <name>_P.pak and zip it for the installer."""
        repak = self._require(find_repak(), "repak")
        if not isinstance(name, str) or not SAFE_NAME.fullmatch(name.strip()):
            raise AudioError("Name the mod with letters, numbers, spaces, dashes or underscores (up to 48 characters).")
        stem = re.sub(r"[ .]+", "_", name.strip())
        with self._lock:
            draft = self._read_draft()
            if not draft:
                raise AudioError("Replace at least one sound first.")
            work = Path(tempfile.mkdtemp(prefix="gzpro-audio-build-"))
            try:
                content = work / f"{stem}_P"
                for item in draft:
                    parts = str(item["path"]).split("/")
                    if any(part in {"", ".", ".."} for part in parts):
                        raise AudioError(f"Unsafe sound path: {item['path']}")
                    destination = content.joinpath(*parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(self.draft_dir / f"{item['id']}.wem", destination)
                pak = work / f"{stem}_P.pak"
                code, out, err = _run(
                    [repak, "pack", "--version", "V11", "--mount-point", "../../../", "--quiet", content, pak],
                    timeout=600,
                )
                if code != 0 or not pak.is_file():
                    raise AudioError(f"Could not build the mod .pak: {(err or out).strip()[:300]}")
                output_dir.mkdir(parents=True, exist_ok=True)
                archive = output_dir / f"{stem}.zip"
                lines = [f"Audio mod made with GZPro Mod Manager's Audio tab.", "", "Replaced sounds:"]
                lines += [f"- {item['path']} (from {item['source']})" for item in draft]
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
                    package.write(pak, pak.name)
                    package.writestr(AUDIO_MOD_MARKER, json.dumps({"format": 1, "pak": pak.name, "files": [item["path"] for item in draft]}, indent=2))
                    package.writestr("INFO.txt", "\n".join(lines))
                return archive
            finally:
                shutil.rmtree(work, ignore_errors=True)

    def clear_draft(self) -> None:
        with self._lock:
            shutil.rmtree(self.draft_dir, ignore_errors=True)
