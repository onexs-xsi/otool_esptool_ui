# Third Party Notices

OTool Esptool UI 由 ONEXS 维护。

本工具在功能实现与分发过程中使用了以下第三方项目。分发本工具时，请同时遵循各上游项目自己的许可证、版权声明与引用要求。

> 版本清单更新时间：2026-08-27
>
> “版本发布日期”表示对应上游版本首次正式发布的日期，不是本机安装时间。

## 组件版本清单

| 组件 | 清单版本 | 版本发布日期 | 使用范围 | 许可证 | 官方来源 |
|---|---:|---:|---|---|---|
| esptool | 5.3.1 | 2026-06-29 | 芯片识别、Flash 读写、eFuse 与安全操作 | GPL-2.0-or-later | [GitHub](https://github.com/espressif/esptool) / [v5.3.1](https://github.com/espressif/esptool/releases/tag/v5.3.1) |
| PyQt6 | 6.11.0 | 2026-03-30 | 桌面图形界面 | GPL-3.0-only / Commercial | [Riverbank](https://www.riverbankcomputing.com/software/pyqt/) / [6.11.0](https://pypi.org/project/PyQt6/6.11.0/) |
| pyserial | 3.5 | 2020-11-23 | 串口枚举与访问 | BSD-3-Clause | [GitHub](https://github.com/pyserial/pyserial) / [3.5](https://pypi.org/project/pyserial/3.5/) |
| pyte | 0.8.2 | 2023-11-12 | Unix VTXXX/ANSI 终端屏幕仿真 | LGPL-3.0-only | [GitHub](https://github.com/selectel/pyte) / [0.8.2](https://pypi.org/project/pyte/0.8.2/) |
| wcwidth | 0.8.2 | 2026-06-29 | 终端 Unicode 字符宽度计算 | MIT | [GitHub](https://github.com/jquast/wcwidth) / [0.8.2](https://pypi.org/project/wcwidth/0.8.2/) |
| PyYAML | 6.0.3 | 2025-09-25 | eFuse 配置与校验方案 YAML | MIT | [GitHub](https://github.com/yaml/pyyaml) / [6.0.3](https://pypi.org/project/PyYAML/6.0.3/) |
| littlefs-python | 0.17.1 | 2026-02-10 | LittleFS 镜像读取 | BSD-3-Clause | [GitHub](https://github.com/jrast/littlefs-python) / [0.17.1](https://pypi.org/project/littlefs-python/0.17.1/) |
| bitstring | 4.4.0 | 2026-03-10 | esptool 位数据解析依赖 | MIT | [GitHub](https://github.com/scott-griffiths/bitstring) / [4.4.0](https://pypi.org/project/bitstring/4.4.0/) |
| cryptography | 46.0.7 | 2026-04-08 | 密钥、签名与加密功能 | Apache-2.0 OR BSD-3-Clause | [GitHub](https://github.com/pyca/cryptography) / [46.0.7](https://pypi.org/project/cryptography/46.0.7/) |
| reedsolo | 1.7.0 | 2023-01-17 | esptool Reed-Solomon 编解码依赖 | Public Domain | [GitHub](https://github.com/tomerfiliba/reedsolomon) / [1.7.0](https://pypi.org/project/reedsolo/1.7.0/) |
| intelhex | 2.3.0 | 2020-10-20 | esptool Intel HEX 文件支持 | BSD | [GitHub](https://github.com/python-intelhex/intelhex) / [2.3.0](https://pypi.org/project/intelhex/2.3.0/) |
| rich-click | 1.9.7 | 2026-01-31 | esptool 命令行帮助与输出格式 | MIT | [GitHub](https://github.com/ewels/rich-click) / [1.9.7](https://pypi.org/project/rich-click/1.9.7/) |
| Click | 8.3.2 | 2026-04-03 | esptool 命令行参数解析 | BSD-3-Clause | [GitHub](https://github.com/pallets/click) / [8.3.2](https://pypi.org/project/click/8.3.2/) |
| PyInstaller | 6.19.0 | 2026-02-14 | Windows 单文件程序构建 | GPL-2.0-or-later with bootloader exception | [GitHub](https://github.com/pyinstaller/pyinstaller) / [6.19.0](https://pypi.org/project/pyinstaller/6.19.0/) |

## 版本识别说明

- 应用中的“引用说明”会优先读取当前运行环境的 Python 包版本。
- 冻结版缺少包元数据时，界面使用本文件对应的离线构建清单版本。
- 如果检测版本与离线清单不同，界面会显示实际版本，并将发布日期标记为“未收录”，不会错误套用其他版本的日期。
- esptool 5.3.1 对应 Git 标签 `v5.3.1`、提交 `0d2dfefe029eb48c23ddde61f9118b32d39dc7b9`。

## Notes

- 如果重新分发 `dist/otool_esptool_ui.exe`，请同时保留本文件。
- 完整许可证文本请参考相关依赖项目、已安装包及 esptool 子模块中的 LICENSE 文件或官方说明。
