$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:LOCALAPPDATA + '\Microsoft\WinGet\Links'
}

function Install-IfMissing([string]$Command, [string]$Package) {
    if (Get-Command $Command -ErrorAction SilentlyContinue) { return }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw '系统没有 winget。请先从 Microsoft Store 安装或更新“应用安装程序”，然后重试。'
    }
    Write-Host ('正在安装依赖：' + $Package)
    & winget install --id $Package --exact --accept-source-agreements --accept-package-agreements --disable-interactivity
    Refresh-Path
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw ('仍未找到 ' + $Command + '。请关闭此窗口，重新双击启动；若仍失败，请把窗口末尾错误文字发来。')
    }
}

try {
    Write-Host 'Viral Lab / 视频采集与二剪工具'
    Write-Host '首次运行需要联网安装依赖。安装过程中如有 Windows 授权提示，请确认。'
    Refresh-Path
    Install-IfMissing 'uv' 'astral-sh.uv'
    Install-IfMissing 'ffmpeg' 'Gyan.FFmpeg'
    if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
        throw 'FFmpeg 安装未提供 ffprobe，请重新安装 Gyan.FFmpeg。'
    }
    if (-not $env:VIRALLAB_FONT) {
        foreach ($Name in @('msyh.ttc', 'msyh.ttf', 'simhei.ttf', 'simsun.ttc')) {
            $Font = Join-Path $env:WINDIR ('Fonts\' + $Name)
            if (Test-Path -LiteralPath $Font) {
                $env:VIRALLAB_FONT = $Font.Replace('\', '/')
                break
            }
        }
    }
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        & uv venv --python 3.12 .venv
        if ($LASTEXITCODE -ne 0) { throw 'Python 环境安装失败，请检查网络后重试。' }
    }
    & uv pip sync --python '.venv\Scripts\python.exe' requirements.txt
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查上方错误后重试。' }
    & '.\.venv\Scripts\python.exe' start_local.py
    if ($LASTEXITCODE -ne 0) { throw '工作台启动失败。' }
} catch {
    Write-Host ('错误：' + $_.Exception.Message) -ForegroundColor Red
    Write-Host '请保留错误文字，以便排查。'
    Read-Host '按回车关闭此窗口'
    exit 1
}
