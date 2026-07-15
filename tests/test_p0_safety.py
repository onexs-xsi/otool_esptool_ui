import unittest

from src.efuse_batch_safety import (
    EfuseTarget,
    build_transport_fingerprint,
    evaluate_efuse_precheck,
    evaluate_efuse_verification,
    extract_stable_device_identity,
)
from src.esptool_commands import build_erase_flash_args, is_erase_flash_command


class P0SafetyTests(unittest.TestCase):
    def test_whole_chip_erase_uses_dedicated_esptool_command(self) -> None:
        command = build_erase_flash_args(
            ["--chip", "esp32s3", "--port", "COM7", "--baud", "921600"]
        )

        self.assertEqual(
            command,
            [
                "--chip",
                "esp32s3",
                "--port",
                "COM7",
                "--baud",
                "921600",
                "erase-flash",
            ],
        )
        self.assertTrue(is_erase_flash_command(command))
        self.assertNotIn("erase-region", command)
        self.assertNotIn("ALL", command)
        self.assertNotIn("--force", command)

    def test_any_precheck_conflict_blocks_partial_burn(self) -> None:
        fields = (
            EfuseTarget("SECURE_BOOT_EN", "1"),
            EfuseTarget("FLASH_CRYPT_CNT", "1"),
        )
        result = evaluate_efuse_precheck(
            fields,
            {
                "SECURE_BOOT_EN": {"value": 0, "writeable": True},
                "FLASH_CRYPT_CNT": {"value": 0, "writeable": False},
            },
        )

        self.assertEqual([field.name for field in result.to_burn], ["SECURE_BOOT_EN"])
        self.assertEqual(result.conflicts, ("FLASH_CRYPT_CNT",))
        self.assertFalse(result.can_burn)
        self.assertFalse(result.all_satisfied)

    def test_precheck_distinguishes_satisfied_and_burnable_fields(self) -> None:
        fields = (EfuseTarget("A", "1"), EfuseTarget("B", "0x2"))
        result = evaluate_efuse_precheck(
            fields,
            {
                "A": {"value": "1", "writeable": False},
                "B": {"value": "0", "writeable": True},
            },
        )

        self.assertEqual(result.skipped, ("A",))
        self.assertEqual([field.name for field in result.to_burn], ["B"])
        self.assertTrue(result.can_burn)

    def test_precheck_blocks_otp_bit_clearing_and_unreadable_fields(self) -> None:
        fields = (
            EfuseTarget("WOULD_CLEAR", "0x1"),
            EfuseTarget("HIDDEN", "1"),
        )

        result = evaluate_efuse_precheck(
            fields,
            {
                "WOULD_CLEAR": {
                    "value": "2",
                    "raw_value": "0x2",
                    "bit_len": 2,
                    "readable": True,
                    "writeable": True,
                },
                "HIDDEN": {
                    "value": "?",
                    "raw_value": "0x0",
                    "bit_len": 1,
                    "readable": False,
                    "writeable": True,
                },
            },
        )

        self.assertEqual(result.to_burn, ())
        self.assertEqual(result.conflicts, ("WOULD_CLEAR", "HIDDEN"))
        self.assertFalse(result.can_burn)

    def test_precheck_prefers_raw_bits_and_fails_closed_for_new_semantic_value(self) -> None:
        raw_result = evaluate_efuse_precheck(
            (EfuseTarget("RAW", "1"),),
            {
                "RAW": {
                    "value": "1",
                    "raw_value": "0x0",
                    "bit_len": 1,
                    "readable": True,
                    "writeable": True,
                }
            },
        )
        semantic_result = evaluate_efuse_precheck(
            (EfuseTarget("PURPOSE", "HMAC_UP"),),
            {
                "PURPOSE": {
                    "value": "USER",
                    "raw_value": "0x1",
                    "bit_len": 4,
                    "readable": True,
                    "writeable": True,
                }
            },
        )
        malformed_raw_result = evaluate_efuse_precheck(
            (EfuseTarget("BROKEN", "1"),),
            {
                "BROKEN": {
                    "value": "1",
                    "raw_value": "not-hex",
                    "bit_len": 1,
                    "readable": True,
                    "writeable": True,
                }
            },
        )

        self.assertEqual([field.name for field in raw_result.to_burn], ["RAW"])
        self.assertEqual(semantic_result.conflicts, ("PURPOSE",))
        self.assertEqual(malformed_raw_result.conflicts, ("BROKEN",))

    def test_factory_mac_is_used_as_device_identity(self) -> None:
        identity = extract_stable_device_identity(
            {"MAC_FACTORY": {"value": "AA:BB:CC:01:02:03", "writeable": False}}
        )

        self.assertEqual(identity, "MAC_FACTORY:aabbcc010203")

    def test_raw_value_is_preferred_for_device_identity(self) -> None:
        identity = extract_stable_device_identity(
            {
                "MAC_FACTORY": {
                    "raw_value": "0xAABBCC010203",
                    "value": "11:22:33:44:55:66",
                }
            }
        )

        self.assertEqual(identity, "MAC_FACTORY:aabbcc010203")

    def test_64_bit_identity_and_supported_separators_are_accepted(self) -> None:
        identity = extract_stable_device_identity(
            {"BASE_MAC_ADDR": {"value": "AA-BB-CC-DD-01-02-03-04"}}
        )

        self.assertEqual(identity, "BASE_MAC_ADDR:aabbccdd01020304")

    def test_missing_or_zero_mac_is_not_considered_an_identity(self) -> None:
        self.assertEqual(extract_stable_device_identity({"FIELD": {"value": 1}}), "")
        self.assertEqual(
            extract_stable_device_identity({"MAC": {"value": "00:00:00:00:00:00"}}),
            "",
        )

    def test_all_f_or_malformed_identity_is_rejected(self) -> None:
        rejected_values = (
            "FF:FF:FF:FF:FF:FF",
            "0xffffffffffffffff",
            "AA:BB:CC:DD:EE",
            "AA:BB:CC:DD:EE:GG",
            "AA/BB/CC/DD/EE/FF",
        )

        for value in rejected_values:
            with self.subTest(value=value):
                self.assertEqual(
                    extract_stable_device_identity(
                        {"MAC_FACTORY": {"raw_value": value}}
                    ),
                    "",
                )

    def test_invalid_raw_value_does_not_fall_back_to_display_value(self) -> None:
        identity = extract_stable_device_identity(
            {
                "MAC_FACTORY": {
                    "raw_value": "not-a-mac",
                    "value": "AA:BB:CC:01:02:03",
                }
            }
        )

        self.assertEqual(identity, "")

    def test_verification_checks_skipped_fields_from_run_config(self) -> None:
        fields = (EfuseTarget("ALREADY_SET", "1"), EfuseTarget("BURNED", "2"))

        missing = evaluate_efuse_verification(
            fields,
            {"BURNED": {"value": "0x2"}},
        )
        changed = evaluate_efuse_verification(
            fields,
            {
                "ALREADY_SET": {"value": "0"},
                "BURNED": {"value": "2"},
            },
        )

        self.assertEqual(missing, ("ALREADY_SET",))
        self.assertEqual(changed, ("ALREADY_SET",))

    def test_verification_reports_all_mismatches_in_field_order(self) -> None:
        fields = (
            EfuseTarget("MISSING", "1"),
            EfuseTarget("CHANGED", "1"),
            EfuseTarget("MATCHED", "0x2"),
        )

        mismatches = evaluate_efuse_verification(
            fields,
            {
                "CHANGED": {"value": "0"},
                "MATCHED": {"value": "2"},
            },
        )

        self.assertEqual(mismatches, ("MISSING", "CHANGED"))

    def test_verification_prefers_raw_bits_and_rejects_unreadable_value(self) -> None:
        fields = (EfuseTarget("RAW", "1"), EfuseTarget("HIDDEN", "1"))

        mismatches = evaluate_efuse_verification(
            fields,
            {
                "RAW": {
                    "value": "1",
                    "raw_value": "0x0",
                    "bit_len": 1,
                    "readable": True,
                },
                "HIDDEN": {
                    "value": "1",
                    "raw_value": "0x1",
                    "bit_len": 1,
                    "readable": False,
                },
            },
        )

        self.assertEqual(mismatches, ("RAW", "HIDDEN"))

    def test_transport_fingerprint_changes_when_usb_identity_changes(self) -> None:
        first = build_transport_fingerprint(
            device="COM7", serial_number="A100", location="1-2", vid=0x10C4, pid=0xEA60
        )
        replacement = build_transport_fingerprint(
            device="COM7", serial_number="B200", location="1-2", vid=0x10C4, pid=0xEA60
        )

        self.assertNotEqual(first, replacement)


if __name__ == "__main__":
    unittest.main()
