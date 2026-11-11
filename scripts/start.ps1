param(
    [string]$Install = 'D:\Games\World of Warcraft',
    [string]$Listen = '127.0.0.1',
    [int]$Port = 8765
)
Set-Location -LiteralPath (Split-Path $PSScriptRoot -Parent)
python -m wowsync --install $Install --listen $Listen --port $Port --open
