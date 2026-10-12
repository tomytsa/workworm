$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
New-Item -ItemType Directory -Path '.\data' -Force | Out-Null
& '.\.venv\Scripts\python.exe' update.py
