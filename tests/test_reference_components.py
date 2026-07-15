import unittest
from importlib.metadata import PackageNotFoundError
from pathlib import Path

from src.reference_components import (
    build_reference_notice_html,
    build_reference_notice_text,
    resolve_reference_components,
)


class ReferenceComponentsTests(unittest.TestCase):
    @staticmethod
    def _missing_distribution(name: str) -> str:
        raise PackageNotFoundError(name)

    def test_offline_manifest_has_unique_components_and_release_dates(self) -> None:
        components = resolve_reference_components(self._missing_distribution)

        self.assertGreaterEqual(len(components), 10)
        self.assertEqual(len({item.name for item in components}), len(components))
        self.assertTrue(all(item.version for item in components))
        self.assertTrue(all(item.release_date for item in components))
        self.assertTrue(all(item.version_source == "构建清单" for item in components))

        esptool = next(item for item in components if item.name == "esptool")
        self.assertEqual(esptool.version, "5.3.1")
        self.assertEqual(esptool.release_date, "2026-06-29")
        self.assertIn("0d2dfefe", esptool.version_detail)

    def test_unknown_runtime_version_does_not_reuse_baseline_date(self) -> None:
        components = resolve_reference_components(lambda _name: "99.0.0")

        self.assertTrue(all(item.version_source == "运行环境" for item in components))
        self.assertTrue(all(item.release_date.startswith("未收录") for item in components))
        self.assertTrue(
            all("版本与离线清单不同" in item.version_detail for item in components)
        )

    def test_reference_text_contains_version_date_and_sources(self) -> None:
        text = build_reference_notice_text(
            "OTool", "0.6.1", "ONEXS", "https://example.com", "开发版本"
        )

        self.assertIn("OTool v0.6.1", text)
        self.assertIn("版本发布日期：2026-06-29", text)
        self.assertIn("Git 标签 v5.3.1", text)
        self.assertIn("许可证：GPL-2.0-or-later", text)
        self.assertIn("https://github.com/espressif/esptool", text)

    def test_reference_html_is_escaped_and_has_detailed_columns(self) -> None:
        html = build_reference_notice_html(
            "A < B", "0.6.1", "ONEXS", "https://example.com?a=1&b=2", "开发版本"
        )

        self.assertIn("A &lt; B", html)
        self.assertIn("https://example.com?a=1&amp;b=2", html)
        self.assertIn("详细版本信息", html)
        self.assertIn("版本发布日期", html)
        self.assertIn("许可证 / 来源", html)

    def test_frozen_build_collects_each_component_metadata(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        spec_text = (project_root / "otool_esptool_ui.spec").read_text(
            encoding="utf-8"
        )
        notices_text = (project_root / "THIRD_PARTY_NOTICES.md").read_text(
            encoding="utf-8"
        )

        for component in resolve_reference_components(self._missing_distribution):
            self.assertIn(f'"{component.distribution}"', spec_text)
            self.assertIn(component.name, notices_text)


if __name__ == "__main__":
    unittest.main()
