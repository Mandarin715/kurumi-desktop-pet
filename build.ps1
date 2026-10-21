$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv venv .venv
    } else {
        python -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create virtual environment.' }
}
# uv also works with Python distributions that omit ensurepip.
if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv pip install --python $python -r requirements.txt
} else {
    & $python -m pip install -r requirements.txt
}
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $python prepare_assets.py
if ($LASTEXITCODE -ne 0) { throw 'Asset preparation failed.' }
& $python -m PyInstaller --noconfirm Kurumi.spec
if ($LASTEXITCODE -ne 0) { throw 'Packaging failed.' }
$out = Join-Path $PSScriptRoot 'dist'
New-Item -ItemType Directory -Force -Path (Join-Path $out 'assets') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $out 'assets\sfx') | Out-Null
foreach ($clip in @('click_1', 'good_1', 'good_2', 'bad_1', 'bad_2', 'bad_3')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "assets\sfx\$clip.mp3") -Destination (Join-Path $out 'assets\sfx') -Force
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'LICENSES.txt') -Destination $out -Force
# Preserve settings edited next to an existing release.
if (-not (Test-Path -LiteralPath (Join-Path $out 'kurumi.json'))) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'kurumi.json') -Destination $out
}
Copy-Item -Path (Join-Path $PSScriptRoot 'assets\kurumi_*.png') -Destination (Join-Path $out 'assets') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot '使用说明.txt') -Destination $out -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination $out -Force
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'docs')) {
    Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot 'docs') -Filter '*.docx' -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $out -Force
    }
}
Write-Host 'Built: dist\Kurumi.exe'
