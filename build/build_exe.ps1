# Builds dist\StitchPilot.exe: a single portable file, no install needed.
# PyInstaller's work folder lives in a short path because Windows can't load some DLLs from deep folders.
$ErrorActionPreference = "Continue"  # PyInstaller logs to stderr; failures are caught via exit code
$root = Split-Path $PSScriptRoot -Parent
$venv = Join-Path $env:LOCALAPPDATA "StitchPilot\venv"
$py = Join-Path $venv "Scripts\python.exe"
$work = Join-Path $env:LOCALAPPDATA "StitchPilot\pyi"

if (-not (Test-Path $py)) {
    python -m venv $venv
    & $py -m pip install --upgrade pip
}
& $py -m pip install --quiet -r (Join-Path $root "requirements.txt")  # picks up packages added since the last build
& $py -m pip install --quiet -r (Join-Path $PSScriptRoot "requirements-build.txt")
& $py (Join-Path $PSScriptRoot "make_icon.py")

# Qt modules the app never uses (keeps the exe much smaller)
$qtSkip = "QtWebEngineCore QtWebEngineWidgets QtWebEngineQuick QtWebChannel QtWebSockets QtQuick QtQuick3D " +
          "QtQml QtQuickWidgets QtMultimedia QtMultimediaWidgets QtCharts QtDataVisualization QtGraphs Qt3DCore " +
          "Qt3DRender Qt3DInput Qt3DExtras QtPdf QtPdfWidgets QtLocation QtPositioning QtSensors QtSerialPort " +
          "QtBluetooth QtNfc QtRemoteObjects QtScxml QtSql QtTest QtTextToSpeech QtDesigner QtHelp QtSpatialAudio " +
          "QtHttpServer QtNetworkAuth QtOpenGL QtOpenGLWidgets QtUiTools QtXml QtConcurrent QtDBus QtStateMachine"
$excludes = @("tkinter", "matplotlib", "pandas", "IPython", "pytest", "dask", "PIL.ImageQt") +
            ($qtSkip.Split(" ") | ForEach-Object { "PySide6.$_" })
$exArgs = $excludes | ForEach-Object { "--exclude-module", $_ }

& $py -m PyInstaller --noconfirm --clean --onefile --windowed --name StitchPilot `
    --icon (Join-Path $PSScriptRoot "icon.ico") `
    --add-data "$(Join-Path $root 'stitchpilot\icon.png');stitchpilot" `
    --collect-data pyembroidery `
    --collect-submodules skimage.morphology `
    --collect-submodules skimage.graph `
    --hidden-import pillow_heif `
    --workpath (Join-Path $work "work") --specpath $work `
    --distpath (Join-Path $root "dist") `
    @exArgs (Join-Path $root "app.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Get-Item (Join-Path $root "dist\StitchPilot.exe") | Select-Object Name, @{n = "MB"; e = { [math]::Round($_.Length / 1MB) } }
