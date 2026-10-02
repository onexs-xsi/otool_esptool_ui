# OTool Esptool UI

> ESP 烧录 / eFuse / 串口校验 / 固件分合 / 多会话终端 · v0.6.15

OTool Esptool UI 是面向 Windows 的 ESP 桌面工作台，基于 PyQt6 与 esptool，提供五个工作台和可独立运行的单文件 EXE。多设备任务共享串口占用管理，避免不同工作台同时打开同一个串口。

![烧录台](images/README/flash.jpg)

## 五个工作台

### 烧录台

- 扫描串口，以设备卡片显示芯片、MAC、Flash 容量等信息；默认过滤系统和蓝牙串口，可切换“显示全部”。
- 支持单台或批量识别、擦除、烧录、停止，以及整片或指定范围 Flash 导出。
- 多条目烧录支持地址与固件配对；执行前检查文件可读性、空文件、32 位地址范围和 4 KiB 擦除扇区重叠。
- 固件名尾部地址（例如 `firmware_0x10000.bin`）可自动填入；可扫描固件目录并选择最新文件。
- 自动烧录需手动开启；停止任务或关闭自动模式后，已排队的延迟操作不会继续启动。
- 显示结构化进度、操作日志和最终结果，刷新设备不会将已完成结果改回空闲。

### 熔丝台

![熔丝台](images/README/efuse.jpg)

- 选择芯片型号，手动编辑字段，或导入 `burn_efuse_fields` / `efuse_presets` YAML 配置。
- 按设备执行读取、预检、烧写和校验流程，显示各设备状态与错误原因。
- 已满足的字段可跳过；缺失字段、不可读数据、写保护冲突、位宽错误及不允许的 OTP 位变化会阻止烧写。
- 强制重试仍受字段与 OTP 安全检查约束，不表示可以撤销已烧写的位。
- 单设备 eFuse 对话框支持字段搜索、已熔丝筛选、JSON summary 和预设字段；右键快捷入口先预填并确认。
- eFuse 烧写不可逆，执行中禁止强制终止。单设备烧写完成后需重新读取核验。

### 校验台

![校验台](images/README/verify.jpg)

- 可视化步骤编排与 YAML 双视图，支持导入、导出、检查脚本和步骤折叠。
- 支持 `reset`、`wait`、`wait_silence`、`send_text`、`expect`、`capture`、`set_result`、`pass`、`fail`、`clear_buffer`。
- 按包含文本或正则匹配启动日志，使用 all / any / none 条件；可提取参数并组合结果摘要。
- 配置步骤超时、失败重试、串口编码、换行和缓冲上限；增量处理分片中文与 CRLF。
- 设备勾选队列、自动校验、单台开始/停止、批量操作和日志统计。

### 分合台

![分合台](images/README/merge-split.jpg)

图中使用本地生成的离线 LittleFS 演示镜像，不含设备数据。

- 分析合并固件中的分区表，提取 bootloader、分区表和分区原始数据；保留合法的尾部 `0xFF`。
- 按地址合并多份固件，支持 raw BIN、Intel HEX、UF2；检查范围重叠和输出覆盖输入。
- 合并成功的 raw BIN 可发送到烧录台；HEX/UF2 不作为原始二进制烧录条目传递。
- 显示可复制的 MD5、SHA-1、SHA-256；支持从设备读取镜像后分析。
- **LittleFS 文件浏览**：只读挂载、目录浏览和文件提取，保留子目录结构；挂载失败不会格式化镜像副本或显示伪成功。
- FAT / SPIFFS 支持分区识别与原始分区提取，**不支持内部文件浏览**。加密、损坏或参数不匹配的镜像不保证可解析。

### 终端台

![标准终端](images/README/terminal.jpg)

- 多会话串口终端，支持标准终端、文本终端与 Unix 终端模式。
- 配置波特率、数据位、校验、停止位、流控、编码与换行；文本/HEX 收发、时间戳、日志筛选和导出。
- 支持监听、断线重连、快捷发送和复位；后台会话重连不切走当前会话。
- 发送处理部分写入，连续发送时仍保留接收机会。
- 精简模式隐藏应用外壳；全屏可独立切换。

![Unix 终端](images/README/unix-terminal.jpg)

Unix 模式提供 ANSI/VT 屏幕仿真、方向键和控制键、直接键盘输入与粘贴，适合串口 Shell。它不是本机命令行，也不是 SSH 客户端；设备端需要提供对应串口服务。

## 界面与配置

- 小屏或高 DPI 下工作区自动提供滚动，底部导航保持可见；窄窗口下导航与版本信息分两行。
- “引用说明”显示实际组件版本、上游发布日期和来源；无需联网即可查看构建清单。
- `config.yaml` 保存 eFuse 预设与 `verify_profiles`；示例见 `esp32p4_burn_efuse_config_example.yaml`。
- 源码运行时读取项目内配置；单文件 EXE 使用构建时嵌入的默认配置；校验台优先读取 EXE 旁的 `config.yaml`，运行中也可通过界面导入 YAML。
- 默认查找程序附近的 `firmware` 目录；可使用 `OTOOL_ESPTOOL_UI_FIRMWARE_DIR` 指定固件目录。
- Windows 任务栏快捷任务可进入烧录、分合、标准终端或 Unix 终端；也可使用 `--tab 0..4` 与 `--terminal-mode terminal|unix|plain`。

## 源码运行

最低 Python 3.10；本次 Windows x64 发布使用 **Python 3.12.10**。锁定清单面向该构建环境。

```powershell
git clone --recurse-submodules https://github.com/onexs-xsi/otool_esptool_ui.git
cd otool_esptool_ui
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe otool_esptool_ui.py
```

`esptool` 子模块固定到正式版本 v5.4.0。未初始化或不完整时使用安装环境中的 esptool；正式构建前需确保子模块和安装版本一致。普通源码运行无需 PyInstaller。

也可使用管理脚本创建/复用环境，默认 Python 版本为 3.12.10：

```powershell
.\management_tools.ps1 -Action init-env
.\management_tools.ps1 -Action run-app
```

不传参数可打开环境与打包管理界面。Python 可来自系统或脚本下载的本地工具目录；切换已有环境的 Python 版本需要明确重建环境。

## 测试与打包

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m pip check
.\management_tools.ps1 -Action package -PythonVersion 3.12.10
```

- 运行清单：`requirements.txt`；构建清单：`requirements-build.txt`（包含运行依赖与 PyInstaller、hooks、setuptools、pip）。两者均锁定具体版本。
- 依赖声明维护在 `pyproject.toml`；构建清单通过同一依赖源的 `build` extra 生成。刷新清单后必须重跑回归与 EXE 检查。
- 每次构建前递增补丁版本，同步入口 `__version__`、项目元数据、README 当前版本及常量回退版本；失败后的重试同样递增。`file_version_info.txt` 由 spec 自动生成。
- 构建检查 Qt 启动、esptool / espefuse / espsecure 分发、Qt/ICU DLL 与 cryptography 原生扩展，输出 `dist/otool_esptool_ui.exe`。
- 单文件 EXE 不需要本机 Python。请保留 [第三方组件说明](THIRD_PARTY_NOTICES.md)，并按需准备固件与独立 YAML 文件。

界面矩阵可通过 `tests/ui_matrix.py` 离屏复现；它只使用模拟空串口，不访问设备。截图来自软件真实运行界面，未展示虚构烧录结果。

## 本次验证边界

本次使用自动回归、模拟串口、离线固件和实际 EXE 验证；**未连接真实 ESP 设备**。真实 USB 热插拔时序、芯片复位、Flash 擦写结果、板级串口行为及 eFuse 写后结果尚需设备验证。详细结果见 [本次检查记录](review_20261003.md)。

## Changelog

### v0.6.15

- 升级 esptool 5.4.0、终端宽度计算库和构建工具，锁定运行与构建依赖。
- 适配 esp-pylib 日志代理，修复工作进程崩溃，统一 JSON 事件 UTF-8 编码。
- 修复 LittleFS 无效镜像被自动格式化后误报成功，以及有效镜像块参数探测失败。
- 修复空 esptool 子模块误判、窄窗口/高 DPI 内容不可达、编码字段裁切和引用说明窗口过大。
- 修复 EXE 下拉箭头资源路径、内置校验配置读取及后台头像刷新。
- 整合已有设备状态、任务取消、串口互斥、固件处理与校验流程修复。
- 更新五个工作台能力说明、界面截图和第三方组件清单。

### v0.2.0

- 新增「校验台」页：支持复位后启动日志校验、串口发送文本、分步骤等待与多字段匹配
- 新增 `verify_profiles` YAML 脚本格式，可在 `config.yaml` 中维护默认检验脚本
- 校验台新增可视化积木编辑器、步骤折叠、参数读取（`capture`）与结果展示（`set_result`）
- 校验台设备队列支持勾选批量开始、全选 / 全不选、双击单台开始 / 停止，日志下移优化排版
- 校验台新增 `wait_silence` / `pass` / `fail` 高级步骤，以及步骤级失败重试能力
- 校验台新增脚本摘要 / 设备状态统计胶囊，复杂脚本和多设备并跑时更易读
- 新增「熔丝台」页：批量 eFuse 烧写，支持多设备并联、热插拔、芯片型号校验
- 熔丝台字段配置：手动添加 / YAML 文件导入（burn_efuse_fields / efuse_presets 双格式）
- 强制烧写（黄色按钮）：跳过预检、对已满足字段再次执行烧录
- 各页签 toolbar 控件独立分离（烧录台/熔丝台互不干扰）
- 多条目烧录（多组地址+固件）
- 清空设备按钮
- eFuse 对话框 JSON 解析 + 5 列布局优化
- 默认窗口尺寸 ×1.2
- 提供 `esp32p4_burn_efuse_config_example.yaml` 配置示例

### v0.1.x

- 初始版本：多设备识别、擦除、烧录、自动烧录

## License

本项目源码采用 [MIT](LICENSE)。第三方依赖遵循各自许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
