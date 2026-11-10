import unittest

from src.constants import resolve_chip_arg


class ConstantsTests(unittest.TestCase):
    def test_resolve_chip_arg_handles_esp32_s31_before_s3_prefix(self) -> None:
        self.assertEqual(resolve_chip_arg("ESP32-S31 (revision v0.0)"), "esp32s31")

    def test_resolve_chip_arg_keeps_esp32_s3_variants(self) -> None:
        self.assertEqual(resolve_chip_arg("ESP32-S3R8 (revision v0.2)"), "esp32s3")

    def test_resolve_chip_arg_unknown_falls_back_to_auto(self) -> None:
        self.assertEqual(resolve_chip_arg("未识别"), "auto")

    def test_project_chip_comparison_handles_targets_and_longer_prefixes(self) -> None:
        for name, target in [
            ("ESP32-C61 (revision v0.0)", "esp32c61"),
            ("ESP32-H21", "esp32h21"),
            ("ESP32-H4", "esp32h4"),
            ("ESP32-E22", "esp32e22"),
            ("ESP8266", "esp8266"),
            ("esp32c6", "esp32c6"),
        ]:
            with self.subTest(name=name):
                self.assertEqual(resolve_chip_arg(name), target)


if __name__ == "__main__":
    unittest.main()
