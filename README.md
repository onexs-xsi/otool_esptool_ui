# OTool Esptool UI

> ESP 多设备识别 / 擦除 / 烧录 / eFuse 桌面工具 · v0.1.1

## Overview

OTool Esptool UI 是一个面向 Windows 串口量产场景的 PyQt6 桌面工具，提供 ESP32 系列芯片的自动识别、批量擦除、批量烧录、eFuse 读取与烧写能力，并内置 PyInstaller 单文件 EXE 打包方案。

## Features

- 自动扫描串口并识别芯片信息（型号、MAC、Flash 大小、晶振频率）
- 多设备卡片并排显示，实时烧录进度条
- 单台 / 全部 擦除、烧录、停止操作
- 自动烧录模式：新设备接入后自动触发烧录
- 内置 eFuse 对话框，支持读取 summary 与预设字段烧写
- 右键设备卡片 eFuse 按钮可快速烧录预设 eFuse 字段
- 烧录地址自动从文件名末尾解析（如 `firmware_0x10000.bin`）
- 固件目录自动扫描，启动时自动选中最新固件
- 多页签切换：烧录台 / 熔丝台 / 校验台
- Windows 11 任务栏 / 开始菜单显示自定义图标
- 单文件 EXE：通过 `otool_esptool_ui.spec` + PyInstaller 构建

## Python Version

- 最低要求：Python 3.10
- 推荐版本：Python 3.12.x

## Project Structure

```text
otool_esptool_ui/
├─ .gitignore
├─ .gitmodules
├─ LICENSE
├─ README.md
├─ THIRD_PARTY_NOTICES.md
├─ __init__.py            # 包入口，供 python -m otool_esptool_ui 使用
├─ __main__.py
├─ otool_esptool_ui.py    # 版本号单一来源（__version__）
├─ otool_esptool_ui.spec  # PyInstaller 打包规格
├─ pyproject.toml
├─ requirements.txt
├─ config.yaml            # 运行时配置
├─ logo_all_size.ico      # 应用图标（含 16–256px 全尺寸帧）
├─ assets/
│   └─ onexs_avatar.png
├─ firmware/              # 放置待烧录 .bin 固件，启动时自动识别
├─ src/
│   ├─ bootstrap.py       # 启动分发：冻结模式/工作进程/Qt DLL 路径
│   ├─ constants.py       # 路径、版本、工具命令等常量
│   ├─ main_window.py     # 主窗口 + main() 入口
│   ├─ device_card.py     # 设备卡片控件
│   ├─ efuse_dialog.py    # eFuse 对话框
│   ├─ models.py          # 数据模型
│   ├─ helpers.py         # 头像下载等辅助函数
│   ├─ styles.py          # 共享样式表
│   ├─ flow_layout.py     # 自适应流式布局
│   └─ assets/
│       └─ chevron_down.svg
└─ esptool/               # Git 子模块（espressif/esptool）
```

## Quickstart（源码运行）

```powershell
# 1. 克隆并初始化子模块
git clone --recurse-submodules <repo_url>
cd otool_esptool_ui

# 2. 创建并激活虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. 安装依赖
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# 4. 运行
python otool_esptool_ui.py
```

## Packaging

Windows 下运行 `./management_tools.ps1` 可打开管理界面，支持选择 Python 版本、自动下载 Python zip 并初始化 `.venv`、删除环境、运行程序，以及执行打包生成单文件 `dist/otool_esptool_ui.exe`。

## License

- [OTool Esptool UI - MIT](LICENSE)
