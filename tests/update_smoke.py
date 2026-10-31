"""Over-the-air updates, without the internet: a release made by the real build/publish_update.py is served
by a local update site, and the app's real update code checks it, downloads it and swaps it in (on stand-in
exe files). Covers the signature (tampering, a different key, rollbacks), the download checks, the swap and
its undo, and the update window in the app, including "keep working" when there are unsaved changes."""
import base64
import json
import shutil
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import _testenv  # noqa: F401  (offscreen Qt, throwaway settings)

here = Path(__file__).parent
root = here.parent
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from stitchpilot import __version__, gui, updates  # noqa: E402
import stitchpilot.dialogs as dialogs  # noqa: E402

tmp = _testenv.TMP
site = tmp / "site"
site.mkdir()


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(site)))
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"


def make_key(name: str) -> tuple[Path, str]:
    key = Ed25519PrivateKey.generate()
    path = tmp / name
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return path, base64.b64encode(pub).decode()


key, pub = make_key("signing.pem")
other_key, _ = make_key("someone-else.pem")
NEW = b"MZ new StitchPilot " * 50000  # ~1 MB stand-in for the new exe
(tmp / "new.exe").write_bytes(NEW)


def publish(version: str, signing_key: Path = key, notes: str = "Faster stitching"):
    out = tmp / f"rel-{version}"
    subprocess.run([sys.executable, str(root / "build" / "publish_update.py"), "release", "--notes", notes,
                    "--version", version, "--exe", str(tmp / "new.exe"), "--key", str(signing_key), "--out", str(out)],
                   check=True, capture_output=True)
    for f in out.iterdir():
        shutil.copy2(f, site / f.name)
    return json.loads((out / "latest.json").read_text(encoding="utf-8"))


def expect_error(fn, *words):
    try:
        fn()
    except updates.UpdateError as e:
        assert all(w in str(e) for w in words), e
        return str(e)
    raise AssertionError(f"expected an UpdateError mentioning {words}")


# ---- versions
assert updates.is_newer("0.1.1", "0.1.0") and updates.is_newer("0.10.0", "0.9.9") and updates.is_newer("1", "0.9")
assert not updates.is_newer("0.1.0", "0.1") and not updates.is_newer("0.0.9", "0.1.0")

# ---- not set up yet: updates are off, quietly
updates.UPDATE_URL, updates.PUBLIC_KEY = "", ""
assert not updates.configured()
expect_error(updates.check, "aren't set up")
updates.UPDATE_URL, updates.PUBLIC_KEY = f"{base}/latest.json", pub

# ---- a newer signed release is offered, with its download checked against what was signed
updates.platform_key = lambda: "windows"  # these releases only have a Windows exe (the tests run on Macs too)
nv = "99.0.0"
doc = publish(nv)
rel = updates.check()
assert rel and rel.version == nv and rel.notes == "Faster stitching", rel
assert rel.url == f"{base}/StitchPilot-{nv}.exe" and rel.size == len(NEW), rel
assert updates.check(current=nv) is None, "already up to date"
print("offered:", rel.version, rel.url, rel.size, "bytes")

# ---- what isn't trusted
good = (site / "latest.json").read_bytes()
tampered = dict(doc, payload=doc["payload"].replace(nv, "99.0.1"))
(site / "latest.json").write_text(json.dumps(tampered))
print("tampered:", expect_error(updates.check, "isn't signed"))
publish("99.1.0", signing_key=other_key)
expect_error(updates.check, "isn't signed")  # signed, but not with StitchPilot's key
(site / "latest.json").write_text(json.dumps({"payload": "{}", "signature": "not base64!"}))
expect_error(updates.check, "isn't signed")
(site / "latest.json").write_bytes(b" " * (updates.MANIFEST_LIMIT + 10))
expect_error(updates.check, "too large")
publish("0.0.1")  # an old (genuinely signed) release: never a downgrade
assert updates.check() is None, "rollback refused"
saved_url = updates.UPDATE_URL
updates.UPDATE_URL = "http://127.0.0.1:9/latest.json"
expect_error(updates.check, "Couldn't reach")
updates.UPDATE_URL = f"{base}/no-release-yet/latest.json"  # before the first signed release: nothing to do
assert updates.check() is None
updates.UPDATE_URL = saved_url
(site / "latest.json").write_bytes(good)
assert updates.check().version == nv

# ---- downloads: checked as they arrive; a bad one leaves nothing behind
app_dir = tmp / "app"
app_dir.mkdir()
exe = app_dir / "StitchPilot.exe"
exe.write_bytes(b"MZ old StitchPilot")
stage = updates.staging_path(exe)
assert stage.name == "StitchPilot.exe.new"
seen = []
assert updates.download(rel, stage, lambda d, t: seen.append(d)) == stage
assert stage.read_bytes() == NEW and seen[-1] == len(NEW) and len(seen) >= 1
stage.unlink()
real = (site / f"StitchPilot-{nv}.exe").read_bytes()
(site / f"StitchPilot-{nv}.exe").write_bytes(real[:-1] + b"X")  # swapped on the server
print("corrupted:", expect_error(lambda: updates.download(rel, stage), "didn't match"))
(site / f"StitchPilot-{nv}.exe").write_bytes(real + b"extra")
expect_error(lambda: updates.download(rel, stage), "larger than")
(site / f"StitchPilot-{nv}.exe").write_bytes(real)
expect_error(lambda: updates.download(rel, stage, cancelled=lambda: True), "cancelled")
assert not stage.exists() and not list(app_dir.glob("*.part")), list(app_dir.iterdir())

# ---- the swap, and putting things back if it can't finish
updates.download(rel, stage)
assert updates.install(stage, exe) == exe
assert exe.read_bytes() == NEW and (app_dir / "StitchPilot.exe.old").read_bytes() == b"MZ old StitchPilot"
assert updates.cleanup(exe) and sorted(p.name for p in app_dir.iterdir()) == ["StitchPilot.exe"]
expect_error(lambda: updates.install(app_dir / "missing.exe", exe), "nothing was changed")
assert exe.read_bytes() == NEW and not (app_dir / "StitchPilot.exe.old").exists(), "undone"

# ---- in the app: Help ▸ Check for updates, Update and restart
exe.write_bytes(b"MZ old StitchPilot")
relaunched, boxes = [], []
updates.can_install = lambda: True
updates.staging_path = lambda e=None, _real=updates.staging_path: _real(exe)
updates.install = lambda new, e=None, _real=updates.install: _real(new, exe)
updates.relaunch = lambda e=None: relaunched.append(e)
updates.cleanup = lambda e=None, _real=updates.cleanup: _real(exe)
gui.QMessageBox.information = staticmethod(lambda _p, _t, text, *a, **k: boxes.append(text))
gui.QMessageBox.warning = staticmethod(lambda _p, _t, text, *a, **k: boxes.append("WARNING " + text))
app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)
gui.apply_light_theme(app)


def wait_for(cond, timeout=20):
    loop = QEventLoop()
    t = QTimer()
    t.timeout.connect(lambda: cond() and loop.quit())
    t.start(30)
    QTimer.singleShot(timeout * 1000, loop.quit)
    loop.exec()


windows = []
answer = {"click": "go"}


def run_update(dlg):
    windows.append(dlg)
    assert nv in dlg.text.text() and __version__ in dlg.text.text()
    assert dlg.go.text() == "Update and restart"
    if answer["click"] == "skip":
        dlg.skip.click()
        return 0
    dlg.show()
    dlg.go.click()
    wait_for(lambda: not dlg.isVisible())
    return 1


dialogs.UpdateDialog.exec = run_update
w = gui.MainWindow()
w.show()
assert w.auto_update.isChecked(), "on by default"

# the automatic check: "Skip this version" is remembered; nothing more is shown for it
answer["click"] = "skip"
w.check_updates(quiet=True)
wait_for(lambda: windows)
assert w.qs.value("update_skip") == nv and len(windows) == 1
w.qs.setValue("update_checked_at", 0)
w.check_updates(quiet=True)
wait_for(lambda: False, timeout=1)
assert len(windows) == 1, "a skipped version isn't offered again automatically"
w.check_updates(quiet=True)  # and it checks at most once a day
wait_for(lambda: False, timeout=1)
assert len(windows) == 1 and not boxes, boxes

# unsaved changes and "keep working": installed, starts next time
answer["click"] = "go"
gui.MainWindow.maybe_save = lambda self: False
w.check_updates()  # asked for by hand: offered even though it was skipped
wait_for(lambda: boxes)
assert exe.read_bytes() == NEW and w.isVisible() and not relaunched, (boxes, relaunched)
assert "next time" in boxes[-1], boxes
print("kept working:", boxes[-1])

# nothing newer: says so when asked by hand
publish(__version__)
w.check_updates()
wait_for(lambda: len(boxes) == 2)
assert "latest version" in boxes[-1], boxes
(site / "latest.json").write_bytes(good)

# and the usual way: swap, close, start the new copy
updates.cleanup(exe)
exe.write_bytes(b"MZ old StitchPilot")
gui.MainWindow.maybe_save = lambda self: True
w.check_updates()
wait_for(lambda: relaunched)
assert relaunched == [exe] and exe.read_bytes() == NEW and not w.isVisible(), relaunched
assert (app_dir / "StitchPilot.exe.old").exists()
w2 = gui.MainWindow()
w2._cleanup_update()  # the new copy tidies up after the old one
print("updated and restarted:", relaunched[0].name, "| left in the folder:", sorted(p.name for p in app_dir.iterdir()))

# ---- a computer this release has no file for: point at the download page
updates.platform_key = lambda: "mac-arm64"
r = updates.check()
assert r.url == "" and r.page == "https://github.com/jessiepullaro414/StitchPilot/releases/latest", r

# ---- the GitHub route: the draft release's files are downloaded and their build attestations checked (gh,
# stood in for here), latest.json is signed for them and attached; it points at that release's own files
sys.path.insert(0, str(root / "build"))
import publish_update as pu  # noqa: E402

draft = tmp / "draft"
draft.mkdir()
(draft / "StitchPilot-99.1.0.exe").write_bytes(NEW)
(draft / "StitchPilot-99.1.0-mac-arm64.zip").write_bytes(b"PK mac StitchPilot")
calls = []


def fake_gh(*args, capture=False):
    calls.append(args)
    if args[:2] == ("release", "download"):
        for f in draft.iterdir():
            shutil.copy2(f, Path(args[args.index("-D") + 1]) / f.name)
    return ""


pu.gh = fake_gh
sys.argv = ["publish_update.py", "release", "--github", "v99.1.0", "--notes", "From GitHub", "--key", str(key),
            "--out", str(tmp / "gh-release")]
pu.main()
steps = [c[:2] for c in calls]
assert steps == [("release", "download"), ("attestation", "verify"), ("attestation", "verify"),
                 ("release", "upload")], steps
assert "--draft=false" not in sum(calls, ()), "publishing stays the user's click"
shutil.copy2(tmp / "gh-release" / "latest.json", site / "latest.json")
gh_base = "https://github.com/jessiepullaro414/StitchPilot/releases/download/v99.1.0/"
r = updates.check()
assert r.version == "99.1.0" and r.url == gh_base + "StitchPilot-99.1.0-mac-arm64.zip" and r.size == 18, r
updates.platform_key = lambda: "windows"
r = updates.check()
assert r.url == gh_base + "StitchPilot-99.1.0.exe" and r.size == len(NEW) and r.notes == "From GitHub", r
print("GitHub release signed:", r.url)
srv.shutdown()
print("update smoke OK")
