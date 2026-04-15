param(
    [ValidateSet('gui', 'init-env', 'delete-env', 'run-app', 'package')]
    [string]$Action = 'gui',

    [string]$PythonVersion = '3.12.10',

    [ValidateSet('auto', 'system', 'online')]
    [string]$PythonSource = 'auto',

    [switch]$Recreate,
    [switch]$SkipUpgradePip,
    [switch]$SkipDependencyInstall,
    [switch]$NoClean
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Script:ScriptPath       = $MyInvocation.MyCommand.Path
$Script:ProjectRoot      = Split-Path -Parent $Script:ScriptPath
$Script:ToolsRoot        = Join-Path $Script:ProjectRoot '.tools'
$Script:PythonCacheRoot  = Join-Path $Script:ToolsRoot 'python-cache'
$Script:PythonInstallRoot = Join-Path $Script:ToolsRoot 'python'
$Script:VenvDir          = Join-Path $Script:ProjectRoot '.venv'
$Script:VenvPython       = Join-Path $Script:VenvDir 'Scripts\python.exe'
$Script:MainScript       = Join-Path $Script:ProjectRoot 'otool_esptool_ui.py'
$Script:RequirementsFile = Join-Path $Script:ProjectRoot 'requirements.txt'
$Script:SpecFile         = Join-Path $Script:ProjectRoot 'otool_esptool_ui.spec'
$Script:PackageOutput    = Join-Path $Script:ProjectRoot 'dist\otool_esptool_ui.exe'
$Script:EsptoolDir       = Join-Path $Script:ProjectRoot 'esptool'

$Script:KnownOnlineVersions = @('3.10.11', '3.11.9', '3.12.10', '3.13.2')
$Script:RecommendedVersion  = '3.12.10'

# ─── Utility helpers ──────────────────────────────────────────────

function Write-Step {
    param([Parameter(Mandatory = $true)] [string]$Message)
    Write-Host "[mgmt] $Message" -ForegroundColor Cyan
}

function Ensure-Directory {
    param([Parameter(Mandatory = $true)] [string]$Path)
    if (-not (Test-Path $Path)) { New-Item -ItemType Directory -Path $Path | Out-Null }
}

function Invoke-CheckedExternal {
    param(
        [Parameter(Mandatory = $true)] [string]$FilePath,
        [Parameter(Mandatory = $true)] [string[]]$Arguments,
        [Parameter(Mandatory = $true)] [string]$Description
    )
    Write-Step $Description
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Description failed (exit $LASTEXITCODE)" }
}

function Get-DisplayValue {
    param([string]$Value, [string]$Fallback = 'unknown')
    if ([string]::IsNullOrWhiteSpace($Value)) { return $Fallback }
    return $Value
}

# ─── Python path helpers ──────────────────────────────────────────

function Get-PortablePythonExePath {
    param([Parameter(Mandatory = $true)] [string]$Version)
    return Join-Path (Join-Path $Script:PythonInstallRoot $Version) 'tools\python.exe'
}

function Get-PortablePythonZipPath {
    param([Parameter(Mandatory = $true)] [string]$Version)
    return Join-Path $Script:PythonCacheRoot ("python-{0}.zip" -f $Version)
}

function Get-PortablePythonUri {
    param([Parameter(Mandatory = $true)] [string]$Version)
    return "https://www.nuget.org/api/v2/package/python/$Version"
}

function Get-InterpreterVersion {
    param([Parameter(Mandatory = $true)] [string]$PythonExe)
    if (-not (Test-Path $PythonExe)) { return $null }
    $output = & $PythonExe -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return ($output | Select-Object -Last 1).Trim()
}

function Get-VenvVersion { return Get-InterpreterVersion -PythonExe $Script:VenvPython }

# ─── System Python detection ─────────────────────────────────────

function Find-SystemPythonVersions {
    $found = [ordered]@{}

    # Method 1: py launcher
    $pyCmd = Get-Command 'py' -CommandType Application -ErrorAction SilentlyContinue
    if ($pyCmd) {
        try {
            $lines = & py -0p 2>$null
            if ($LASTEXITCODE -eq 0 -and $lines) {
                foreach ($line in $lines) {
                    if ($line -match '([A-Za-z]:\\.+?)\s*$') {
                        $exePath = $Matches[1].Trim()
                        if (Test-Path $exePath) {
                            $ver = Get-InterpreterVersion -PythonExe $exePath
                            if ($ver -and -not $found.Contains($ver)) { $found[$ver] = $exePath }
                        }
                    }
                }
            }
        } catch { }
    }

    # Method 2: where.exe python
    try {
        $paths = & where.exe python 2>$null
        if ($LASTEXITCODE -eq 0 -and $paths) {
            foreach ($p in $paths) {
                $p = $p.Trim()
                if ($p -and (Test-Path $p) -and ($p -notmatch 'WindowsApps')) {
                    $ver = Get-InterpreterVersion -PythonExe $p
                    if ($ver -and -not $found.Contains($ver)) { $found[$ver] = $p }
                }
            }
        }
    } catch { }

    return $found
}

function Build-VersionList {
    param([Parameter(Mandatory = $true)] $SystemVersions)

    $list = @()
    $rec = $Script:RecommendedVersion

    # System versions (descending)
    $sysKeys = @($SystemVersions.Keys)
    if ($sysKeys.Count -gt 0) {
        $sorted = $sysKeys | Sort-Object { [version]$_ } -Descending
        foreach ($v in $sorted) {
            $suffix = if ($v -eq $rec) { ' (推荐)' } else { '' }
            $list += ,@{ Version = $v; Source = 'system'; Path = $SystemVersions[$v]; Display = "$v [系统]$suffix" }
        }
    }

    # Known online versions not already in system (descending)
    $onSorted = $Script:KnownOnlineVersions | Sort-Object { [version]$_ } -Descending
    foreach ($v in $onSorted) {
        if (-not $SystemVersions.Contains($v)) {
            $suffix = if ($v -eq $rec) { ' (推荐)' } else { '' }
            $list += ,@{ Version = $v; Source = 'online'; Path = $null; Display = "$v [在线]$suffix" }
        }
    }

    return ,$list
}

# ─── Bootstrap resolver ──────────────────────────────────────────

function Resolve-BootstrapPython {
    param(
        [Parameter(Mandatory = $true)] [string]$Version,
        [Parameter(Mandatory = $true)] [string]$Source
    )

    if ($Source -eq 'system') {
        $sys = Find-SystemPythonVersions
        if ($sys.Contains($Version)) {
            Write-Step "Using system Python ${Version}: $($sys[$Version])"
            return $sys[$Version]
        }
        throw "系统中未找到 Python $Version"
    }

    if ($Source -eq 'online') {
        return Ensure-PortablePython -Version $Version
    }

    # auto: prefer system, fallback to online
    $sys = Find-SystemPythonVersions
    if ($sys.Contains($Version)) {
        Write-Step "Using system Python ${Version}: $($sys[$Version])"
        return $sys[$Version]
    }
    return Ensure-PortablePython -Version $Version
}

function Ensure-PortablePython {
    param([Parameter(Mandatory = $true)] [string]$Version)

    $pythonExe = Get-PortablePythonExePath -Version $Version
    if (Test-Path $pythonExe) { return $pythonExe }

    Ensure-Directory -Path $Script:ToolsRoot
    Ensure-Directory -Path $Script:PythonCacheRoot
    Ensure-Directory -Path $Script:PythonInstallRoot

    $zipPath = Get-PortablePythonZipPath -Version $Version
    if (-not (Test-Path $zipPath)) {
        Write-Step "Downloading Python $Version from NuGet"
        Invoke-WebRequest -Uri (Get-PortablePythonUri -Version $Version) -OutFile $zipPath
    }

    $installRoot = Join-Path $Script:PythonInstallRoot $Version
    if (Test-Path $installRoot) { Remove-Item -Path $installRoot -Recurse -Force }

    Write-Step "Extracting Python $Version"
    Expand-Archive -Path $zipPath -DestinationPath $installRoot -Force

    if (-not (Test-Path $pythonExe)) { throw "Portable Python extraction failed: $pythonExe" }
    return $pythonExe
}

# ─── Prerequisite checks ─────────────────────────────────────────

function Ensure-EnvironmentPrerequisites {
    if (-not (Test-Path $Script:RequirementsFile)) { throw "Missing: $Script:RequirementsFile" }
    if (-not (Test-Path $Script:MainScript))       { throw "Missing: $Script:MainScript" }
}

function Ensure-PackagePrerequisites {
    Ensure-EnvironmentPrerequisites
    if (-not (Test-Path $Script:SpecFile))   { throw "Missing: $Script:SpecFile" }
    if (-not (Test-Path $Script:EsptoolDir)) { throw "Missing esptool. Run: git submodule update --init --recursive" }
}

# ─── Core actions ─────────────────────────────────────────────────

function Remove-Environment {
    if (Test-Path $Script:VenvDir) {
        Write-Step 'Removing .venv'
        Remove-Item -Path $Script:VenvDir -Recurse -Force
        Write-Step '.venv removed'
    } else {
        Write-Step '.venv not found'
    }
}

function Initialize-Environment {
    param(
        [Parameter(Mandatory = $true)] [string]$Version,
        [string]$Source = 'auto',
        [switch]$RecreateEnvironment,
        [switch]$SkipPipUpgrade,
        [switch]$SkipRequirementInstall
    )

    Ensure-EnvironmentPrerequisites

    $currentVer = Get-VenvVersion
    if (Test-Path $Script:VenvPython) {
        if ($RecreateEnvironment) {
            Remove-Environment
        }
        elseif ($currentVer -and $currentVer -ne $Version) {
            throw ".venv uses Python ${currentVer}. Use -Recreate or delete first."
        }
        else {
            Write-Step "Reusing existing .venv (Python $(Get-DisplayValue -Value $currentVer))"
        }
    }

    if (-not (Test-Path $Script:VenvPython)) {
        $bootstrap = Resolve-BootstrapPython -Version $Version -Source $Source
        Invoke-CheckedExternal -FilePath $bootstrap `
            -Arguments @('-m', 'venv', $Script:VenvDir) `
            -Description "Creating .venv with Python $Version"
    }

    if (-not (Test-Path $Script:VenvPython)) { throw "venv creation failed: $Script:VenvPython" }

    if (-not $SkipPipUpgrade) {
        Invoke-CheckedExternal -FilePath $Script:VenvPython `
            -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip') `
            -Description 'Upgrading pip'
    }

    if (-not $SkipRequirementInstall) {
        Invoke-CheckedExternal -FilePath $Script:VenvPython `
            -Arguments @('-m', 'pip', 'install', '-r', $Script:RequirementsFile) `
            -Description 'Installing project requirements'
    }

    Write-Step "Environment ready: .venv (Python $(Get-VenvVersion))"
}

function Start-AppProcess {
    Ensure-EnvironmentPrerequisites
    if (-not (Test-Path $Script:VenvPython)) { throw '.venv not found. Initialize the environment first.' }
    Write-Step 'Starting application'
    $proc = Start-Process -FilePath $Script:VenvPython `
        -ArgumentList @($Script:MainScript) `
        -WorkingDirectory $Script:ProjectRoot `
        -WindowStyle Hidden -PassThru
    Write-Step "Application started (PID $($proc.Id))"
}

function Build-Package {
    param(
        [Parameter(Mandatory = $true)] [string]$Version,
        [string]$Source = 'auto',
        [switch]$RecreateEnvironment,
        [switch]$SkipPipUpgrade,
        [switch]$SkipRequirementInstall,
        [switch]$DisableCleanBuild
    )

    Ensure-PackagePrerequisites
    Initialize-Environment -Version $Version -Source $Source `
        -RecreateEnvironment:$RecreateEnvironment `
        -SkipPipUpgrade:$SkipPipUpgrade `
        -SkipRequirementInstall:$SkipRequirementInstall

    Invoke-CheckedExternal -FilePath $Script:VenvPython `
        -Arguments @('-m', 'pip', 'install', 'pyinstaller') `
        -Description 'Installing PyInstaller'

    $pyiArgs = @('-m', 'PyInstaller')
    if (-not $DisableCleanBuild) { $pyiArgs += '--clean' }
    $pyiArgs += @('-y', $Script:SpecFile)

    Invoke-CheckedExternal -FilePath $Script:VenvPython -Arguments $pyiArgs -Description 'Running PyInstaller'

    if (-not (Test-Path $Script:PackageOutput)) { throw "Package not found: $Script:PackageOutput" }
    Write-Step "Package ready: $Script:PackageOutput"
}

# ─── GUI helpers ──────────────────────────────────────────────────

function Add-UiLog {
    param(
        [Parameter(Mandatory = $true)] $TextBox,
        [Parameter(Mandatory = $true)] [string]$Message
    )
    $TextBox.AppendText("[$(Get-Date -Format 'HH:mm:ss')] $Message`r`n")
}

function Show-UiError {
    param([Parameter(Mandatory = $true)] [string]$Message)
    [void][System.Windows.Forms.MessageBox]::Show(
        $Message, 'OTool 管理工具',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error)
}

function Start-ManagedActionWindow {
    param(
        [Parameter(Mandatory = $true)] [string]$RequestedAction,
        [Parameter(Mandatory = $true)] [string]$Version,
        [string]$VersionSource = 'auto',
        [switch]$RecreateEnvironment,
        [switch]$DisableCleanBuild
    )

    $ps = (Get-Command powershell -CommandType Application).Source
    $argList = @(
        '-NoExit', '-ExecutionPolicy', 'Bypass',
        '-File', ('"{0}"' -f $Script:ScriptPath),
        '-Action', $RequestedAction,
        '-PythonVersion', $Version,
        '-PythonSource', $VersionSource
    )
    if ($RecreateEnvironment) { $argList += '-Recreate' }
    if ($DisableCleanBuild)   { $argList += '-NoClean' }

    Start-Process -FilePath $ps -ArgumentList $argList -WorkingDirectory $Script:ProjectRoot | Out-Null
}

# ─── Management UI ────────────────────────────────────────────────

function Show-ManagementUi {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()

    # ── Scan versions ──────────────────────────────────────────
    $sysVersions = Find-SystemPythonVersions
    $initList    = Build-VersionList -SystemVersions $sysVersions

    # Shared mutable state accessible from event handlers
    $ui = @{
        List   = (New-Object System.Collections.ArrayList)
        Custom = (New-Object System.Collections.ArrayList)
    }
    foreach ($item in $initList) { [void]$ui.List.Add($item) }

    # ── Form ───────────────────────────────────────────────────
    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'OTool 管理工具'
    $form.StartPosition = 'CenterScreen'
    $form.Size = New-Object System.Drawing.Size(730, 530)
    $form.MinimumSize = New-Object System.Drawing.Size(700, 460)
    $form.Font = New-Object System.Drawing.Font('Segoe UI', 9)

    # ── Main layout ────────────────────────────────────────────
    $main = New-Object System.Windows.Forms.TableLayoutPanel
    $main.Dock = 'Fill'
    $main.ColumnCount = 1
    $main.RowCount = 5
    $main.Padding = New-Object System.Windows.Forms.Padding(10)

    [void]$main.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 100)))
    [void]$main.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute, 66)))
    [void]$main.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute, 84)))
    [void]$main.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute, 58)))
    [void]$main.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute, 6)))
    [void]$main.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 100)))

    # ─── Row 0 : Header ──────────────────────────────────────
    $header = New-Object System.Windows.Forms.Panel
    $header.Dock = 'Fill'

    $lblTitle = New-Object System.Windows.Forms.Label
    $lblTitle.Text = 'OTool 管理工具'
    $lblTitle.Font = New-Object System.Drawing.Font('Segoe UI', 13, [System.Drawing.FontStyle]::Bold)
    $lblTitle.Location = New-Object System.Drawing.Point(0, 0)
    $lblTitle.AutoSize = $true

    $lblProject = New-Object System.Windows.Forms.Label
    $lblProject.Text = "项目路径: $Script:ProjectRoot"
    $lblProject.Location = New-Object System.Drawing.Point(2, 28)
    $lblProject.AutoSize = $true
    $lblProject.ForeColor = [System.Drawing.Color]::Gray

    $lblStatus = New-Object System.Windows.Forms.Label
    $lblStatus.Location = New-Object System.Drawing.Point(2, 46)
    $lblStatus.AutoSize = $true

    [void]$header.Controls.Add($lblTitle)
    [void]$header.Controls.Add($lblProject)
    [void]$header.Controls.Add($lblStatus)
    [void]$main.Controls.Add($header, 0, 0)

    # ─── Row 1 : Python 版本 GroupBox ─────────────────────────
    $grpVersion = New-Object System.Windows.Forms.GroupBox
    $grpVersion.Text = 'Python 版本'
    $grpVersion.Dock = 'Fill'

    $flowVer = New-Object System.Windows.Forms.FlowLayoutPanel
    $flowVer.Dock = 'Fill'
    $flowVer.FlowDirection = 'LeftToRight'
    $flowVer.WrapContents = $false
    $flowVer.Padding = New-Object System.Windows.Forms.Padding(4, 8, 0, 0)

    $cboVersion = New-Object System.Windows.Forms.ComboBox
    $cboVersion.Width = 220
    $cboVersion.DropDownStyle = 'DropDownList'
    $recIdx = 0
    for ($i = 0; $i -lt $ui.List.Count; $i++) {
        [void]$cboVersion.Items.Add($ui.List[$i].Display)
        if ($ui.List[$i].Version -eq $Script:RecommendedVersion) { $recIdx = $i }
    }
    if ($cboVersion.Items.Count -gt 0) { $cboVersion.SelectedIndex = $recIdx }

    $lblSep = New-Object System.Windows.Forms.Label
    $lblSep.Text = '|'
    $lblSep.AutoSize = $true
    $lblSep.Margin = New-Object System.Windows.Forms.Padding(10, 7, 4, 0)
    $lblSep.ForeColor = [System.Drawing.Color]::Silver

    $lblInput = New-Object System.Windows.Forms.Label
    $lblInput.Text = '手动输入在线版本:'
    $lblInput.AutoSize = $true
    $lblInput.Margin = New-Object System.Windows.Forms.Padding(0, 7, 0, 0)

    $txtCustom = New-Object System.Windows.Forms.TextBox
    $txtCustom.Width = 90
    $txtCustom.Margin = New-Object System.Windows.Forms.Padding(4, 4, 0, 0)

    $btnAdd = New-Object System.Windows.Forms.Button
    $btnAdd.Text = '添加'
    $btnAdd.Width = 50
    $btnAdd.Margin = New-Object System.Windows.Forms.Padding(4, 3, 0, 0)

    $btnRefresh = New-Object System.Windows.Forms.Button
    $btnRefresh.Text = '刷新'
    $btnRefresh.Width = 50
    $btnRefresh.Margin = New-Object System.Windows.Forms.Padding(12, 3, 0, 0)

    [void]$flowVer.Controls.Add($cboVersion)
    [void]$flowVer.Controls.Add($lblSep)
    [void]$flowVer.Controls.Add($lblInput)
    [void]$flowVer.Controls.Add($txtCustom)
    [void]$flowVer.Controls.Add($btnAdd)
    [void]$flowVer.Controls.Add($btnRefresh)
    [void]$grpVersion.Controls.Add($flowVer)
    [void]$main.Controls.Add($grpVersion, 0, 1)

    # ─── Row 2 : 操作 GroupBox ────────────────────────────────
    $grpAction = New-Object System.Windows.Forms.GroupBox
    $grpAction.Text = '操作'
    $grpAction.Dock = 'Fill'

    $flowAct = New-Object System.Windows.Forms.FlowLayoutPanel
    $flowAct.Dock = 'Fill'
    $flowAct.FlowDirection = 'LeftToRight'
    $flowAct.WrapContents = $false
    $flowAct.Padding = New-Object System.Windows.Forms.Padding(4, 6, 0, 0)

    $btnInit = New-Object System.Windows.Forms.Button
    $btnInit.Text = '初始化环境'
    $btnInit.Width = 90

    $btnDelete = New-Object System.Windows.Forms.Button
    $btnDelete.Text = '删除环境'
    $btnDelete.Width = 80

    $btnRun = New-Object System.Windows.Forms.Button
    $btnRun.Text = '运行程序'
    $btnRun.Width = 80

    $btnPackage = New-Object System.Windows.Forms.Button
    $btnPackage.Text = '打包程序'
    $btnPackage.Width = 80

    $chkRecreate = New-Object System.Windows.Forms.CheckBox
    $chkRecreate.Text = '强制重建'
    $chkRecreate.AutoSize = $true
    $chkRecreate.Margin = New-Object System.Windows.Forms.Padding(16, 5, 0, 0)

    $chkNoClean = New-Object System.Windows.Forms.CheckBox
    $chkNoClean.Text = '不清理'
    $chkNoClean.AutoSize = $true
    $chkNoClean.Margin = New-Object System.Windows.Forms.Padding(8, 5, 0, 0)

    [void]$flowAct.Controls.Add($btnInit)
    [void]$flowAct.Controls.Add($btnDelete)
    [void]$flowAct.Controls.Add($btnRun)
    [void]$flowAct.Controls.Add($btnPackage)
    [void]$flowAct.Controls.Add($chkRecreate)
    [void]$flowAct.Controls.Add($chkNoClean)
    [void]$grpAction.Controls.Add($flowAct)
    [void]$main.Controls.Add($grpAction, 0, 2)

    # ─── Row 3 : spacer ──────────────────────────────────────

    # ─── Row 4 : Log ─────────────────────────────────────────
    $txtLog = New-Object System.Windows.Forms.TextBox
    $txtLog.Dock = 'Fill'
    $txtLog.Multiline = $true
    $txtLog.ScrollBars = 'Vertical'
    $txtLog.ReadOnly = $true
    $txtLog.Font = New-Object System.Drawing.Font('Consolas', 9)
    [void]$main.Controls.Add($txtLog, 0, 4)

    # ─── Helper script blocks ────────────────────────────────

    $updateStatus = {
        $vv = Get-VenvVersion
        if (Test-Path $Script:VenvPython) {
            $envText = ".venv 就绪 (Python $(Get-DisplayValue -Value $vv))"
        } else {
            $envText = '未创建'
        }
        $distText = if (Test-Path $Script:PackageOutput) { '已生成' } else { '未生成' }
        $lblStatus.Text = "环境: $envText  |  打包产物: $distText"
    }

    $getSelected = {
        $idx = $cboVersion.SelectedIndex
        if ($idx -ge 0 -and $idx -lt $ui.List.Count) { return $ui.List[$idx] }
        return $null
    }

    $rebuildCombo = {
        param($PreserveVersion)
        $cboVersion.Items.Clear()
        $selectIdx = 0
        for ($i = 0; $i -lt $ui.List.Count; $i++) {
            [void]$cboVersion.Items.Add($ui.List[$i].Display)
            if ($PreserveVersion -and $ui.List[$i].Version -eq $PreserveVersion) { $selectIdx = $i }
            elseif (-not $PreserveVersion -and $ui.List[$i].Version -eq $Script:RecommendedVersion) { $selectIdx = $i }
        }
        if ($cboVersion.Items.Count -gt 0) { $cboVersion.SelectedIndex = $selectIdx }
    }

    # ─── Events ───────────────────────────────────────────────

    $btnAdd.Add_Click({
        $raw = $txtCustom.Text.Trim()
        if ($raw -notmatch '^\d+\.\d+\.\d+$') {
            Show-UiError '请输入有效版本号，例如 3.14.1'
            return
        }
        foreach ($existing in $ui.List) {
            if ($existing.Version -eq $raw) {
                Show-UiError "版本 $raw 已在列表中。"
                return
            }
        }
        $newItem = @{ Version = $raw; Source = 'online'; Path = $null; Display = "$raw [在线]" }
        [void]$ui.List.Add($newItem)
        [void]$ui.Custom.Add($raw)
        & $rebuildCombo $raw
        $txtCustom.Text = ''
        Add-UiLog -TextBox $txtLog -Message "已添加在线版本 $raw。"
    })

    $btnRefresh.Add_Click({
        $prevVer = $null
        $sel = & $getSelected
        if ($sel) { $prevVer = $sel.Version }

        $newSys  = Find-SystemPythonVersions
        $newBase = Build-VersionList -SystemVersions $newSys

        $ui.List.Clear()
        foreach ($item in $newBase) { [void]$ui.List.Add($item) }

        # Preserve user-added online versions
        foreach ($cv in $ui.Custom) {
            $exists = $false
            foreach ($item in $ui.List) { if ($item.Version -eq $cv) { $exists = $true; break } }
            if (-not $exists) {
                [void]$ui.List.Add(@{ Version = $cv; Source = 'online'; Path = $null; Display = "$cv [在线]" })
            }
        }

        & $rebuildCombo $prevVer
        & $updateStatus
        Add-UiLog -TextBox $txtLog -Message '版本列表已刷新。'
    })

    $btnInit.Add_Click({
        $sel = & $getSelected
        if (-not $sel) { Show-UiError '请先选择 Python 版本。'; return }

        $shouldRecreate = $chkRecreate.Checked
        $currentVer = Get-VenvVersion
        if ((Test-Path $Script:VenvPython) -and (($currentVer -and $currentVer -ne $sel.Version) -or $shouldRecreate)) {
            $answer = [System.Windows.Forms.MessageBox]::Show(
                '将替换当前 .venv 环境，是否继续？', '确认',
                [System.Windows.Forms.MessageBoxButtons]::YesNo,
                [System.Windows.Forms.MessageBoxIcon]::Question)
            if ($answer -ne [System.Windows.Forms.DialogResult]::Yes) { return }
            $shouldRecreate = $true
        }

        Start-ManagedActionWindow -RequestedAction 'init-env' -Version $sel.Version -VersionSource $sel.Source -RecreateEnvironment:$shouldRecreate
        Add-UiLog -TextBox $txtLog -Message "已打开初始化控制台 (Python $($sel.Version) [$($sel.Source)])。"
    })

    $btnDelete.Add_Click({
        $answer = [System.Windows.Forms.MessageBox]::Show(
            '确定删除当前 .venv 环境？', '确认',
            [System.Windows.Forms.MessageBoxButtons]::YesNo,
            [System.Windows.Forms.MessageBoxIcon]::Question)
        if ($answer -ne [System.Windows.Forms.DialogResult]::Yes) { return }
        try {
            Remove-Environment
            & $updateStatus
            Add-UiLog -TextBox $txtLog -Message '环境已删除。'
        } catch {
            Show-UiError $_.Exception.Message
        }
    })

    $btnRun.Add_Click({
        try {
            Start-AppProcess
            Add-UiLog -TextBox $txtLog -Message '程序已启动。'
        } catch {
            Show-UiError $_.Exception.Message
        }
    })

    $btnPackage.Add_Click({
        $sel = & $getSelected
        if (-not $sel) { Show-UiError '请先选择 Python 版本。'; return }
        Start-ManagedActionWindow -RequestedAction 'package' -Version $sel.Version -VersionSource $sel.Source -DisableCleanBuild:$chkNoClean.Checked
        Add-UiLog -TextBox $txtLog -Message "已打开打包控制台 (Python $($sel.Version) [$($sel.Source)])。"
    })

    # ─── Show ─────────────────────────────────────────────────
    $form.Add_Shown({
        & $updateStatus
        Add-UiLog -TextBox $txtLog -Message '管理工具已就绪。'
    })

    [void]$form.Controls.Add($main)
    [void]$form.ShowDialog()
}

# ─── Entry point ──────────────────────────────────────────────────

try {
    Push-Location $Script:ProjectRoot

    switch ($Action) {
        'gui'        { Show-ManagementUi }
        'init-env'   { Initialize-Environment -Version $PythonVersion -Source $PythonSource -RecreateEnvironment:$Recreate -SkipPipUpgrade:$SkipUpgradePip -SkipRequirementInstall:$SkipDependencyInstall }
        'delete-env' { Remove-Environment }
        'run-app'    { Start-AppProcess }
        'package'    { Build-Package -Version $PythonVersion -Source $PythonSource -RecreateEnvironment:$Recreate -SkipPipUpgrade:$SkipUpgradePip -SkipRequirementInstall:$SkipDependencyInstall -DisableCleanBuild:$NoClean }
    }
}
catch {
    if ($Action -eq 'gui') {
        Add-Type -AssemblyName System.Windows.Forms
        [void][System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'OTool 管理工具', [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Error)
    } else {
        Write-Host "[error] $($_.Exception.Message)" -ForegroundColor Red
    }
    exit 1
}
finally {
    Pop-Location
}
