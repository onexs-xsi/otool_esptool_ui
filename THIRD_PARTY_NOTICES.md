# Third Party Notices

OTool Esptool UI 由 ONEXS 维护。分发本工具时请保留本文件，并遵循各上游许可证。

> 构建清单更新时间：2026-10-03。发布日期为该上游版本首次发布的日期。

## 主要组件版本

| 组件 | 构建版本 | 发布日期 | 用途 | 许可证 | 官方来源 |
|---|---|---|---|---|---|
| esptool | 5.4.0 | 2026-09-02 | 芯片识别、Flash 擦除/读写、eFuse 与安全相关操作 | GPL-2.0-or-later | [项目](https://github.com/espressif/esptool) / [版本](https://github.com/espressif/esptool/releases/tag/v5.4.0) |
| PyQt6 | 6.11.0 | 2026-03-30 | 桌面界面、窗口、控件、线程与进程事件集成 | GPL-3.0-only / Commercial | [项目](https://www.riverbankcomputing.com/software/pyqt/) / [版本](https://pypi.org/project/PyQt6/6.11.0/) |
| pyserial | 3.5 | 2020-11-23 | 串口枚举、终端收发与自动校验串口访问 | BSD-3-Clause | [项目](https://github.com/pyserial/pyserial) / [版本](https://pypi.org/project/pyserial/3.5/) |
| pyte | 0.8.2 | 2023-11-12 | Unix VTXXX/ANSI 终端控制序列解析与屏幕仿真 | LGPL-3.0-only | [项目](https://github.com/selectel/pyte) / [版本](https://pypi.org/project/pyte/0.8.2/) |
| wcwidth | 0.9.1 | 2026-09-23 | 终端 Unicode 字符显示宽度计算 | MIT | [项目](https://github.com/jquast/wcwidth) / [版本](https://pypi.org/project/wcwidth/0.9.1/) |
| PyYAML | 6.0.3 | 2025-09-25 | 读取 eFuse 配置与校验方案 YAML | MIT | [项目](https://github.com/yaml/pyyaml) / [版本](https://pypi.org/project/PyYAML/6.0.3/) |
| littlefs-python | 0.19.0 | 2026-08-17 | 分合台中的 LittleFS 镜像读取 | BSD-3-Clause | [项目](https://github.com/jrast/littlefs-python) / [版本](https://pypi.org/project/littlefs-python/0.19.0/) |
| bitstring | 4.4.0 | 2026-03-10 | esptool 位数据解析依赖 | MIT | [项目](https://github.com/scott-griffiths/bitstring) / [版本](https://pypi.org/project/bitstring/4.4.0/) |
| cryptography | 50.0.2 | 2026-09-30 | esptool/espsecure 密钥、签名与加密功能 | Apache-2.0 OR BSD-3-Clause | [项目](https://github.com/pyca/cryptography) / [版本](https://pypi.org/project/cryptography/50.0.2/) |
| reedsolo | 1.7.0 | 2023-01-17 | esptool Reed-Solomon 编解码依赖 | Public Domain | [项目](https://github.com/tomerfiliba/reedsolomon) / [版本](https://pypi.org/project/reedsolo/1.7.0/) |
| intelhex | 2.3.0 | 2020-10-20 | esptool Intel HEX 文件支持 | BSD | [项目](https://github.com/python-intelhex/intelhex) / [版本](https://pypi.org/project/intelhex/2.3.0/) |
| rich-click | 1.9.9 | 2026-09-05 | esptool 命令行帮助和输出格式 | MIT | [项目](https://github.com/ewels/rich-click) / [版本](https://pypi.org/project/rich-click/1.9.9/) |
| Click | 8.5.0 | 2026-08-26 | esptool 命令行参数解析 | BSD-3-Clause | [项目](https://github.com/pallets/click) / [版本](https://pypi.org/project/click/8.5.0/) |
| PyInstaller | 6.22.3 | 2026-09-12 | 构建 Windows 单文件可执行程序 | GPL-2.0-or-later with bootloader exception | [项目](https://github.com/pyinstaller/pyinstaller) / [版本](https://pypi.org/project/pyinstaller/6.22.3/) |
| esp-pylib | 1.1.5 | 2026-09-09 | Espressif 共享日志、串口和命令行支持 | Apache-2.0 | [项目](https://github.com/espressif/esp-pylib) / [版本](https://pypi.org/project/esp-pylib/1.1.5/) |
| Qt6 | 6.11.2 | 2026-08-23 | PyQt6 使用的 Qt 桌面运行库 | LGPL-3.0-only / GPL / Commercial | [项目](https://www.qt.io/) / [版本](https://pypi.org/project/PyQt6-Qt6/6.11.2/) |
| PyInstaller hooks | 2026.8 | 2026-09-30 | 原生扩展和动态依赖的冻结收集规则 | Apache-2.0 / GPL-2.0-or-later | [项目](https://github.com/pyinstaller/pyinstaller-hooks-contrib) / [版本](https://pypi.org/project/pyinstaller-hooks-contrib/2026.8/) |

## 版本与分发说明

- 应用“引用说明”优先读取包元数据；元数据缺失时使用此构建清单。版本不同时不沿用旧版本发布日期。
- esptool 固定到 v5.4.0，提交 `5ac7935ee036f64080a4b2f5cda6c9a6188ae93b`；源码子模块与安装包版本保持一致。
- 完整运行依赖见 `requirements.txt`，完整构建依赖见 `requirements-build.txt`。
- Qt 和 PyQt 的许可证独立于本项目 MIT 许可证；完整文本请查阅包内许可证和上游链接。
- PyInstaller 是构建工具，其 bootloader exception 适用于所生成程序；hooks 中的独立授权以该项目说明为准。
- LittleFS 内核由 littlefs-python 包含，采用 BSD-3-Clause；本工具仅以只读方式浏览镜像。
