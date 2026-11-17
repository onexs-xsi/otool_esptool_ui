param([string]$Payload)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$update = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Payload)) | ConvertFrom-Json
$directory = $update.directory
$target = $update.target
$asset = [IO.Path]::Combine($directory, 'otool_esptool_ui.exe')
$backup = "$target.otool-update.bak"
$commit = [IO.Path]::Combine($directory, 'commit')
$started = [IO.Path]::Combine($directory, 'started')
$mutex = [Threading.Mutex]::new($false, $update.mutex)
$locked = $false
$authorized = $false
$replaced = $false
$newApplication = $null

# Only this file transaction can run elevated. It never launches the app or
# writes status files into the user's temporary directory.
$replaceExecutable = {
    param($Update, [bool]$Restore)

    $target = $Update.target
    $backup = "$target.otool-update.bak"
    $transaction = "$target.$([IO.Path]::GetFileName($Update.directory))"
    $candidate = "$transaction.new"
    $displaced = $backup
    $expected = $Update.current
    $replacementVersion = $Update.version
    if ($Restore) {
        $candidate = $backup
        $displaced = "$transaction.failed"
        $expected = $Update.version
        $replacementVersion = $Update.current
    }

    $ownsCandidate = $false
    try {
        foreach ($path in @($target, $backup)) {
            if ([IO.File]::Exists($path) -and ([IO.File]::GetAttributes($path) -band [IO.FileAttributes]::ReparsePoint)) {
                return 10
            }
        }
        $current = [Diagnostics.FileVersionInfo]::GetVersionInfo($target)
        if ($current.FileVersion -ne $expected -or $current.OriginalFilename -ne 'otool_esptool_ui.exe') {
            return 10
        }
        if (-not $Restore) {
            $staged = [IO.Path]::Combine($Update.directory, 'otool_esptool_ui.exe')
            $inputFile = [IO.File]::OpenRead($staged)
            try {
                $output = [IO.File]::Open($candidate, 'CreateNew', 'Write', 'None')
                $ownsCandidate = $true
                try {
                    $inputFile.CopyTo($output)
                    $output.Flush($true)
                } finally { $output.Dispose() }
            } finally { $inputFile.Dispose() }
        }
        if (-not $Restore) {
            $verifiedFile = [IO.File]::OpenRead($candidate)
            $sha256 = [Security.Cryptography.SHA256]::Create()
            try { $actual = [BitConverter]::ToString($sha256.ComputeHash($verifiedFile)).Replace('-', '') }
            finally {
                $sha256.Dispose()
                $verifiedFile.Dispose()
            }
            if ($actual -ine $Update.digest) { return 11 }
        }
        $version = [Diagnostics.FileVersionInfo]::GetVersionInfo($candidate)
        if ($version.FileVersion -ne $replacementVersion -or $version.ProductVersion -ne $replacementVersion -or
            $version.OriginalFilename -ne 'otool_esptool_ui.exe') {
            return 11
        }

        # The bootloader or antivirus may hold the image briefly after exit.
        for ($attempt = 1; $attempt -le 10; $attempt++) {
            try {
                [IO.File]::Replace($candidate, $target, $displaced, $false)
                if ($Restore) { try { [IO.File]::Delete($displaced) } catch {} }
                return 0
            } catch {
                $code = $_.Exception.GetBaseException().HResult -band 0xffff
                if ($attempt -eq 10 -or $code -notin @(5, 32, 33)) { throw }
                [Threading.Thread]::Sleep(200)
            }
        }
    } catch {
        $code = $_.Exception.GetBaseException().HResult -band 0xffff
        # ReplaceFile can leave the original at the backup path on a partial failure.
        if ($code -eq 1177 -and -not [IO.File]::Exists($target) -and [IO.File]::Exists($displaced)) {
            try { [IO.File]::Move($displaced, $target) } catch {}
        }
        if ($code -eq 5) { return 5 }
        if ($code -in @(32, 33)) { return 12 }
        return 13
    } finally {
        if ($ownsCandidate) { try { [IO.File]::Delete($candidate) } catch {} }
    }
}

function Invoke-Replacement([bool]$Restore) {
    $code = & $replaceExecutable $update $Restore
    if ($code -eq 5) {
        # Encode fixed code and individual data fields to avoid shell quoting
        # ambiguity and loading a mutable script or profile under UAC.
        $fields = foreach ($key in @('directory', 'target', 'digest', 'current', 'version')) {
            $value = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($update.$key))
            "$key = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$value'))"
        }
        $restoreLiteral = if ($Restore) { '$true' } else { '$false' }
        $elevatedCommand = @"
`$ErrorActionPreference = 'Stop'
`$update = @{
$($fields -join "`n")
}
exit (& {
$replaceExecutable
} `$update $restoreLiteral)
"@
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($elevatedCommand))
        try {
            $elevated = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') `
                -ArgumentList @('-NoProfile', '-NonInteractive', '-EncodedCommand', $encoded) `
                -Verb RunAs -WindowStyle Hidden -PassThru
            $elevated.WaitForExit()
            $code = $elevated.ExitCode
            $elevated.Dispose()
        } catch {
            $cause = $_.Exception.GetBaseException()
            if ($cause -is [ComponentModel.Win32Exception] -and $cause.NativeErrorCode -eq 1223) {
                throw '已取消管理员权限申请。'
            }
            throw '无法取得安装目录的写入权限。'
        }
    }
    switch ($code) {
        0 { return }
        5 { throw 'Windows 未授予安装目录的写入权限。' }
        10 { throw '安装位置的程序已发生变化，请重新启动后再更新。' }
        11 { throw '安装包的校验值或内部版本不匹配。' }
        12 { throw '程序文件仍被占用，请关闭其他实例后重试。' }
        default { throw '无法替换程序文件，请检查安装目录和可用空间。' }
    }
}

function Start-Application([string]$Arguments) {
    $start = [Diagnostics.ProcessStartInfo]::new($target, $Arguments)
    $start.WorkingDirectory = [IO.Path]::GetDirectoryName($target)
    $start.UseShellExecute = $false
    return [Diagnostics.Process]::Start($start)
}

function Assert-NoOtherInstances {
    $name = [WildcardPattern]::Escape([IO.Path]::GetFileNameWithoutExtension($target))
    $other = Get-Process -Name $name -ErrorAction SilentlyContinue | Where-Object {
        $_.Id -notin @($update.guiPid, $update.bootPid) -and $_.Path -eq $target
    }
    if ($other) { throw '请关闭同一位置启动的其他窗口，再开始更新。' }
}

function Show-UpdateMessage([string]$Message) {
    Add-Type -AssemblyName System.Windows.Forms
    [void][Windows.Forms.MessageBox]::Show($Message, 'OTool Esptool UI 更新', 'OK', 'Warning')
}

try {
    try { $locked = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw '这个程序已有更新正在进行。' }
    $oldApplication = [Diagnostics.Process]::GetProcessById($update.guiPid)
    [void]$oldApplication.Handle
    $bootloader = Get-Process -Id $update.bootPid -ErrorAction SilentlyContinue
    if ($bootloader -and $bootloader.Path -ne $target) { $bootloader = $null }
    if ($bootloader) { [void]$bootloader.Handle }
    Assert-NoOtherInstances
    $staged = [Diagnostics.FileVersionInfo]::GetVersionInfo($asset)
    if ($staged.FileVersion -ne $update.version -or $staged.ProductVersion -ne $update.version -or
        $staged.OriginalFilename -ne 'otool_esptool_ui.exe') {
        throw '安装包的内部版本与 GitHub 发布记录不一致。'
    }
    [IO.File]::WriteAllText([IO.Path]::Combine($directory, 'ready'), '')
    while (-not [IO.File]::Exists($commit)) {
        if ($oldApplication.WaitForExit(150) -and -not [IO.File]::Exists($commit)) { return }
    }
    $authorized = $true
    if (-not $oldApplication.WaitForExit(15000) -or ($bootloader -and -not $bootloader.WaitForExit(20000))) {
        Show-UpdateMessage '原版本仍在退出。请确认所有窗口已关闭，再重试更新。'
        return
    }
    Assert-NoOtherInstances
    Invoke-Replacement $false
    $replaced = $true
    $newApplication = Start-Application "$($update.arguments) $($update.startupArgument)"
    $startup = [Diagnostics.Stopwatch]::StartNew()
    while (-not [IO.File]::Exists($started)) {
        if ($newApplication.WaitForExit(150)) { throw '新版未能完成启动。' }
        if ($startup.Elapsed.TotalSeconds -ge 45) {
            Show-UpdateMessage "新版进程已启动，主窗口尚未报告就绪。`n上一版备份路径`n$backup"
            return
        }
    }
} catch {
    $message = $_.Exception.Message
    if (-not $authorized) {
        [IO.File]::WriteAllText([IO.Path]::Combine($directory, 'error.txt'), $message)
        exit 1
    }
    try {
        if ($replaced) { Invoke-Replacement $true }
        $existing = [Diagnostics.FileVersionInfo]::GetVersionInfo($target)
        if ($existing.FileVersion -ne $update.current) { throw '原版本的路径已变化。' }
        [void](Start-Application $update.arguments)
        Show-UpdateMessage "$message`n已重新打开原版本。"
    } catch {
        $location = if ([IO.File]::Exists($backup)) { "`n上一版备份路径`n$backup" } else { '' }
        Show-UpdateMessage "$message`n无法自动重新打开原版本。$location`n程序路径`n$target"
    }
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
    if ($authorized -or [IO.File]::Exists([IO.Path]::Combine($directory, 'ready'))) {
        foreach ($name in @('otool_esptool_ui.exe', 'ready', 'commit', 'started', 'error.txt')) {
            try { [IO.File]::Delete([IO.Path]::Combine($directory, $name)) } catch {}
        }
        try { [IO.Directory]::Delete($directory) } catch {}
    }
}
