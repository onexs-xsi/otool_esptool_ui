from __future__ import annotations

from dataclasses import dataclass
from html import escape
from importlib.metadata import PackageNotFoundError, version as distribution_version
from typing import Callable


@dataclass(frozen=True)
class _ComponentSpec:
    name: str
    distribution: str
    baseline_version: str
    release_date: str
    purpose: str
    license_name: str
    source_url: str
    release_url: str
    scope: str
    version_detail: str = ""


@dataclass(frozen=True)
class ReferenceComponent:
    name: str
    distribution: str
    version: str
    release_date: str
    purpose: str
    license_name: str
    source_url: str
    release_url: str
    scope: str
    version_source: str
    version_detail: str


# 离线版本清单。发布日期取自对应上游正式发布页/PyPI release 记录，
# 表示“该版本首次正式发布时间”，不是本机安装时间。
_COMPONENT_SPECS: tuple[_ComponentSpec, ...] = (
    _ComponentSpec(
        name="esptool",
        distribution="esptool",
        baseline_version="5.3.1",
        release_date="2026-06-29",
        purpose="芯片识别、Flash 擦除/读写、eFuse 与安全相关操作",
        license_name="GPL-2.0-or-later",
        source_url="https://github.com/espressif/esptool",
        release_url="https://github.com/espressif/esptool/releases/tag/v5.3.1",
        scope="核心运行组件",
        version_detail="Git 标签 v5.3.1 · commit 0d2dfefe",
    ),
    _ComponentSpec(
        name="PyQt6",
        distribution="PyQt6",
        baseline_version="6.11.0",
        release_date="2026-03-30",
        purpose="桌面界面、窗口、控件、线程与进程事件集成",
        license_name="GPL-3.0-only / Commercial",
        source_url="https://www.riverbankcomputing.com/software/pyqt/",
        release_url="https://pypi.org/project/PyQt6/6.11.0/",
        scope="核心运行组件",
    ),
    _ComponentSpec(
        name="pyserial",
        distribution="pyserial",
        baseline_version="3.5",
        release_date="2020-11-23",
        purpose="串口枚举、终端收发与自动校验串口访问",
        license_name="BSD-3-Clause",
        source_url="https://github.com/pyserial/pyserial",
        release_url="https://pypi.org/project/pyserial/3.5/",
        scope="核心运行组件",
    ),
    _ComponentSpec(
        name="pyte",
        distribution="pyte",
        baseline_version="0.8.2",
        release_date="2023-11-12",
        purpose="Unix VTXXX/ANSI 终端控制序列解析与屏幕仿真",
        license_name="LGPL-3.0-only",
        source_url="https://github.com/selectel/pyte",
        release_url="https://pypi.org/project/pyte/0.8.2/",
        scope="核心运行组件",
    ),
    _ComponentSpec(
        name="wcwidth",
        distribution="wcwidth",
        baseline_version="0.8.2",
        release_date="2026-06-29",
        purpose="终端 Unicode 字符显示宽度计算",
        license_name="MIT",
        source_url="https://github.com/jquast/wcwidth",
        release_url="https://pypi.org/project/wcwidth/0.8.2/",
        scope="终端仿真依赖",
    ),
    _ComponentSpec(
        name="PyYAML",
        distribution="PyYAML",
        baseline_version="6.0.3",
        release_date="2025-09-25",
        purpose="读取 eFuse 配置与校验方案 YAML",
        license_name="MIT",
        source_url="https://github.com/yaml/pyyaml",
        release_url="https://pypi.org/project/PyYAML/6.0.3/",
        scope="核心运行组件",
    ),
    _ComponentSpec(
        name="littlefs-python",
        distribution="littlefs-python",
        baseline_version="0.17.1",
        release_date="2026-02-10",
        purpose="分合台中的 LittleFS 镜像读取",
        license_name="BSD-3-Clause",
        source_url="https://github.com/jrast/littlefs-python",
        release_url="https://pypi.org/project/littlefs-python/0.17.1/",
        scope="可选运行组件",
    ),
    _ComponentSpec(
        name="bitstring",
        distribution="bitstring",
        baseline_version="4.4.0",
        release_date="2026-03-10",
        purpose="esptool 位数据解析依赖",
        license_name="MIT",
        source_url="https://github.com/scott-griffiths/bitstring",
        release_url="https://pypi.org/project/bitstring/4.4.0/",
        scope="esptool 运行依赖",
    ),
    _ComponentSpec(
        name="cryptography",
        distribution="cryptography",
        baseline_version="46.0.7",
        release_date="2026-04-08",
        purpose="esptool/espsecure 密钥、签名与加密功能",
        license_name="Apache-2.0 OR BSD-3-Clause",
        source_url="https://github.com/pyca/cryptography",
        release_url="https://pypi.org/project/cryptography/46.0.7/",
        scope="安全功能依赖",
    ),
    _ComponentSpec(
        name="reedsolo",
        distribution="reedsolo",
        baseline_version="1.7.0",
        release_date="2023-01-17",
        purpose="esptool Reed-Solomon 编解码依赖",
        license_name="Public Domain",
        source_url="https://github.com/tomerfiliba/reedsolomon",
        release_url="https://pypi.org/project/reedsolo/1.7.0/",
        scope="esptool 运行依赖",
    ),
    _ComponentSpec(
        name="intelhex",
        distribution="intelhex",
        baseline_version="2.3.0",
        release_date="2020-10-20",
        purpose="esptool Intel HEX 文件支持",
        license_name="BSD",
        source_url="https://github.com/python-intelhex/intelhex",
        release_url="https://pypi.org/project/intelhex/2.3.0/",
        scope="esptool 运行依赖",
    ),
    _ComponentSpec(
        name="rich-click",
        distribution="rich-click",
        baseline_version="1.9.7",
        release_date="2026-01-31",
        purpose="esptool 命令行帮助和输出格式",
        license_name="MIT",
        source_url="https://github.com/ewels/rich-click",
        release_url="https://pypi.org/project/rich-click/1.9.7/",
        scope="esptool 运行依赖",
    ),
    _ComponentSpec(
        name="Click",
        distribution="click",
        baseline_version="8.3.2",
        release_date="2026-04-03",
        purpose="esptool 命令行参数解析",
        license_name="BSD-3-Clause",
        source_url="https://github.com/pallets/click",
        release_url="https://pypi.org/project/click/8.3.2/",
        scope="esptool 运行依赖",
    ),
    _ComponentSpec(
        name="PyInstaller",
        distribution="PyInstaller",
        baseline_version="6.19.0",
        release_date="2026-02-14",
        purpose="构建 Windows 单文件可执行程序",
        license_name="GPL-2.0-or-later with bootloader exception",
        source_url="https://github.com/pyinstaller/pyinstaller",
        release_url="https://pypi.org/project/pyinstaller/6.19.0/",
        scope="构建工具",
    ),
)


def resolve_reference_components(
    version_resolver: Callable[[str], str] | None = None,
) -> tuple[ReferenceComponent, ...]:
    """Return the detected component versions with an honest offline fallback."""

    resolver = version_resolver or distribution_version
    components: list[ReferenceComponent] = []
    for spec in _COMPONENT_SPECS:
        try:
            detected_version = resolver(spec.distribution)
            version_source = "运行环境"
        except (PackageNotFoundError, KeyError, ValueError):
            detected_version = spec.baseline_version
            version_source = "构建清单"

        if detected_version == spec.baseline_version:
            release_date = spec.release_date
            version_detail = spec.version_detail or f"Python 包：{spec.distribution}"
        else:
            release_date = (
                f"未收录（清单 {spec.baseline_version}：{spec.release_date}）"
            )
            version_detail = f"Python 包：{spec.distribution}；版本与离线清单不同"

        components.append(
            ReferenceComponent(
                name=spec.name,
                distribution=spec.distribution,
                version=detected_version,
                release_date=release_date,
                purpose=spec.purpose,
                license_name=spec.license_name,
                source_url=spec.source_url,
                release_url=spec.release_url,
                scope=spec.scope,
                version_source=version_source,
                version_detail=version_detail,
            )
        )
    return tuple(components)


def build_reference_notice_text(
    app_title: str,
    app_version: str,
    author: str,
    github_url: str,
    build_time: str,
) -> str:
    lines = [
        f"{app_title} v{app_version}",
        f"构建时间：{build_time}",
        f"Coder：{author}",
        f"GitHub：{github_url}",
        "",
        "组件版本明细",
        "版本发布日期表示上游首次正式发布时间，不是本机安装时间。",
    ]
    for component in resolve_reference_components():
        lines.extend(
            [
                "",
                f"- {component.name} [{component.scope}]",
                f"  版本：{component.version}（{component.version_source}）",
                f"  版本详情：{component.version_detail}",
                f"  版本发布日期：{component.release_date}",
                f"  用途：{component.purpose}",
                f"  许可证：{component.license_name}",
                f"  官方来源：{component.source_url}",
                f"  版本页面：{component.release_url}",
            ]
        )
    lines.extend(
        [
            "",
            "分发本工具时，请一并保留各上游项目的许可证与引用说明。",
            "完整清单见仓库根目录 THIRD_PARTY_NOTICES.md。",
        ]
    )
    return "\n".join(lines)


def build_reference_notice_html(
    app_title: str,
    app_version: str,
    author: str,
    github_url: str,
    build_time: str,
) -> str:
    def e(value: str) -> str:
        return escape(value, quote=True)

    rows: list[str] = []
    for component in resolve_reference_components():
        rows.append(
            "".join(
                [
                    "<tr>",
                    "<td>",
                    f"<b>{e(component.name)}</b><br>",
                    f"<span class='muted'>{e(component.scope)}</span>",
                    "</td>",
                    "<td>",
                    f"<b>{e(component.version)}</b><br>",
                    f"<span class='muted'>{e(component.version_source)} · "
                    f"{e(component.version_detail)}</span>",
                    "</td>",
                    "<td>",
                    f"<a href='{e(component.release_url)}'>"
                    f"{e(component.release_date)}</a>",
                    "</td>",
                    f"<td>{e(component.purpose)}</td>",
                    "<td>",
                    f"{e(component.license_name)}<br>",
                    f"<a href='{e(component.source_url)}'>官方来源</a>",
                    "</td>",
                    "</tr>",
                ]
            )
        )

    return f"""
    <html>
    <head>
      <style>
        body {{ color: #263449; font-family: 'Segoe UI', 'Microsoft YaHei'; }}
        h2 {{ color: #183b78; margin-bottom: 4px; }}
        p {{ line-height: 1.45; }}
        .meta {{ color: #52647f; margin-bottom: 12px; }}
        .note {{ background: #eef5ff; border: 1px solid #c9dcff;
                 padding: 8px; color: #31527f; }}
        .muted {{ color: #718096; font-size: 11px; }}
        table {{ border-collapse: collapse; width: 100%; margin-top: 12px; }}
        th {{ background: #edf3fb; color: #294668; text-align: left;
              border: 1px solid #cdd8e8; padding: 7px; }}
        td {{ border: 1px solid #d8e0eb; padding: 7px; vertical-align: top; }}
        a {{ color: #2560e0; text-decoration: none; }}
      </style>
    </head>
    <body>
      <h2>{e(app_title)} v{e(app_version)}</h2>
      <p class="meta">构建时间：{e(build_time)}　·　Coder：{e(author)}　·　
        <a href="{e(github_url)}">项目主页</a>
      </p>
      <p class="note"><b>时间口径：</b>“版本发布日期”表示对应上游版本首次正式发布的日期，
      不是本机安装时间。运行版本与离线清单不一致时会明确显示“未收录”，不会套用错误日期。</p>
      <table cellspacing="0" cellpadding="0">
        <thead>
          <tr>
            <th width="13%">组件</th>
            <th width="22%">详细版本信息</th>
            <th width="15%">版本发布日期</th>
            <th width="30%">用途</th>
            <th width="20%">许可证 / 来源</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
      <p class="note">分发本工具时，请一并保留各上游项目的许可证与引用说明。
      完整清单见仓库根目录 <b>THIRD_PARTY_NOTICES.md</b>。</p>
    </body>
    </html>
    """
