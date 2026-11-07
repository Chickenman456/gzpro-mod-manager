from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from manager import ConflictError, ManagerError, ModManager, create_server


def make_package(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        for name, data in members.items():
            package.writestr(name, data)
    return buffer.getvalue()


def default_members(stem: str = "TestMod") -> dict[str, bytes]:
    return {
        f"{stem}.utoc": b"cooked toc",
        f"{stem}.ucas": b"cooked container",
    }


class ModManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.game_dir = self.root / "game"
        self.paks_dir = self.game_dir / "GZPro" / "Content" / "Paks"
        self.paks_dir.mkdir(parents=True)
        executable = self.game_dir / "GZPro" / "Binaries" / "Win64" / "GZProSteam-Win64-Shipping.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"test executable")
        self.original_archive = self.paks_dir / "GZPro-Windows.pak"
        self.original_archive.write_bytes(b"untouched game archive")
        self.manager = ModManager(
            game_dir=self.game_dir,
            state_base=self.root / "manager-state",
        )
        self.manager._is_game_running = lambda: False

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def install(
        self,
        filename: str = "TestMod.zip",
        payload: bytes | None = None,
        kind: str = "blueprint",
    ):
        if payload is None:
            payload = make_package(default_members())
        return self.manager.install_mod(io.BytesIO(payload), filename, len(payload), kind)

    def deployed(self, stem: str = "TestMod", kind: str = "blueprint") -> list[Path]:
        if kind == "override":
            base = self.paks_dir
            stem = f"GZPro-{stem}"
        else:
            base = self.paks_dir / "LogicMods"
        return [base / f"{stem}{suffix}" for suffix in (".utoc", ".ucas", ".pak")]



    def test_open_mod_location_launches_explorer_for_paks_directory(self) -> None:
        from unittest.mock import patch
        import manager as manager_module

        with patch.object(manager_module.sys, "platform", "win32"), patch.object(
            manager_module.subprocess, "Popen"
        ) as popen:
            result = self.manager.open_mod_location()

        self.assertTrue(result["opened"])
        self.assertTrue(Path(result["path"]).samefile(self.paks_dir))
        self.assertEqual(popen.call_args.args[0][0], "explorer.exe")
        self.assertTrue(Path(popen.call_args.args[0][1]).samefile(self.paks_dir))

    def test_mod_display_name_can_be_renamed_without_changing_deployed_files(self) -> None:
        mod = self.install()
        renamed = self.manager.rename_mod(mod["id"], "My Preferred Name")
        status = self.manager.get_status()
        saved = next(item for item in status["mods"] if item["id"] == mod["id"])
        self.assertEqual(renamed["display_name"], "My Preferred Name")
        self.assertEqual(saved["display_name"], "My Preferred Name")
        self.assertEqual(self.deployed()[0].read_bytes(), b"cooked toc")

    def test_duplicate_import_offers_replace_and_replacement_keeps_mod_id(self) -> None:
        original = self.install()
        updated_payload = make_package({
            "TestMod.utoc": b"updated toc",
            "TestMod.ucas": b"updated container",
        })
        with self.assertRaises(ConflictError) as raised:
            self.manager.install_mod(io.BytesIO(updated_payload), "TestMod-v2.zip", len(updated_payload), "blueprint")
        self.assertTrue(str(raised.exception).startswith("MOD_REPLACE_REQUIRED:"))
        replaced = self.manager.install_mod(
            io.BytesIO(updated_payload), "TestMod-v2.zip", len(updated_payload), "blueprint", original["id"]
        )
        self.assertEqual(replaced["id"], original["id"])
        self.assertEqual(self.deployed()[0].read_bytes(), b"updated toc")
        self.assertEqual(self.deployed()[1].read_bytes(), b"updated container")
        self.assertEqual(len(self.manager.get_status()["mods"]), 1)

    def test_combined_burning_godzilla_package_installs_skin_and_ue4ss_loader(self) -> None:
        payload = make_package({
            "README.txt": b"Install both components",
            "HeiseiBurningSkin/HeiseiBurningSkin_P.utoc": b"toc",
            "HeiseiBurningSkin/HeiseiBurningSkin_P.ucas": b"cas",
            "GZSkinLoader/Scripts/main.lua": b"print('loader')",
            "GZSkinLoader/enabled.txt": b"",
        })
        mod = self.install("BurningGodzilla.zip", payload, kind="override")
        paks_mods = self.paks_dir / "~mods"
        loader = self.game_dir / "GZPro" / "Binaries" / "Win64" / "ue4ss" / "Mods" / "GZSkinLoader"
        self.assertEqual((paks_mods / "HeiseiBurningSkin_P.utoc").read_bytes(), b"toc")
        self.assertEqual((paks_mods / "HeiseiBurningSkin_P.ucas").read_bytes(), b"cas")
        self.assertTrue((paks_mods / "HeiseiBurningSkin_P.pak").is_file())
        self.assertEqual((loader / "Scripts" / "main.lua").read_bytes(), b"print('loader')")
        self.assertEqual((loader / "enabled.txt").read_bytes(), b"")
        self.assertEqual(mod["file_count"], 5)
        disabled = self.manager.set_mod_enabled(str(mod["id"]), False)
        self.assertFalse(disabled["enabled"])
        self.assertFalse((paks_mods / "HeiseiBurningSkin_P.utoc").exists())
        self.assertFalse((loader / "Scripts" / "main.lua").exists())
        self.assertTrue((self.manager.archive_dir / f"{mod['id']}.zip").is_file())
        enabled = self.manager.set_mod_enabled(str(mod["id"]), True)
        self.assertTrue(enabled["enabled"])
        self.assertEqual((loader / "Scripts" / "main.lua").read_bytes(), b"print('loader')")
        self.manager.delete_mod(str(mod["id"]))
        self.assertFalse((paks_mods / "HeiseiBurningSkin_P.ucas").exists())
        self.assertFalse((loader / "enabled.txt").exists())

    def test_game_root_ue4ss_character_package_installs_and_removes_reversibly(self) -> None:
        payload = make_package({
            "GZPro/Binaries/Win64/dwmapi.dll": b"proxy loader",
            "GZPro/Binaries/Win64/ue4ss/UE4SS.dll": b"UE4SS core",
            "GZPro/Binaries/Win64/ue4ss/UE4SS-settings.ini": b"[Settings]",
            "GZPro/Binaries/Win64/ue4ss/Mods/CharacterPack/enabled.txt": b"",
            "GZPro/Binaries/Win64/ue4ss/Mods/CharacterPack/Scripts/main.lua": b"print('character mod')",
            "GZPro/Content/Paks/~mods/CharacterPack_P.utoc": b"toc",
            "GZPro/Content/Paks/~mods/CharacterPack_P.ucas": b"cas",
        })
        mod = self.install("CharacterPack.zip", payload, kind="override")
        binaries = self.game_dir / "GZPro" / "Binaries" / "Win64"
        paks_mods = self.paks_dir / "~mods"
        loader_mod = binaries / "ue4ss" / "Mods" / "CharacterPack"

        self.assertTrue(mod["requires_ue4ss"])
        self.assertEqual((binaries / "dwmapi.dll").read_bytes(), b"proxy loader")
        self.assertEqual((binaries / "ue4ss" / "UE4SS.dll").read_bytes(), b"UE4SS core")
        self.assertEqual((loader_mod / "Scripts" / "main.lua").read_bytes(), b"print('character mod')")
        self.assertEqual((paks_mods / "CharacterPack_P.utoc").read_bytes(), b"toc")
        self.assertTrue(self.manager.get_status()["ue4ss_installed"])

        self.manager.set_mod_enabled(str(mod["id"]), False)
        self.assertFalse((binaries / "dwmapi.dll").exists())
        self.assertFalse((loader_mod / "Scripts" / "main.lua").exists())
        self.assertFalse((paks_mods / "CharacterPack_P.utoc").exists())

        self.manager.set_mod_enabled(str(mod["id"]), True)
        self.assertEqual((binaries / "dwmapi.dll").read_bytes(), b"proxy loader")
        self.assertEqual((loader_mod / "Scripts" / "main.lua").read_bytes(), b"print('character mod')")

        self.manager.delete_mod(str(mod["id"]))
        self.assertFalse((binaries / "dwmapi.dll").exists())
        self.assertFalse((binaries / "ue4ss" / "UE4SS.dll").exists())
        self.assertFalse((loader_mod / "enabled.txt").exists())
        self.assertFalse((paks_mods / "CharacterPack_P.ucas").exists())

    def test_character_package_reuses_identical_existing_ue4ss_runtime(self) -> None:
        binaries = self.game_dir / "GZPro" / "Binaries" / "Win64"
        ue4ss = binaries / "ue4ss"
        ue4ss.mkdir(parents=True)
        (binaries / "dwmapi.dll").write_bytes(b"proxy loader")
        (ue4ss / "UE4SS.dll").write_bytes(b"UE4SS core")
        (ue4ss / "UE4SS-settings.ini").write_bytes(b"[Settings]")

        payload = make_package({
            "GZPro/Binaries/Win64/dwmapi.dll": b"proxy loader",
            "GZPro/Binaries/Win64/ue4ss/UE4SS.dll": b"UE4SS core",
            "GZPro/Binaries/Win64/ue4ss/UE4SS-settings.ini": b"[Settings]",
            "GZPro/Binaries/Win64/ue4ss/Mods/CharacterPack/enabled.txt": b"",
            "GZPro/Binaries/Win64/ue4ss/Mods/CharacterPack/Scripts/main.lua": b"print('character mod')",
        })
        mod = self.install("CharacterPack.zip", payload, kind="override")
        self.assertTrue(mod["requires_ue4ss"])
        self.assertEqual((binaries / "dwmapi.dll").read_bytes(), b"proxy loader")

        self.manager.set_mod_enabled(str(mod["id"]), False)
        self.assertTrue((binaries / "dwmapi.dll").is_file())
        self.assertTrue((ue4ss / "UE4SS.dll").is_file())
        self.assertFalse((ue4ss / "Mods" / "CharacterPack" / "Scripts" / "main.lua").exists())

        self.manager.delete_mod(str(mod["id"]))
        self.assertTrue((binaries / "dwmapi.dll").is_file())
        self.assertTrue((ue4ss / "UE4SS.dll").is_file())

    def test_character_package_refuses_different_existing_ue4ss_dll(self) -> None:
        binaries = self.game_dir / "GZPro" / "Binaries" / "Win64"
        binaries.mkdir(parents=True, exist_ok=True)
        existing = binaries / "dwmapi.dll"
        existing.write_bytes(b"a different proxy DLL")
        payload = make_package({
            "GZPro/Binaries/Win64/dwmapi.dll": b"mod proxy DLL",
            "GZPro/Binaries/Win64/ue4ss/UE4SS.dll": b"UE4SS core",
            "GZPro/Binaries/Win64/ue4ss/Mods/CharacterPack/enabled.txt": b"",
            "GZPro/Binaries/Win64/ue4ss/Mods/CharacterPack/Scripts/main.lua": b"print('character mod')",
        })
        with self.assertRaises(ConflictError):
            self.install("CharacterPack.zip", payload, kind="override")
        self.assertEqual(existing.read_bytes(), b"a different proxy DLL")
        self.assertFalse((binaries / "ue4ss" / "UE4SS.dll").exists())
        self.assertEqual(list(self.manager.archive_dir.glob("*.zip")), [])

    def test_ue4ss_only_package_uses_mod_folder_name(self) -> None:
        payload = make_package({
            "GZPro/Binaries/Win64/dwmapi.dll": b"proxy loader",
            "GZPro/Binaries/Win64/ue4ss/UE4SS.dll": b"UE4SS core",
            "GZPro/Binaries/Win64/ue4ss/Mods/CharacterOnly/enabled.txt": b"",
            "GZPro/Binaries/Win64/ue4ss/Mods/CharacterOnly/Scripts/main.lua": b"print('character only')",
        })
        mod = self.install("UnhelpfulArchiveName.zip", payload, kind="override")
        self.assertEqual(mod["stem"], "CharacterOnly")
        self.assertTrue((self.game_dir / "GZPro" / "Binaries" / "Win64" / "ue4ss" / "Mods" / "CharacterOnly" / "Scripts" / "main.lua").is_file())

    def test_profiles_save_and_switch_enabled_mod_combinations(self) -> None:
        first = self.install("First.zip")
        second_payload = make_package(default_members("SecondMod"))
        second = self.install("SecondMod.zip", second_payload)
        self.manager.set_mod_enabled(str(second["id"]), False)
        profile_a = self.manager.save_profile("First setup")
        self.assertEqual(profile_a["profiles"][0]["mod_ids"], [first["id"]])
        self.manager.set_mod_enabled(str(first["id"]), False)
        self.manager.set_mod_enabled(str(second["id"]), True)
        profile_b = self.manager.save_profile("Second setup")
        self.assertEqual(set(profile_b["profiles"][1]["mod_ids"]), {second["id"]})
        applied = self.manager.apply_profile(profile_a["profiles"][0]["id"])
        enabled = {mod["id"] for mod in applied["mods"] if mod["enabled"]}
        self.assertEqual(enabled, {first["id"]})
        self.assertEqual(applied["active_profile_id"], profile_a["profiles"][0]["id"])
        self.assertTrue((self.manager.archive_dir / f"{first['id']}.zip").is_file())
        self.assertTrue((self.manager.archive_dir / f"{second['id']}.zip").is_file())

    def test_profiles_reject_duplicate_names_and_delete_without_changing_mods(self) -> None:
        self.install()
        saved = self.manager.save_profile("Default")
        with self.assertRaises(ConflictError):
            self.manager.save_profile("default")
        self.manager.delete_profile(saved["profiles"][0]["id"])
        self.assertTrue(self.deployed()[0].is_file())
        self.assertTrue(self.manager.get_status()["mods"][0]["enabled"])

    def test_import_keeps_verified_backup_and_does_not_change_original_archives(self) -> None:
        original_bytes = self.original_archive.read_bytes()
        mod = self.install()
        utoc, ucas, stub = self.deployed()

        self.assertTrue(utoc.is_file())
        self.assertTrue(ucas.is_file())
        self.assertTrue(stub.is_file())
        self.assertEqual(utoc.read_bytes(), b"cooked toc")
        self.assertEqual(ucas.read_bytes(), b"cooked container")
        self.assertGreater(stub.stat().st_size, 0)
        self.assertEqual(self.original_archive.read_bytes(), original_bytes)
        self.assertTrue(mod["enabled"])
        self.assertEqual(mod["file_count"], 3)
        status = self.manager.get_status()
        self.assertEqual(len(status["mods"]), 1)
        self.assertTrue(status["mods"][0]["backup_available"])
        self.assertEqual(status["unmanaged_mods"], [])

    def test_override_mods_deploy_to_paks_root_with_game_prefix(self) -> None:
        mod = self.install(kind="override")
        utoc, ucas, stub = self.deployed(kind="override")

        self.assertTrue(all(path.is_file() for path in (utoc, ucas, stub)))
        self.assertEqual(utoc.read_bytes(), b"cooked toc")
        self.assertFalse((self.paks_dir / "LogicMods").exists())
        self.assertEqual(mod["kind"], "override")

        disabled = self.manager.set_mod_enabled(str(mod["id"]), False)
        self.assertFalse(disabled["enabled"])
        self.assertTrue(all(not path.exists() for path in (utoc, ucas, stub)))

    def test_override_mods_refuse_game_container_names(self) -> None:
        payload = make_package(default_members("Windows"))
        with self.assertRaises(ManagerError):
            self.install("Windows.zip", payload, kind="override")

    def test_invalid_kind_is_rejected(self) -> None:
        with self.assertRaises(ManagerError):
            self.install(kind="sideways")

    def test_package_companion_pak_is_used_instead_of_the_stub(self) -> None:
        members = default_members()
        members["TestMod.pak"] = b"author supplied pak"
        mod = self.install(payload=make_package(members))

        stub = self.paks_dir / "LogicMods" / "TestMod.pak"
        self.assertEqual(stub.read_bytes(), b"author supplied pak")
        self.assertEqual(mod["file_count"], 3)

    def test_disable_and_enable_preserve_backup_and_are_reversible(self) -> None:
        mod = self.install()
        deployed = self.deployed()
        backup = self.manager.archive_dir / f"{mod['id']}.zip"

        disabled = self.manager.set_mod_enabled(str(mod["id"]), False)
        self.assertTrue(all(not path.exists() for path in deployed))
        self.assertTrue(backup.is_file())
        self.assertFalse(disabled["enabled"])

        enabled = self.manager.set_mod_enabled(str(mod["id"]), True)
        self.assertTrue(all(path.is_file() for path in deployed))
        self.assertEqual(deployed[0].read_bytes(), b"cooked toc")
        self.assertTrue(enabled["enabled"])

    def test_delete_removes_verified_installed_files_backup_and_manifest_entry(self) -> None:
        mod = self.install()
        deployed = self.deployed()
        backup = self.manager.archive_dir / f"{mod['id']}.zip"

        result = self.manager.delete_mod(str(mod["id"]))

        self.assertEqual(result, {"deleted": True, "stem": "TestMod"})
        self.assertTrue(all(not path.exists() for path in deployed))
        self.assertFalse(backup.exists())
        self.assertEqual(self.manager.get_status()["mods"], [])

    def test_delete_refuses_to_remove_files_changed_outside_manager(self) -> None:
        mod = self.install()
        changed = self.deployed()[1]
        changed.write_bytes(b"changed outside manager")
        backup = self.manager.archive_dir / f"{mod['id']}.zip"

        with self.assertRaises(ConflictError):
            self.manager.delete_mod(str(mod["id"]))

        self.assertEqual(changed.read_bytes(), b"changed outside manager")
        self.assertTrue(backup.is_file())
        self.assertEqual(len(self.manager.get_status()["mods"]), 1)

    def test_info_and_thumb_metadata_are_shown_but_not_deployed(self) -> None:
        members = default_members()
        members["INFO.txt"] = "A replacement music pack.\\nChanges the battle themes.".encode("utf-8")
        members["THUMB.png"] = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXioAAAAASUVORK5CYII="
        )
        mod = self.install(payload=make_package(members))

        self.assertEqual(mod["description"], "A replacement music pack.\\nChanges the battle themes.")
        self.assertTrue(str(mod["thumbnail"]).startswith("data:image/png;base64,"))
        self.assertEqual(mod["file_count"], 3)
        self.assertFalse((self.paks_dir / "LogicMods" / "INFO.txt").exists())
        self.assertFalse((self.paks_dir / "LogicMods" / "THUMB.png").exists())
        status_mod = self.manager.get_status()["mods"][0]
        self.assertEqual(status_mod["description"], mod["description"])
        self.assertEqual(status_mod["thumbnail"], mod["thumbnail"])

    def test_info_file_must_be_root_level_and_utf8(self) -> None:
        nested = make_package({**default_members(), "docs/INFO.txt": b"description"})
        with self.assertRaises(ManagerError):
            self.install(payload=nested)

    def test_info_and_thumb_metadata_allow_dot_slash_zip_prefix(self) -> None:
        members = default_members()
        members["./INFO.txt"] = b"Metadata with a ZIP root prefix"
        members["./THUMB.png"] = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXioAAAAASUVORK5CYII="
        )

        mod = self.install(payload=make_package(members))

        self.assertEqual(mod["description"], "Metadata with a ZIP root prefix")
        self.assertTrue(str(mod["thumbnail"]).startswith("data:image/png;base64,"))
        self.assertEqual(mod["file_count"], 3)

    def test_ignores_root_level_documentation_files(self) -> None:
        members = default_members()
        members["README.txt"] = b"Installation instructions"
        members["readme.md"] = b"More documentation"
        members["LICENSE"] = b"License text"
        payload = make_package(members)

        mod = self.install(payload=payload)

        self.assertEqual(mod["file_count"], 3)
        self.assertEqual(
            [path.name for path in self.deployed()],
            ["TestMod.utoc", "TestMod.ucas", "TestMod.pak"],
        )

    def test_ignores_root_level_install_instructions(self) -> None:
        members = default_members()
        members["INSTALL.txt"] = b"Installation instructions"
        members["Installation.md"] = b"More installation instructions"
        payload = make_package(members)

        mod = self.install(payload=payload)

        self.assertEqual(mod["file_count"], 3)

    def test_still_rejects_nested_documentation_files(self) -> None:
        payload = make_package({
            **default_members(),
            "docs/README.txt": b"Nested documentation",
        })
        with self.assertRaises(ManagerError):
            self.install(payload=payload)

    def test_rejects_non_zip_and_path_like_names(self) -> None:
        for filename in ("track.wav", "..\\escape.zip", "folder/mod.zip", "CON.zip", "TestMod.pak"):
            with self.subTest(filename=filename), self.assertRaises(ManagerError):
                payload = b"data"
                self.manager.install_mod(io.BytesIO(payload), filename, len(payload))
        self.assertFalse((self.paks_dir / "LogicMods").exists())

    def test_rejects_packages_without_a_container(self) -> None:
        payload = make_package({"TestMod.pak": b"loose pak only"})
        with self.assertRaises(ManagerError):
            self.install(payload=payload)
        self.assertFalse((self.paks_dir / "LogicMods").exists())

    def test_accepts_different_container_stems_and_normalizes_to_utoc_name(self) -> None:
        payload = make_package({"OtherMod.ucas": b"cas", "TestMod.utoc": b"toc"})
        mod = self.install(payload=payload)
        self.assertEqual(mod["stem"], "TestMod")
        self.assertEqual(self.deployed()[0].read_bytes(), b"toc")
        self.assertEqual(self.deployed()[1].read_bytes(), b"cas")

    def test_skip_unsupported_package_files_when_requested(self) -> None:
        payload = make_package({
            **default_members("CharacterPack"),
            "notes/custom-data.txt": b"not deployed",
        })
        archive = self.root / "SkipUnsupported.zip"
        archive.write_bytes(payload)
        stem, members, _, _ = self.manager._read_package_members(archive, skip_unsupported=True)
        self.assertEqual(stem, "CharacterPack")
        self.assertEqual({member["name"] for member in members}, {
            "CharacterPack.utoc", "CharacterPack.ucas", "CharacterPack.pak"
        })

    def test_read_me_with_space_is_ignored(self) -> None:
        payload = make_package({
            "READ ME.txt": b"Instructions",
            **default_members("CharacterPack"),
        })
        mod = self.install(payload=payload)
        self.assertEqual(mod["stem"], "CharacterPack")

    def test_run_anyway_installs_dlls_into_game_binary_folder(self) -> None:
        payload = make_package({
            "dwmapi.dll": b"proxy loader",
            "Win64/ExtraLoader.dll": b"additional loader",
            "GZPro/Binaries/Win64/DirectPath.dll": b"direct path loader",
        })
        archive = self.root / "DllPackage.zip"
        archive.write_bytes(payload)
        with self.assertRaises(ManagerError) as raised:
            self.manager._read_package_members(archive)
        self.assertIn("DLL_INSTALL_CONFIRM_REQUIRED:", str(raised.exception))

        mod = self.manager.install_mod(
            io.BytesIO(payload), "DllPackage.zip", len(payload), "override",
            None, False, True,
        )
        binaries = self.game_dir / "GZPro" / "Binaries" / "Win64"
        self.assertEqual((binaries / "dwmapi.dll").read_bytes(), b"proxy loader")
        self.assertEqual((binaries / "ExtraLoader.dll").read_bytes(), b"additional loader")
        self.assertEqual((binaries / "DirectPath.dll").read_bytes(), b"direct path loader")
        self.assertTrue(mod["requires_ue4ss"])
        self.assertEqual(mod["file_count"], 3)

        self.manager.set_mod_enabled(str(mod["id"]), False)
        self.assertFalse((binaries / "dwmapi.dll").exists())
        self.assertFalse((binaries / "ExtraLoader.dll").exists())
        self.assertFalse((binaries / "DirectPath.dll").exists())
        self.manager.set_mod_enabled(str(mod["id"]), True)
        self.assertEqual((binaries / "ExtraLoader.dll").read_bytes(), b"additional loader")
        self.assertEqual((binaries / "DirectPath.dll").read_bytes(), b"direct path loader")

    def test_run_anyway_does_not_allow_arbitrary_non_dll_files(self) -> None:
        payload = make_package({"scripts/anything.exe": b"not a DLL"})
        archive = self.root / "UnsupportedExecutable.zip"
        archive.write_bytes(payload)
        with self.assertRaises(ManagerError):
            self.manager._read_package_members(archive, install_dlls=True)

    def test_rejects_packages_with_folder_paths(self) -> None:
        payload = make_package({"nested/TestMod.utoc": b"toc", "TestMod.ucas": b"cas"})
        with self.assertRaises(ManagerError):
            self.install(payload=payload)
        self.assertFalse((self.paks_dir / "LogicMods").exists())

    def test_does_not_overwrite_existing_managed_or_unmanaged_files(self) -> None:
        logic_mods = self.paks_dir / "LogicMods"
        logic_mods.mkdir()
        existing = logic_mods / "TestMod.ucas"
        existing.write_bytes(b"owned by someone else")

        with self.assertRaises(ConflictError):
            self.install()

        self.assertEqual(existing.read_bytes(), b"owned by someone else")
        self.assertFalse((logic_mods / "TestMod.utoc").exists())
        self.assertEqual(list(self.manager.archive_dir.glob("*.zip")), [])

    def test_refuses_to_change_mods_while_game_is_running(self) -> None:
        self.manager._is_game_running = lambda: True

        with self.assertRaises(ConflictError):
            self.install()
        self.assertFalse((self.paks_dir / "LogicMods").exists())

    def test_disable_refuses_to_remove_a_changed_file(self) -> None:
        mod = self.install()
        installed = self.paks_dir / "LogicMods" / "TestMod.ucas"
        installed.write_bytes(b"changed by another tool")

        with self.assertRaises(ConflictError):
            self.manager.set_mod_enabled(str(mod["id"]), False)

        self.assertEqual(installed.read_bytes(), b"changed by another tool")
        self.assertTrue((self.paks_dir / "LogicMods" / "TestMod.utoc").is_file())
        self.assertTrue((self.manager.archive_dir / f"{mod['id']}.zip").is_file())

    def test_lists_existing_unmanaged_mods_without_managing_them(self) -> None:
        logic_mods = self.paks_dir / "LogicMods"
        logic_mods.mkdir()
        existing = logic_mods / "ExternalMod.pak"
        existing.write_bytes(b"external")

        status = self.manager.get_status()

        self.assertEqual(
            status["unmanaged_mods"],
            [{"stem": "ExternalMod", "size": 8, "location": "LogicMods", "files": ["ExternalMod.pak"]}],
        )
        self.assertEqual(existing.read_bytes(), b"external")

    def test_detects_manual_mod_files_in_paks_without_manager_prefix(self) -> None:
        manual_utoc = self.paks_dir / "CustomMusic.utoc"
        manual_ucas = self.paks_dir / "CustomMusic.ucas"
        manual_pak = self.paks_dir / "CustomMusic.pak"
        manual_utoc.write_bytes(b"toc")
        manual_ucas.write_bytes(b"container")
        manual_pak.write_bytes(b"pak")

        status = self.manager.get_status()

        self.assertEqual(
            status["unmanaged_mods"],
            [{
                "stem": "CustomMusic",
                "size": 15,
                "location": "Paks",
                "files": ["CustomMusic.pak", "CustomMusic.ucas", "CustomMusic.utoc"],
            }],
        )
        self.assertEqual(manual_ucas.read_bytes(), b"container")
        self.assertEqual(self.original_archive.read_bytes(), b"untouched game archive")

    def test_adopts_manual_paks_mod_and_manages_it_reversibly(self) -> None:
        files = {
            "ExternalMusic.pak": b"pak bytes",
            "ExternalMusic.utoc": b"toc bytes",
            "ExternalMusic.ucas": b"container bytes",
        }
        for filename, contents in files.items():
            (self.paks_dir / filename).write_bytes(contents)

        adopted = self.manager.adopt_unmanaged_mod("Paks", "ExternalMusic")
        self.assertTrue(adopted["enabled"])
        self.assertEqual(adopted["kind"], "override")
        self.assertTrue(adopted["backup_available"])
        self.assertTrue(all((self.paks_dir / name).read_bytes() == data for name, data in files.items()))
        self.assertEqual(self.manager.get_status()["unmanaged_mods"], [])

        disabled = self.manager.set_mod_enabled(str(adopted["id"]), False)
        self.assertFalse(disabled["enabled"])
        self.assertTrue(all(not (self.paks_dir / name).exists() for name in files))
        self.assertTrue((self.manager.archive_dir / f"{adopted['id']}.zip").is_file())

        enabled = self.manager.set_mod_enabled(str(adopted["id"]), True)
        self.assertTrue(enabled["enabled"])
        self.assertTrue(all((self.paks_dir / name).read_bytes() == data for name, data in files.items()))

    def test_adopted_manual_logic_mod_is_managed_in_logicmods(self) -> None:
        logic_mods = self.paks_dir / "LogicMods"
        logic_mods.mkdir()
        (logic_mods / "ExternalBP.utoc").write_bytes(b"toc")
        (logic_mods / "ExternalBP.ucas").write_bytes(b"container")

        adopted = self.manager.adopt_unmanaged_mod("LogicMods", "ExternalBP")
        self.assertEqual(adopted["kind"], "blueprint")
        disabled = self.manager.set_mod_enabled(str(adopted["id"]), False)
        self.assertFalse(disabled["enabled"])
        self.assertFalse((logic_mods / "ExternalBP.utoc").exists())
        self.assertFalse((logic_mods / "ExternalBP.ucas").exists())

    def test_adoption_refuses_to_remove_or_backup_changed_files(self) -> None:
        source = self.paks_dir / "External.pak"
        source.write_bytes(b"original")
        original = source.read_bytes()
        adopted = self.manager.adopt_unmanaged_mod("Paks", "External")
        source.write_bytes(b"changed")
        with self.assertRaises(ConflictError):
            self.manager.set_mod_enabled(str(adopted["id"]), False)
        self.assertEqual(source.read_bytes(), b"changed")
        self.assertNotEqual(source.read_bytes(), original)

    def test_refuses_case_insensitive_filename_collision_with_a_disabled_mod(self) -> None:
        first = self.install()
        self.manager.set_mod_enabled(str(first["id"]), False)

        with self.assertRaises(ConflictError):
            self.install("testmod.ZIP", make_package(default_members("testmod")))

    def test_selected_game_path_is_validated_and_persisted(self) -> None:
        selected = self.manager.set_game_dir(str(self.game_dir))
        self.assertTrue(selected["game_valid"])
        config = json.loads((self.root / "manager-state" / "config.json").read_text())
        self.assertEqual(Path(config["game_dir"]), self.game_dir.resolve())

        restored = ModManager(state_base=self.root / "manager-state")
        self.assertEqual(restored.game_dir, self.game_dir.resolve())


    def test_unmanaged_mod_scan_cache_survives_restart_and_invalidates_on_folder_change(self) -> None:
        external = self.paks_dir / "ExternalCacheTest.pak"
        external.write_bytes(b"external")
        first = self.manager.get_status()
        self.assertEqual([item["stem"] for item in first["unmanaged_mods"]], ["ExternalCacheTest"])
        self.assertTrue(self.manager.unmanaged_cache_path.is_file())

        restored = ModManager(game_dir=self.game_dir, state_base=self.root / "manager-state")
        original_scan = restored._scan_unmanaged_mods
        calls = []
        def counted_scan(managed_names):
            calls.append(True)
            return original_scan(managed_names)
        restored._scan_unmanaged_mods = counted_scan
        cached = restored.get_status()
        self.assertEqual([item["stem"] for item in cached["unmanaged_mods"]], ["ExternalCacheTest"])
        self.assertEqual(calls, [])

        added = self.paks_dir / "NewExternalCacheTest.pak"
        added.write_bytes(b"new")
        refreshed = restored.get_status()
        self.assertEqual({item["stem"] for item in refreshed["unmanaged_mods"]}, {"ExternalCacheTest", "NewExternalCacheTest"})
        self.assertEqual(len(calls), 1)


class ModManagerHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.game_dir = self.root / "game"
        paks_dir = self.game_dir / "GZPro" / "Content" / "Paks"
        paks_dir.mkdir(parents=True)
        executable = self.game_dir / "GZPro" / "Binaries" / "Win64" / "GZProSteam-Win64-Shipping.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"test executable")
        self.manager = ModManager(
            game_dir=self.game_dir,
            state_base=self.root / "manager-state",
        )
        self.manager._is_game_running = lambda: False
        self.server = create_server(self.manager, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.origin = self.base_url

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def post(self, path: str, data: bytes = b"", headers: dict[str, str] | None = None):
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Origin": self.origin, **(headers or {})},
        )
        return urlopen(request, timeout=5)

    def test_browser_api_import_disable_and_restore_workflow(self) -> None:
        with urlopen(f"{self.base_url}/api/status", timeout=5) as response:
            initial_status = json.load(response)
        self.assertTrue(initial_status["game_valid"])
        self.assertFalse(initial_status["game_running"])

        payload = make_package(default_members("BrowserTest"))
        with self.post(
            "/api/mods",
            payload,
            {
                "Content-Type": "application/octet-stream",
                "X-Mod-Name": "BrowserTest.zip",
                "X-Mod-Kind": "blueprint",
            },
        ) as response:
            installed = json.load(response)
            self.assertEqual(response.status, 201)
        logic_mods = self.game_dir / "GZPro" / "Content" / "Paks" / "LogicMods"
        game_copy = logic_mods / "BrowserTest.ucas"
        self.assertEqual(game_copy.read_bytes(), b"cooked container")
        self.assertTrue((logic_mods / "BrowserTest.utoc").is_file())
        self.assertTrue((logic_mods / "BrowserTest.pak").is_file())

        with self.post(f"/api/mods/{installed['id']}/disable") as response:
            self.assertFalse(json.load(response)["enabled"])
        self.assertFalse(game_copy.exists())

        with self.post(f"/api/mods/{installed['id']}/enable") as response:
            self.assertTrue(json.load(response)["enabled"])
        self.assertEqual(game_copy.read_bytes(), b"cooked container")

        with self.post(f"/api/mods/{installed['id']}/delete") as response:
            self.assertTrue(json.load(response)["deleted"])
        self.assertFalse(game_copy.exists())
        self.assertFalse((logic_mods / "BrowserTest.utoc").exists())
        self.assertFalse((logic_mods / "BrowserTest.pak").exists())
        self.assertEqual(self.manager.get_status()["mods"], [])
        self.assertFalse((self.manager.archive_dir / f"{installed['id']}.zip").exists())

    def test_status_progress_endpoint_reports_current_stage(self) -> None:
        with urlopen(f"{self.base_url}/api/status/progress", timeout=5) as response:
            progress = json.load(response)
        self.assertIsInstance(progress.get("active"), bool)
        self.assertIsInstance(progress.get("stage"), str)

    def test_upload_rejects_non_local_origin_before_writing_files(self) -> None:
        request = Request(
            f"{self.base_url}/api/mods",
            data=b"not a mod",
            headers={
                "Origin": "https://example.com",
                "Content-Type": "application/octet-stream",
                "X-Mod-Name": "External.pak",
            },
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=5)
        self.assertEqual(error.exception.code, 403)
        self.assertFalse(
            (self.game_dir / "GZPro" / "Content" / "Paks" / "LogicMods").exists()
        )


if __name__ == "__main__":
    unittest.main()
