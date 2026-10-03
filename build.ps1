# Rebuilds installer\Forge-Setup-<version>.exe  (needs: pip install pyinstaller pillow, and Inno Setup 6)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$version = (Select-String -Path src\forge\version.py -Pattern '"([\d.]+)"').Matches[0].Groups[1].Value
Write-Host "Building Forge $version"
$ErrorActionPreference = "Continue"
python -m PyInstaller --noconfirm --clean forge.spec 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
$ErrorActionPreference = "Stop"
$out = Join-Path $env:TEMP "forge-out"   # built outside OneDrive, which can lock the file
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" "/O$out" "/DAppVersion=$version" forge.iss
New-Item -ItemType Directory -Force installer | Out-Null
Copy-Item "$out\*.exe" installer\ -Force


