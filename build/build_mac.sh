#!/usr/bin/env bash
# Builds dist/StitchPilot.app and dist/StitchPilot-mac-<arch>.zip. Run it on a Mac:
#   bash build/build_mac.sh
#
# The app is built for this Mac's processor (Apple Silicon or Intel) and is ad-hoc signed, so
# macOS asks once before opening it (right-click > Open). To ship it without that warning, set:
#   SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"   Developer ID signing
#   NOTARY_PROFILE="stitchpilot"   a notarytool keychain profile (xcrun notarytool store-credentials)
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
[[ "$(uname)" == "Darwin" ]] || { echo "build_mac.sh must run on macOS" >&2; exit 1; }

venv="$HOME/Library/Application Support/StitchPilot/venv"
work="$HOME/Library/Caches/StitchPilot/pyi"
if [[ ! -x "$venv/bin/python" ]]; then
    bash "$root/run.sh" --setup-only  # creates the Python environment, then stops
    [[ -x "$venv/bin/python" ]] || { echo "Couldn't set up Python (see run.sh)" >&2; exit 1; }
fi
py="$venv/bin/python"
"$py" -m pip install --quiet -r requirements.txt  # picks up packages added since the last build
"$py" -m pip install --quiet -r build/requirements-build.txt
"$py" build/make_icon.py

version="$("$py" -c 'import stitchpilot; print(stitchpilot.__version__)')"
arch="$(uname -m)"

# Qt modules the app never uses (keeps the app much smaller)
qt_skip=(QtWebEngineCore QtWebEngineWidgets QtWebEngineQuick QtWebChannel QtWebSockets QtQuick QtQuick3D
         QtQml QtQuickWidgets QtMultimedia QtMultimediaWidgets QtCharts QtDataVisualization QtGraphs Qt3DCore
         Qt3DRender Qt3DInput Qt3DExtras QtPdf QtPdfWidgets QtLocation QtPositioning QtSensors QtSerialPort
         QtBluetooth QtNfc QtRemoteObjects QtScxml QtSql QtTest QtTextToSpeech QtDesigner QtHelp QtSpatialAudio
         QtHttpServer QtNetworkAuth QtOpenGL QtOpenGLWidgets QtUiTools QtXml QtConcurrent QtDBus QtStateMachine)
excludes=(--exclude-module tkinter --exclude-module matplotlib --exclude-module pandas --exclude-module IPython
          --exclude-module pytest --exclude-module dask --exclude-module PIL.ImageQt)
for m in "${qt_skip[@]}"; do excludes+=(--exclude-module "PySide6.$m"); done

rm -rf dist/StitchPilot.app
# full paths: PyInstaller reads relative ones from the --specpath folder, not from here
"$py" -m PyInstaller --noconfirm --clean --windowed --name StitchPilot \
    --osx-bundle-identifier app.stitchpilot.StitchPilot \
    --icon "$root/stitchpilot/icon.png" \
    --add-data "$root/stitchpilot/icon.png:stitchpilot" \
    --collect-data pyembroidery \
    --collect-submodules skimage.morphology \
    --collect-submodules skimage.graph \
    --hidden-import pillow_heif \
    --workpath "$work/work" --specpath "$work" --distpath "$root/dist" \
    "${excludes[@]}" "$root/app.py"

app="dist/StitchPilot.app"
plist="$app/Contents/Info.plist"
pb=/usr/libexec/PlistBuddy
# version, and let Finder hand pictures / embroidery files to the app (drop on the Dock icon, Open With)
$pb -c "Set :CFBundleShortVersionString $version" "$plist" 2>/dev/null ||
    $pb -c "Add :CFBundleShortVersionString string $version" "$plist"
$pb -c "Add :NSHumanReadableCopyright string StitchPilot" "$plist" 2>/dev/null || true
$pb -c "Add :LSApplicationCategoryType string public.app-category.graphics-design" "$plist" 2>/dev/null || true
$pb -c "Delete :CFBundleDocumentTypes" "$plist" 2>/dev/null || true
$pb -c "Add :CFBundleDocumentTypes array" "$plist"
$pb -c "Add :CFBundleDocumentTypes:0 dict" "$plist"
$pb -c "Add :CFBundleDocumentTypes:0:CFBundleTypeName string Picture" "$plist"
$pb -c "Add :CFBundleDocumentTypes:0:CFBundleTypeRole string Viewer" "$plist"
$pb -c "Add :CFBundleDocumentTypes:0:LSHandlerRank string Alternate" "$plist"
$pb -c "Add :CFBundleDocumentTypes:0:LSItemContentTypes array" "$plist"
$pb -c "Add :CFBundleDocumentTypes:0:LSItemContentTypes:0 string public.image" "$plist"
$pb -c "Add :CFBundleDocumentTypes:0:LSItemContentTypes:1 string public.svg-image" "$plist"
$pb -c "Add :CFBundleDocumentTypes:1 dict" "$plist"
$pb -c "Add :CFBundleDocumentTypes:1:CFBundleTypeName string Embroidery file" "$plist"
$pb -c "Add :CFBundleDocumentTypes:1:CFBundleTypeRole string Editor" "$plist"
$pb -c "Add :CFBundleDocumentTypes:1:LSHandlerRank string Alternate" "$plist"
$pb -c "Add :CFBundleDocumentTypes:1:CFBundleTypeExtensions array" "$plist"
i=0
for ext in pes dst jef exp vp3 xxx pec u01 tbf; do
    $pb -c "Add :CFBundleDocumentTypes:1:CFBundleTypeExtensions:$i string $ext" "$plist"
    i=$((i + 1))
done

# sign (changing Info.plist invalidated PyInstaller's ad-hoc signature)
if [[ -n "${SIGN_IDENTITY:-}" ]]; then
    codesign --force --deep --options runtime --timestamp \
        --entitlements build/entitlements.plist --sign "$SIGN_IDENTITY" "$app"
else
    codesign --force --deep --sign - "$app"
fi
codesign --verify --deep --strict "$app"

# headless self-test of the packaged app: picture in, embroidery file out (a broken app isn't zipped)
if [[ -f tests/samples/badge.png ]]; then
    out="$work/selftest.pes"
    rm -f "$out" "$out.txt"
    if ! "$app/Contents/MacOS/StitchPilot" --selftest "$root/tests/samples/badge.png" "$out"; then
        cat "$out.txt" >&2 2>/dev/null || echo "self-test produced no report" >&2
        echo "The packaged app failed its self-test" >&2
        exit 1
    fi
    cat "$out.txt"
fi

zip="dist/StitchPilot-mac-$arch.zip"
rm -f "$zip"
ditto -c -k --keepParent "$app" "$zip"

if [[ -n "${SIGN_IDENTITY:-}" && -n "${NOTARY_PROFILE:-}" ]]; then
    xcrun notarytool submit "$zip" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$app"
    rm -f "$zip"
    ditto -c -k --keepParent "$app" "$zip"
fi

echo
echo "Built $app ($(du -sh "$app" | cut -f1)) and $zip"
