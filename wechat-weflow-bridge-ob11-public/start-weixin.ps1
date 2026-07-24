$ErrorActionPreference = "Stop"

$env:QT_ANGLE_PLATFORM = "software"
$env:QT_OPENGL = "software"
$env:QT_QUICK_BACKEND = "software"
$env:QSG_RHI_BACKEND = "software"
$env:QT_ENABLE_HIGHDPI_SCALING = "0"
$env:QT_AUTO_SCREEN_SCALE_FACTOR = "0"
$env:QT_SCALE_FACTOR = "1"
$env:QT_FONT_DPI = "96"

$weixinPath = "C:\Program Files\Tencent\Weixin\Weixin.exe"
Start-Process -FilePath $weixinPath -WorkingDirectory (Split-Path $weixinPath)

$pythonPath = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
$loginScript = Join-Path $PSScriptRoot "login-weixin.py"
& $pythonPath $loginScript
