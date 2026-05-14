import unittest

from src.constants import resolve_chip_arg


class ConstantsTests(unittest.TestCase):
    def test_resolve_chip_arg_handles_esp32_s31_before_s3_prefix(self) -> None:
        self.assertEqual(resolve_chip_arg("ESP32-S31 (revision v0.0)"), "esp32s31")

    def test_resolve_chip_arg_keeps_esp32_s3_variants(self) -> None:
        self.assertEqual(resolve_chip_arg("ESP32-S3R8 (revision v0.2)"), "esp32s3")

    def test_resolve_chip_arg_unknown_falls_back_to_auto(self) -> None:
        self.assertEqual(resolve_chip_arg("未识别"), "auto")


if __name__ == "__main__":
    unittest.main()
