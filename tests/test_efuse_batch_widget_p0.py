from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from src.efuse_batch_dialog import (
    BurnAuthorization,
    BurnEfuseBatchWidget,
    BurnTaskItem,
    BurnTaskState,
)
from src.efuse_batch_safety import EfuseRunConfig, EfuseTarget


def _port(device: str, serial_number: str) -> SimpleNamespace:
    return SimpleNamespace(
        device=device,
        description="USB Serial Port",
        serial_number=serial_number,
        location="1-2",
        vid=0x10C4,
        pid=0xEA60,
        hwid=f"USB VID:PID=10C4:EA60 SER={serial_number}",
    )


def _run_config(*fields: EfuseTarget) -> EfuseRunConfig:
    return EfuseRunConfig(fields=tuple(fields), chip="esp32p4", baud="115200")


class EfuseBatchWidgetP0Tests(unittest.TestCase):
    _app: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.widget = BurnEfuseBatchWidget()

    def tearDown(self) -> None:
        self.widget.close()
        self.widget.deleteLater()
        self._app.processEvents()

    def _attach_task(
        self,
        *,
        port: str = "COM7",
        transport_id: str = "usb:10c4:ea60:loc=1-2:sn=unit-a",
        state: BurnTaskState = BurnTaskState.WAITING,
        config: EfuseRunConfig | None = None,
    ) -> BurnTaskItem:
        task = BurnTaskItem(
            device_id=f"{transport_id}#1",
            port=port,
            transport_id=transport_id,
            chip_name="esp32p4",
            state=state,
            run_config=config,
        )
        self.widget._tasks.append(task)
        self.widget._present_transports[port] = transport_id
        return task

    def test_same_com_transport_change_replaces_task_without_safety_state(self) -> None:
        with (
            patch(
                "src.efuse_batch_dialog.list_ports.comports",
                return_value=[_port("COM7", "UNIT-A")],
            ),
            patch("src.efuse_batch_dialog._identify_chip", return_value="esp32p4"),
            patch.object(self.widget, "_schedule_next"),
        ):
            self.widget._poll_ports()

        old_task = self.widget._tasks[0]
        old_task.state = BurnTaskState.READ_OK
        old_task.read_result = {"FIELD": {"value": "0"}}
        old_task.fields_to_burn = [EfuseTarget("FIELD", "1")]
        old_task.fields_skipped = ["SATISFIED"]
        old_task.fields_conflict = ["LOCKED"]
        old_task.precheck_identity = "MAC_FACTORY:aabbcc000001"
        old_task.run_config = _run_config(EfuseTarget("FIELD", "1"))
        old_task.authorization = BurnAuthorization.BATCH
        old_task.authorization_id = 41

        with (
            patch(
                "src.efuse_batch_dialog.list_ports.comports",
                return_value=[_port("COM7", "UNIT-B")],
            ),
            patch("src.efuse_batch_dialog._identify_chip", return_value="esp32p4"),
            patch.object(self.widget, "_schedule_next"),
        ):
            self.widget._poll_ports()

        self.assertEqual(len(self.widget._tasks), 1)
        replacement = self.widget._tasks[0]
        self.assertIsNot(replacement, old_task)
        self.assertNotEqual(replacement.device_id, old_task.device_id)
        self.assertNotEqual(replacement.transport_id, old_task.transport_id)
        self.assertEqual(replacement.state, BurnTaskState.WAITING)
        self.assertEqual(replacement.read_result, {})
        self.assertEqual(replacement.fields_to_burn, [])
        self.assertEqual(replacement.fields_skipped, [])
        self.assertEqual(replacement.fields_conflict, [])
        self.assertEqual(replacement.precheck_identity, "")
        self.assertIsNone(replacement.run_config)
        self.assertEqual(replacement.authorization, BurnAuthorization.NONE)
        self.assertIsNone(replacement.authorization_id)

        self.assertNotIn(old_task, self.widget._tasks)
        self.assertEqual(old_task.read_result, {})
        self.assertEqual(old_task.precheck_identity, "")
        self.assertEqual(old_task.authorization, BurnAuthorization.NONE)
        self.assertIsNone(old_task.authorization_id)

    def test_batch_authorizes_only_waiting_tasks_present_at_confirmation(self) -> None:
        first_scan = [_port("COM7", "UNIT-A")]
        second_scan = [*_port_list(first_scan), _port("COM8", "UNIT-B")]
        config = _run_config(EfuseTarget("SECURE_BOOT_EN", "1"))

        with (
            patch(
                "src.efuse_batch_dialog.list_ports.comports",
                return_value=first_scan,
            ),
            patch("src.efuse_batch_dialog._identify_chip", return_value="esp32p4"),
            patch.object(self.widget, "_schedule_next"),
        ):
            self.widget._poll_ports()

        confirmed_task = self.widget._tasks[0]
        with (
            patch.object(self.widget, "_capture_run_config", return_value=config),
            patch.object(self.widget, "_schedule_next"),
        ):
            self.widget._start_all()

        self.assertEqual(confirmed_task.authorization, BurnAuthorization.NONE)
        self.assertIsNotNone(confirmed_task.batch_precheck_id)

        with (
            patch(
                "src.efuse_batch_dialog.list_ports.comports",
                return_value=second_scan,
            ),
            patch("src.efuse_batch_dialog._identify_chip", return_value="esp32p4"),
            patch.object(self.widget, "_schedule_next"),
        ):
            self.widget._poll_ports()

        joined_task = next(task for task in self.widget._tasks if task.port == "COM8")
        confirmed_task.state = BurnTaskState.READ_OK
        confirmed_task.precheck_identity = "MAC_FACTORY:aabbcc000001"
        with (
            patch.object(
                QMessageBox,
                "warning",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch.object(self.widget, "_schedule_next"),
        ):
            self.widget._maybe_confirm_pending_batch()

        self.assertEqual(confirmed_task.authorization, BurnAuthorization.BATCH)
        self.assertIsNotNone(confirmed_task.authorization_id)
        self.assertEqual(
            confirmed_task.authorized_identity,
            confirmed_task.precheck_identity,
        )
        self.assertIsNone(confirmed_task.batch_precheck_id)
        self.assertEqual(joined_task.authorization, BurnAuthorization.NONE)
        self.assertIsNone(joined_task.authorization_id)
        self.assertIsNone(joined_task.run_config)

    def test_pending_batch_retry_keeps_snapshot_and_mismatch_fails_closed(self) -> None:
        snapshot = _run_config(EfuseTarget("FIELD_A", "1"))
        changed = _run_config(EfuseTarget("FIELD_B", "1"))
        self.widget._pending_batch_id = 31
        self.widget._pending_batch_config = snapshot
        task = self._attach_task(state=BurnTaskState.FAILED, config=changed)
        task.batch_precheck_id = 31
        task.force_burn = True

        with patch.object(self.widget, "_run_task"):
            self.widget._retry_task(task)

        self.assertEqual(task.run_config, snapshot)
        self.assertFalse(task.force_burn)
        self.assertEqual(task.batch_precheck_id, 31)

        task.state = BurnTaskState.READ_OK
        task.run_config = changed
        task.precheck_identity = "MAC_FACTORY:aabbcc000001"
        with patch.object(QMessageBox, "warning") as warning:
            self.widget._maybe_confirm_pending_batch()

        self.assertEqual(task.state, BurnTaskState.FAILED)
        self.assertIn("配置", task.error_message)
        self.assertEqual(task.authorization, BurnAuthorization.NONE)
        warning.assert_not_called()

    def test_mixed_burnable_and_conflict_precheck_never_enters_identity_check(self) -> None:
        config = _run_config(
            EfuseTarget("BURNABLE", "1"),
            EfuseTarget("LOCKED", "1"),
        )
        task = self._attach_task(state=BurnTaskState.READING, config=config)
        task.authorization = BurnAuthorization.BATCH
        task.authorization_id = 7
        output = json.dumps(
            {
                "MAC_FACTORY": {"value": "AA:BB:CC:00:00:01"},
                "BURNABLE": {"value": "0", "writeable": True},
                "LOCKED": {"value": "0", "writeable": False},
            }
        )

        with (
            patch.object(self.widget, "_do_burn") as do_burn,
            patch.object(self.widget, "_start_process") as start_process,
            patch.object(self.widget, "_on_task_done"),
        ):
            self.widget._on_read_finished(task, 0, output)

        self.assertEqual(task.state, BurnTaskState.FAILED)
        self.assertEqual([field.name for field in task.fields_to_burn], ["BURNABLE"])
        self.assertEqual(task.fields_conflict, ["LOCKED"])
        do_burn.assert_not_called()
        start_process.assert_not_called()

    def test_identity_change_blocks_burn_command_construction(self) -> None:
        config = _run_config(EfuseTarget("SECURE_BOOT_EN", "1"))
        task = self._attach_task(state=BurnTaskState.IDENTITY_CHECK, config=config)
        task.precheck_identity = "MAC_FACTORY:aabbcc000001"
        task.authorized_identity = task.precheck_identity
        task.fields_to_burn = [EfuseTarget("SECURE_BOOT_EN", "1")]
        task.authorization = BurnAuthorization.BATCH
        task.authorization_id = 8
        replacement_output = json.dumps(
            {
                "MAC_FACTORY": {"value": "AA:BB:CC:00:00:02"},
                "SECURE_BOOT_EN": {"value": "0", "writeable": True},
            }
        )

        with (
            patch.object(self.widget, "_build_espefuse_cmd") as build_command,
            patch.object(self.widget, "_execute_burn") as execute_burn,
            patch.object(self.widget, "_on_task_done"),
        ):
            self.widget._on_identity_check_finished(task, 0, replacement_output)

        self.assertEqual(task.state, BurnTaskState.FAILED)
        build_command.assert_not_called()
        execute_burn.assert_not_called()

    def test_matching_identity_consumes_authorization_and_starts_burn_once(self) -> None:
        config = _run_config(EfuseTarget("SECURE_BOOT_EN", "1"))
        task = self._attach_task(state=BurnTaskState.IDENTITY_CHECK, config=config)
        task.precheck_identity = "MAC_FACTORY:aabbcc000001"
        task.authorized_identity = task.precheck_identity
        task.fields_to_burn = [EfuseTarget("SECURE_BOOT_EN", "1")]
        task.authorization = BurnAuthorization.BATCH
        task.authorization_id = 9
        identity_output = json.dumps(
            {
                "MAC_FACTORY": {"value": "AA:BB:CC:00:00:01"},
                "SECURE_BOOT_EN": {"value": "0", "writeable": True},
            }
        )
        started_commands: list[list[str]] = []

        def capture_start(
            started_task: BurnTaskItem,
            command: list[str],
            _callback: object,
        ) -> bool:
            self.assertIs(started_task, task)
            self.assertEqual(started_task.state, BurnTaskState.BURNING)
            self.assertEqual(started_task.authorization, BurnAuthorization.NONE)
            self.assertIsNone(started_task.authorization_id)
            started_commands.append(command)
            return True

        with (
            patch.object(
                self.widget,
                "_build_espefuse_cmd",
                return_value=["espefuse", "burn-efuse"],
            ) as build_command,
            patch.object(self.widget, "_start_process", side_effect=capture_start),
            patch.object(self.widget, "_on_task_done"),
        ):
            self.widget._on_identity_check_finished(task, 0, identity_output)
            self.widget._on_identity_check_finished(task, 0, identity_output)

        build_command.assert_called_once_with(
            task,
            ["--do-not-confirm", "burn-efuse", "SECURE_BOOT_EN", "1"],
        )
        self.assertEqual(started_commands, [["espefuse", "burn-efuse"]])
        self.assertEqual(task.state, BurnTaskState.BURNING)
        self.assertEqual(task.authorization, BurnAuthorization.NONE)
        self.assertIsNone(task.authorization_id)

    def test_disarmed_auto_read_callback_stops_at_read_ok(self) -> None:
        config = _run_config(EfuseTarget("SECURE_BOOT_EN", "1"))
        self.widget._auto_authorization_id = 23
        self.widget._auto_run_config = config
        task = self._attach_task(state=BurnTaskState.READING, config=config)
        task.authorization = BurnAuthorization.AUTO
        task.authorization_id = 23
        output = json.dumps(
            {
                "MAC_FACTORY": {"value": "AA:BB:CC:00:00:01"},
                "SECURE_BOOT_EN": {"value": "0", "writeable": True},
            }
        )

        self.widget._disarm_auto_burn(log=False)
        with (
            patch.object(self.widget, "_do_burn") as do_burn,
            patch.object(self.widget, "_on_task_done") as task_done,
        ):
            self.widget._on_read_finished(task, 0, output)

        self.assertEqual(task.state, BurnTaskState.READ_OK)
        self.assertEqual(task.authorization, BurnAuthorization.NONE)
        self.assertIsNone(task.authorization_id)
        do_burn.assert_not_called()
        task_done.assert_not_called()

    def test_verify_fails_when_precheck_skipped_field_is_missing_or_changed(self) -> None:
        config = _run_config(
            EfuseTarget("ALREADY_SET", "1"),
            EfuseTarget("BURNED", "2"),
        )
        verification_cases = {
            "missing": {
                "MAC_FACTORY": {"value": "AA:BB:CC:00:00:01"},
                "BURNED": {"value": "2"},
            },
            "changed": {
                "MAC_FACTORY": {"value": "AA:BB:CC:00:00:01"},
                "ALREADY_SET": {"value": "0"},
                "BURNED": {"value": "2"},
            },
        }

        for case_name, verify_data in verification_cases.items():
            with self.subTest(case=case_name):
                self.widget._tasks.clear()
                self.widget._present_transports.clear()
                task = self._attach_task(
                    state=BurnTaskState.VERIFYING,
                    config=config,
                )
                task.precheck_identity = "MAC_FACTORY:aabbcc000001"
                task.fields_skipped = ["ALREADY_SET"]
                task.fields_to_burn = [EfuseTarget("BURNED", "2")]

                with patch.object(self.widget, "_on_task_done"):
                    self.widget._on_verify_finished(task, 0, json.dumps(verify_data))

                self.assertEqual(task.state, BurnTaskState.FAILED)
                self.assertIn("ALREADY_SET", task.error_message)

    def test_stop_and_clear_do_not_kill_irreversible_task(self) -> None:
        config = _run_config(EfuseTarget("SECURE_BOOT_EN", "1"))
        task = self._attach_task(state=BurnTaskState.BURNING, config=config)
        task.process = MagicMock(name="burn_process")

        with patch.object(self.widget, "_kill_task") as kill_task:
            self.widget._stop_all()
            self.widget._clear_all_devices()

        kill_task.assert_not_called()
        self.assertIn(task, self.widget._tasks)
        self.assertEqual(task.state, BurnTaskState.BURNING)
        task.process = None

    def test_poll_does_not_kill_burn_when_transport_disappears(self) -> None:
        config = _run_config(EfuseTarget("SECURE_BOOT_EN", "1"))
        task = self._attach_task(state=BurnTaskState.BURNING, config=config)
        process = MagicMock(name="burn_process")
        callback = MagicMock(name="burn_callback")
        task.process = process
        task.process_token = 44

        with (
            patch("src.efuse_batch_dialog.list_ports.comports", return_value=[]),
            patch.object(self.widget, "_kill_task") as kill_task,
            patch.object(self.widget, "_schedule_next"),
        ):
            self.widget._poll_ports()

        kill_task.assert_not_called()
        self.assertIn(task, self.widget._tasks)
        self.assertIn("连接已丢失", task.transport_warning)

        self.widget._on_process_finished(task, process, 44, 1, "lost", callback)
        callback.assert_called_once_with(task, 1, "lost")
        process.deleteLater.assert_called_once_with()
        self.assertIsNone(task.process)

    def test_stale_process_and_token_finished_callback_is_ignored(self) -> None:
        task = self._attach_task(state=BurnTaskState.READING)
        current_process = MagicMock(name="current_process")
        stale_process = MagicMock(name="stale_process")
        callback = MagicMock(name="callback")
        task.process = current_process
        task.process_token = 12

        self.widget._on_process_finished(
            task,
            stale_process,
            11,
            0,
            "stale output",
            callback,
        )

        callback.assert_not_called()
        stale_process.deleteLater.assert_called_once_with()
        self.assertIs(task.process, current_process)
        self.assertEqual(task.process_token, 12)
        current_process.deleteLater.assert_not_called()
        task.process = None


def _port_list(ports: list[SimpleNamespace]) -> list[SimpleNamespace]:
    """Return a fresh list while keeping fake port objects readable in test setup."""

    return list(ports)


if __name__ == "__main__":
    unittest.main()
