$ErrorActionPreference = "Stop"

$WeFlowExe = Join-Path $env:LOCALAPPDATA "Programs\WeFlow\WeFlow.exe"

function Get-WeFlowMainProcess {
    Get-CimInstance Win32_Process -Filter "Name = 'WeFlow.exe'" |
        Where-Object {
            $_.ExecutablePath -eq $WeFlowExe -and
            $_.CommandLine -notmatch "--type="
        } |
        Select-Object -First 1
}

while ($true) {
    if (-not (Test-Path -LiteralPath $WeFlowExe)) {
        Start-Sleep -Seconds 30
        continue
    }

    $mainProcess = Get-WeFlowMainProcess
    if (-not $mainProcess) {
        Start-Process -FilePath $WeFlowExe -WindowStyle Hidden | Out-Null
        Start-Sleep -Seconds 5
        $mainProcess = Get-WeFlowMainProcess
    }

    while (
        $mainProcess -and
        (Get-Process -Id $mainProcess.ProcessId -ErrorAction SilentlyContinue)
    ) {
        Start-Sleep -Seconds 5
    }

    Start-Sleep -Seconds 3
}
