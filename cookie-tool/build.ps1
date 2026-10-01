$ErrorActionPreference = 'Stop'

$toolRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $toolRoot '..')
$venvPython = Join-Path $toolRoot '.venv\Scripts\python.exe'
$outputDir = Join-Path $repoRoot 'downloads'

if (-not (Test-Path -LiteralPath $venvPython)) {
    py -3 -m venv (Join-Path $toolRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the build environment.' }
}

& $venvPython -m pip install --disable-pip-version-check --upgrade pyinstaller playwright
if ($LASTEXITCODE -ne 0) { throw 'Could not install the executable build dependencies.' }

New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
& $venvPython -m PyInstaller --noconfirm --clean --onefile --console `
    --name Get-Douyin-Cookies --collect-all playwright `
    --distpath $outputDir --workpath (Join-Path $toolRoot 'build') --specpath $toolRoot `
    (Join-Path $toolRoot 'Get-Douyin-Cookies.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

Write-Output (Join-Path $outputDir 'Get-Douyin-Cookies.exe')
