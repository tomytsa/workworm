$ErrorActionPreference = 'Stop'
$hours = [double]((Get-Content (Join-Path $PSScriptRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json).autopilot.check_every_hours)
if (-not $hours) { $hours = 4 }
$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

# Agent: starts 2 minutes after logon and repeats every N hours (autopilot.check_every_hours) while the session is open.
# pythonw + launcher.pyw: no console window at all (Windows Terminal ignores -WindowStyle Hidden).
$pythonw = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$launcher = "`"$(Join-Path $PSScriptRoot 'launcher.pyw')`""
$agent = New-ScheduledTaskAction -Execute $pythonw -Argument "$launcher agent" -WorkingDirectory $PSScriptRoot
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$atLogon.Delay = 'PT2M'
$atLogon.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours $hours)).Repetition
$daily = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours(9) -RepetitionInterval (New-TimeSpan -Hours $hours)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 12) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'WorkWorm' -Action $agent -Trigger @($atLogon, $daily) -Principal $principal -Settings $settings -Description "Ciclo del agente de WorkWorm: al iniciar sesión y cada $hours horas." -Force | Out-Null

# Dashboard: http://127.0.0.1:8501 at logon, kept running.
$board = New-ScheduledTaskAction -Execute $pythonw -Argument "$launcher board" -WorkingDirectory $PSScriptRoot
# Delay + retries: at logon the D: drive may not be ready yet (task failed with 0x80070002).
$boardSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
$boardLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$boardLogon.Delay = 'PT1M'
Register-ScheduledTask -TaskName 'WorkWorm - Tablero' -Action $board -Trigger $boardLogon -Principal $principal -Settings $boardSettings -Description 'Abre el tablero de WorkWorm en http://127.0.0.1:8501 al iniciar sesión.' -Force | Out-Null

Write-Output "Tareas creadas: agente (al iniciar sesión y cada $hours h) y tablero (al iniciar sesión)."
