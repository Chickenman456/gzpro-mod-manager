from __future__ import annotations

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

    def test_rejects_packages_with_mismatched_mod_names(self) -> None:
        payload = make_package({"TestMod.utoc": b"toc", "OtherMod.ucas": b"cas"})
        with self.assertRaises(ManagerError):
            self.install(payload=payload)
        self.assertFalse((self.paks_dir / "LogicMods").exists())

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
            [{"filename": "ExternalMod.pak", "size": 8, "location": "LogicMods"}],
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
            [
                {"filename": "CustomMusic.pak", "size": 3, "location": "Paks"},
                {"filename": "CustomMusic.ucas", "size": 9, "location": "Paks"},
                {"filename": "CustomMusic.utoc", "size": 3, "location": "Paks"},
            ],
        )
        self.assertEqual(manual_ucas.read_bytes(), b"container")
        self.assertEqual(self.original_archive.read_bytes(), b"untouched game archive")

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
