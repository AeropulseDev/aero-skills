# loop-watch — установка на Windows: токен в переменную пользователя, задача планировщика, проверка.
# Запуск: двойной клик по setup.cmd (или powershell -ExecutionPolicy Bypass -File setup.ps1)
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $dir

# 1. Настоящий python.exe (не заглушка Microsoft Store) и pythonw.exe рядом с ним
$py = (& python -c "import sys; print(sys.executable)" 2>$null)
if (-not $py -or -not (Test-Path $py)) { throw "Не найден python. Установите Python 3.9+ и повторите." }
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }
Write-Host "Python: $py"

# 2. Токен GitHub -> переменная пользователя GITHUB_TOKEN (в config.yaml не остаётся)
$cur = [Environment]::GetEnvironmentVariable('GITHUB_TOKEN', 'User')
Write-Host ""
Write-Host ("Токен GitHub сейчас: " + $(if ($cur) { "задан в переменной GITHUB_TOKEN" } else { "в переменной не задан" }))
$sec = Read-Host -AsSecureString "Вставьте НОВЫЙ токен (Enter - оставить текущий / перенести из config.yaml)"
$new = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
if ($new) {
    [Environment]::SetEnvironmentVariable('GITHUB_TOKEN', $new.Trim(), 'User')
    Write-Host "Новый токен записан в GITHUB_TOKEN."
}
& $py watch.py move-token
# убрать старый токен из config.yaml, даже если задан новый
$env:GITHUB_TOKEN = [Environment]::GetEnvironmentVariable('GITHUB_TOKEN', 'User')

# 3. Задача планировщика: каждые 15 минут, пропущенный запуск догоняется после включения компьютера
$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$dir\watch.py`" run" -WorkingDirectory $dir
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'loop-watch' -Action $action -Trigger @($every, $logon) -Settings $set `
        -Description 'loop-watch: опрос GitHub без Claude, новое в watch.db + тост' -Force | Out-Null
Write-Host "Задача планировщика 'loop-watch' создана: каждые 15 минут и при входе в Windows."

# 4. Проверка: тост, первый опрос, дочитать пропущенное с 06.11
& $py watch.py notify-test
& $py watch.py run
& $py watch.py backfill 2026-11-06
& $py watch.py run
& $py watch.py status
Write-Host ""
Write-Host "Готово. Удалить задачу: Unregister-ScheduledTask loop-watch"
Read-Host "Enter - закрыть"
