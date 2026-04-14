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

### 前提

- Windows
- 已激活虚拟环境（含 PyInstaller）
- 已初始化 Git 子模块 `esptool/`

### 安装打包依赖

```powershell
python -m pip install pyinstaller
```

### 执行打包

> **注意**：使用 `python -m PyInstaller` 而非 `pyinstaller` 直接调用，避免 launcher 路径问题。

```powershell
python -m PyInstaller --clean -y otool_esptool_ui.spec
```

### 打包内容

- 主程序 `otool_esptool_ui.py`（同时作为运行时版本号读取来源）
- 本地 Git 子模块 `esptool/` 对应的 `esptool`、`espefuse`、`espsecure` 包
- `bitstring`、`serial` 等运行时依赖
- `THIRD_PARTY_NOTICES.md`、`config.yaml`、`assets/`、`logo_all_size.ico`
- Windows 版本信息（由 spec 从 `__version__` 自动生成）

### 输出结果

```text
dist/
└─ otool_esptool_ui.exe   # 单文件 EXE，约 30–40 MB
```

## Version Management

版本号只需修改 `otool_esptool_ui.py` 一处：

```python
__version__ = "0.1.1"
```

- **程序标题栏**：`constants.py` 在运行时从该文件（源码模式）或 `sys._MEIPASS/otool_esptool_ui.py`（冻结模式）读取
- **EXE 文件属性**：`otool_esptool_ui.spec` 在构建时解析该值生成 `file_version_info.txt`
- **`pyproject.toml`**：应与 `__version__` 手动保持一致

## License

- [OTool Esptool UI - MIT](LICENSE)
