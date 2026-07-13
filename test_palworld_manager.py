import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import palworld_manager as manager


SAMPLE = '''[/Script/Pal.PalGameWorldSettings]
OptionSettings=(ServerName="palworld, friends",ServerPlayerMaxNum=8,ExpRate=2.500000,bIsPvP=False,CrossplayPlatforms=(Steam,Xbox,PS5,Mac),DenyTechnologyList=)
'''

COMPOSE = '''services:
  palworld-server:
    image: example/palserver:latest
    command:
      - -port=8211
      - -useperfthreads
      - -NoAsyncLoadingThread
      - -UseMultithreadForDS
    ports:
      - "8211:8211/udp"
'''


class ConfigTest(unittest.TestCase):
    def test_parse_nested_and_quoted_commas(self):
        values = manager.parse_ini(SAMPLE)
        self.assertEqual("palworld, friends", values["ServerName"])
        self.assertEqual(8, values["ServerPlayerMaxNum"])
        self.assertEqual(2.5, values["ExpRate"])
        self.assertFalse(values["bIsPvP"])
        self.assertEqual(["Steam", "Xbox", "PS5", "Mac"], values["CrossplayPlatforms"])
        self.assertEqual("", values["DenyTechnologyList"])

    def test_round_trip_preserves_raw_fields(self):
        values = manager.parse_ini(SAMPLE)
        rendered = manager.render_ini(values)
        reparsed = manager.parse_ini(rendered)
        self.assertEqual(values, reparsed)
        self.assertIn("DenyTechnologyList=)", rendered)

    def test_validation_rejects_out_of_range_value(self):
        values = manager.parse_ini(SAMPLE)
        values["BaseCampMaxNumInGuild"] = 11
        with self.assertRaisesRegex(ValueError, "每公会据点数"):
            manager.normalize_values(values)

    def test_parse_and_render_official_arguments(self):
        arguments = manager.parse_compose_arguments(COMPOSE)
        self.assertEqual(8211, arguments["port"])
        self.assertTrue(arguments["performanceFlags"])
        arguments.update({"port": 9000, "performanceFlags": False, "players": 12, "logFormat": "json"})
        rendered = manager.render_compose_arguments(COMPOSE, arguments)
        self.assertIn("- -port=9000", rendered)
        self.assertIn("- -players=12", rendered)
        self.assertIn("- -logformat=json", rendered)
        self.assertNotIn("-useperfthreads", rendered)
        self.assertIn('"9000:9000/udp"', rendered)

    def test_rest_api_mapping_is_loopback_only(self):
        rendered = manager.render_rest_api_mapping(COMPOSE, True, 8212)
        self.assertIn('"127.0.0.1:8212:8212/tcp"', rendered)
        self.assertEqual(1, rendered.count("Palworld Manager local REST API"))
        rendered = manager.render_rest_api_mapping(rendered, True, 8213)
        self.assertNotIn("8212:8212/tcp", rendered)
        self.assertIn("8213:8213/tcp", rendered)
        rendered = manager.render_rest_api_mapping(rendered, False, 8213)
        self.assertNotIn("Palworld Manager local REST API", rendered)

    def test_player_action_uses_official_payload(self):
        with mock.patch.object(manager, "pal_api_request", return_value={}) as request:
            message = manager.pal_action({"action": "kick", "userid": "steam_123", "message": "bye"})
        self.assertEqual("玩家已踢出", message)
        request.assert_called_once_with("/kick", "POST", {"userid": "steam_123", "message": "bye"})

    def test_members_remain_after_player_goes_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            member_path = Path(directory) / "members.json"
            player = {"userId": "steam_123", "name": "朋友", "iP": "192.0.2.8", "ping": 12.8, "level": 9}
            with mock.patch.object(manager, "MEMBER_PATH", member_path):
                online = manager.merge_online_members([player])
                offline = manager.merge_online_members([])
            self.assertTrue(online[0]["online"])
            self.assertFalse(offline[0]["online"])
            self.assertEqual("朋友", offline[0]["name"])
            self.assertEqual("192.0.2.8", offline[0]["ip"])

    def test_log_timestamps_are_converted_to_shanghai(self):
        output = "palworld | 2026-07-10T08:43:29.540000000Z [2026-07-10 08:43:29] server started\n"
        result = subprocess.CompletedProcess([], 0, output, "")
        with mock.patch.object(manager, "compose_command", return_value=result):
            logs = manager.recent_logs(20)
        self.assertIn("2026-07-10T16:43:29+08:00", logs)
        self.assertIn("[2026-07-10 16:43:29]", logs)

    def test_world_archive_contains_save_and_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved = root / "Saved"
            ini = saved / "Config/LinuxServer/PalWorldSettings.ini"
            ini.parent.mkdir(parents=True)
            ini.write_text(SAMPLE, encoding="utf-8")
            world = saved / "SaveGames/0/world/Level.sav"
            world.parent.mkdir(parents=True)
            world.write_bytes(b"world-data")
            compose = root / "compose.yaml"
            compose.write_text(COMPOSE, encoding="utf-8")
            target = root / "export.zip"
            with mock.patch.object(manager, "PALWORLD_ROOT", root), mock.patch.object(
                manager, "INI_PATH", ini
            ), mock.patch.object(manager, "COMPOSE_FILE", compose), mock.patch.object(
                manager, "DATA_DIR", root / "manager"
            ), mock.patch.object(manager, "STATE_PATH", root / "manager/settings.json"), mock.patch.object(
                manager, "MEMBER_PATH", root / "manager/members.json"
            ):
                manager.create_world_archive(target)
                manifest = manager.validate_world_archive(target)
            self.assertEqual("palworld-manager-archive-v1", manifest["format"])
            with zipfile.ZipFile(target) as archive:
                self.assertIn("Saved/SaveGames/0/world/Level.sav", archive.namelist())
                self.assertIn("config/compose.yaml", archive.namelist())

    def test_world_archive_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "unsafe.zip"
            with zipfile.ZipFile(target, "w") as archive:
                archive.writestr("manifest.json", '{"format":"palworld-manager-archive-v1"}')
                archive.writestr("Saved/Level.sav", "world")
                archive.writestr("../escape", "bad")
            with self.assertRaisesRegex(ValueError, "不安全路径"):
                manager.validate_world_archive(target)

    def test_atomic_state_file(self):
        values = manager.parse_ini(SAMPLE)
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "settings.json"
            with mock.patch.object(manager, "STATE_PATH", state_path), mock.patch.object(manager, "DATA_DIR", Path(directory)):
                manager.save_state(values, manager.DEFAULT_ARGUMENTS)
                payload = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual("palworld, friends", payload["values"]["ServerName"])

    def test_apply_rolls_back_when_restart_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ini_path = root / "PalWorldSettings.ini"
            state_path = root / "settings.json"
            backup_dir = root / "backups"
            compose_path = root / "compose.yaml"
            ini_path.write_text(SAMPLE, encoding="utf-8")
            compose_path.write_text(COMPOSE, encoding="utf-8")
            original = ini_path.read_text(encoding="utf-8")
            values = manager.parse_ini(SAMPLE)
            values["ServerName"] = "changed"
            with mock.patch.object(manager, "INI_PATH", ini_path), mock.patch.object(
                manager, "STATE_PATH", state_path
            ), mock.patch.object(manager, "DATA_DIR", root), mock.patch.object(
                manager, "BACKUP_DIR", backup_dir
            ), mock.patch.object(manager, "COMPOSE_FILE", compose_path), mock.patch.object(
                manager, "service_action", return_value=(False, "boom")
            ):
                with self.assertRaisesRegex(RuntimeError, "已恢复原配置"):
                    manager.apply_values(values, manager.DEFAULT_ARGUMENTS)
            self.assertEqual(original, ini_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
