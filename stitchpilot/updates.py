"""Over-the-air updates for the desktop app.

- Each release publishes the new app files and a small `latest.json` (build/publish_update.py) as a GitHub
  release. UPDATE_URL is GitHub's address for the newest published release's latest.json, and the entries in
  it point at that release's own (versioned, unchanging) files. deploy/UPDATES.md.
- `latest.json` is signed with StitchPilot's private Ed25519 key, which never leaves the publisher's PC.
  The app only trusts it if the signature matches PUBLIC_KEY, and only takes a newer version, so whoever
  can change the hosted files still can't push a program to people's computers or roll them back.
- The signed information includes each file's SHA-256, so the download is checked before it's used.
- On Windows the portable exe replaces itself: a running exe can be renamed but not overwritten, so the
  new file is downloaded next to it, the old one is renamed to `.old`, and the new one takes its name.
  The next start removes the `.old` file. Elsewhere (Mac, running from source) the app offers the download.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__

# The address of latest.json (drafts and pre-releases never count as "latest"), and the public half of the
# signing key (build/publish_update.py keygen, deploy/UPDATES.md). Until the key is filled in, updates are off.
UPDATE_URL = os.environ.get("STITCHPILOT_UPDATE_URL",
                            "https://github.com/jessiepullaro414/StitchPilot/releases/latest/download/latest.json")
PUBLIC_KEY = os.environ.get("STITCHPILOT_UPDATE_KEY", "")

FROZEN = getattr(sys, "frozen", False)
MANIFEST_LIMIT = 64 * 1024


def platform_key() -> str:
    """Which entry of latest.json is for this computer: "windows", "mac-arm64", "mac-x86_64"…"""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return f"mac-{platform.machine()}"
    return sys.platform


class UpdateError(Exception):
    """Shown to people as is."""


@dataclass
class Release:
    version: str
    notes: str
    url: str        # the file for this computer
    sha256: str
    size: int
    page: str       # where to download it by hand


def configured() -> bool:
    return bool(UPDATE_URL and PUBLIC_KEY)


def parse_version(v: str) -> tuple[int, ...]:
    parts = str(v).strip().removeprefix("v").split(".")
    if not 1 <= len(parts) <= 4 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not a version: {v!r}")
    return tuple(int(p) for p in parts) + (0,) * (4 - len(parts))


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def verify(doc: dict, public_key: str = "") -> dict:
    """The signed release information in latest.json, or UpdateError if the signature doesn't match."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:  # running from source with an older setup
        raise UpdateError("Updates can't be checked: run  pip install -r requirements.txt  first.") from None

    try:
        payload, sig = doc["payload"], base64.b64decode(doc["signature"], validate=True)
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key or PUBLIC_KEY, validate=True))
        key.verify(sig, payload.encode())
        info = json.loads(payload)
    except (InvalidSignature, KeyError, TypeError, ValueError):
        raise UpdateError("The update information isn't signed by StitchPilot, so it was ignored.") from None
    if not isinstance(info, dict) or info.get("app") != "StitchPilot":
        raise UpdateError("The update information is for a different app, so it was ignored.")
    return info


def _get(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"user-agent": f"StitchPilot/{__version__}", "cache-control": "no-cache"})
    return urllib.request.urlopen(req, timeout=timeout)


def check(current: str = __version__) -> Release | None:
    """The newer release for this computer, or None if this is the latest."""
    if not configured():
        raise UpdateError("Updates aren't set up in this version of StitchPilot.")
    try:
        with _get(UPDATE_URL, 20) as r:
            raw = r.read(MANIFEST_LIMIT + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # no signed release published yet
            return None
        raise UpdateError("Couldn't reach the StitchPilot update site. Try again later.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise UpdateError("Couldn't reach the StitchPilot update site. Check your internet connection.") from None
    if len(raw) > MANIFEST_LIMIT:
        raise UpdateError("The update information is too large, so it was ignored.")
    try:
        doc = json.loads(raw)
    except ValueError:
        raise UpdateError("The update information couldn't be read.") from None
    info = verify(doc if isinstance(doc, dict) else {})
    try:
        if not is_newer(info["version"], current):
            return None
        entry = info.get(platform_key())
        page = urllib.parse.urljoin(UPDATE_URL, info.get("page") or "")
        if not isinstance(entry, dict):  # nothing for this computer in this release: point at the page
            return Release(info["version"], info.get("notes", ""), "", "", 0, page)
        return Release(info["version"], str(info.get("notes", "")), urllib.parse.urljoin(UPDATE_URL, entry["url"]),
                       str(entry["sha256"]).lower(), int(entry["size"]), page)
    except (KeyError, TypeError, ValueError):
        raise UpdateError("The update information couldn't be read.") from None


def can_install() -> bool:
    """Whether this copy can replace itself (the portable Windows exe)."""
    return FROZEN and sys.platform == "win32"


def staging_path(exe: Path | None = None) -> Path:
    """Downloads go next to the exe, so the swap is a rename on the same drive."""
    exe = exe or Path(sys.executable)
    return exe.with_name(exe.name + ".new")


def download(rel: Release, dest: Path, progress=None, cancelled=lambda: False) -> Path:
    """Downloads rel to dest, checking its size and SHA-256 as it goes. progress(done_bytes, total_bytes)."""
    if not rel.url:
        raise UpdateError("This update has no download for this computer.")
    part = dest.with_name(dest.name + ".part")
    h, done = hashlib.sha256(), 0
    try:
        with _get(rel.url, 60) as r, open(part, "wb") as f:
            while chunk := r.read(1 << 20):
                done += len(chunk)
                if done > rel.size:
                    raise UpdateError("The download is larger than it should be, so it was thrown away.")
                h.update(chunk)
                f.write(chunk)
                if progress:
                    progress(done, rel.size)
                if cancelled():
                    raise UpdateError("Update cancelled.")
        if done != rel.size or h.hexdigest() != rel.sha256:
            raise UpdateError("The download didn't match what StitchPilot published, so it was thrown away. "
                              "Try again later.")
        os.replace(part, dest)
        return dest
    except PermissionError:
        raise UpdateError(f"StitchPilot can't write to its folder ({dest.parent}). Move StitchPilot.exe to a "
                          "folder you can change, such as Documents, or download the update by hand.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise UpdateError("The download stopped. Check your internet connection and try again.") from None
    finally:
        part.unlink(missing_ok=True)


def install(new_file: Path, exe: Path | None = None) -> Path:
    """Puts new_file in the running exe's place; the running copy carries on as `<exe>.old`."""
    exe = exe or Path(sys.executable)
    old = exe.with_name(exe.name + ".old")
    try:
        old.unlink(missing_ok=True)
        os.replace(exe, old)
    except OSError:
        raise UpdateError("Couldn't replace StitchPilot.exe. Close any other StitchPilot windows and try again.") from None
    try:
        os.replace(new_file, exe)
    except OSError:
        os.replace(old, exe)  # put things back as they were
        raise UpdateError("Couldn't put the new version in place, so nothing was changed.") from None
    return exe


def relaunch(exe: Path | None = None):
    """Starts the (new) exe as a separate program."""
    exe = exe or Path(sys.executable)
    # A one-file exe started from another one would otherwise reuse the old copy's unpacked files.
    env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(exe)], env=env, cwd=str(exe.parent), creationflags=flags, close_fds=True)


def cleanup(exe: Path | None = None) -> bool:
    """Removes what an earlier update left behind. False while the old copy is still closing."""
    if not can_install() and exe is None:
        return True
    exe = exe or Path(sys.executable)
    ok = True
    new = staging_path(exe)
    for leftover in (exe.with_name(exe.name + ".old"), new, new.with_name(new.name + ".part")):
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            ok = False
    return ok
