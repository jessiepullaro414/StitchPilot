"""Publishes a StitchPilot release so existing copies update themselves (see deploy/UPDATES.md).

    python build/publish_update.py keygen
        Makes the update signing key, once. The private key is saved outside the project; the public key
        it prints goes into PUBLIC_KEY in stitchpilot/updates.py (before building the first exe that ships).

    python build/publish_update.py release --github v0.2.0 --notes "What's new"
        The usual way. Downloads the draft GitHub release that .github/workflows/release.yml built for the tag,
        checks each file's build attestation (it was built by this repo's workflow), signs a latest.json for
        those files and attaches it to the draft. Publishing the draft is left to you: until then nothing
        changes for anyone. Needs the GitHub CLI (gh), signed in.

    python build/publish_update.py release --notes "What's new" [--mac dist/StitchPilot-mac-arm64.zip] [--upload s3://bucket]
        Without GitHub: signs a latest.json for the version in stitchpilot/__init__.py, using dist/StitchPilot.exe
        (built with build/build_exe.ps1). Writes everything to release/<version>/; --upload copies it to an S3
        bucket (deploy/aws/updates.yaml) with the AWS CLI, the app files first and latest.json last.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KEY = Path.home() / ".stitchpilot" / "update_signing_key.pem"
REPO = "jessiepullaro414/StitchPilot"
PAGE = f"https://github.com/{REPO}/releases/latest"


def version() -> str:
    text = (ROOT / "stitchpilot" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def keygen(path: Path):
    if path.exists():
        sys.exit(f"{path} already exists. Keep using it: a new key would stop existing copies from updating.")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    if sys.platform != "win32":
        os.chmod(path, 0o600)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    print(f"Private key saved to {path}")
    print("Back it up (a password manager is a good place). Without it, existing copies can't be updated.")
    print("\nPut this in PUBLIC_KEY in stitchpilot/updates.py:\n")
    print(base64.b64encode(pub).decode())


def load_key(path: Path) -> Ed25519PrivateKey:
    if not path.exists():
        sys.exit(f"No signing key at {path}. Run:  python build/publish_update.py keygen")
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        sys.exit(f"{path} isn't an Ed25519 key")
    return key


def entry(file: Path, name: str) -> dict:
    h = hashlib.sha256()
    with open(file, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return {"url": name, "sha256": h.hexdigest(), "size": file.stat().st_size}


def sign(info: dict, key: Ed25519PrivateKey) -> dict:
    payload = json.dumps(info, indent=1, ensure_ascii=False)
    return {"payload": payload, "signature": base64.b64encode(key.sign(payload.encode())).decode()}


def mac_arch(file: Path) -> str:
    return "arm64" if "arm64" in file.name else "x86_64" if "x86_64" in file.name else platform.machine()


def manifest(ver: str, notes: str, page: str, exe: Path, macs: list[Path], key: Ed25519PrivateKey,
             base: str = "") -> dict:
    """The signed latest.json for these files. base goes in front of each file name in its download address:
    empty for files next to latest.json, or a release's own download folder."""
    info = {"app": "StitchPilot", "version": ver, "released": date.today().isoformat(), "notes": notes,
            "page": page, "windows": entry(exe, base + exe.name)}
    for mac in macs:
        info[f"mac-{mac_arch(mac)}"] = entry(mac, base + mac.name)
    return sign(info, key)


def gh(*args: str, capture: bool = False) -> str:
    """Runs the GitHub CLI (stops with its message if it fails)."""
    try:
        r = subprocess.run(["gh", *args], check=True, text=True, capture_output=capture)
    except FileNotFoundError:
        sys.exit("The GitHub CLI isn't installed: https://cli.github.com (then: gh auth login)")
    except subprocess.CalledProcessError as e:
        sys.exit(f"gh {' '.join(args[:2])} failed" + (f": {e.stderr.strip()}" if e.stderr else ""))
    return r.stdout if capture else ""


def from_github(args):
    tag = args.github
    ver = args.version or tag.removeprefix("v")
    key = load_key(Path(args.key))  # before downloading anything: no point without the key
    out = Path(args.out) if args.out else ROOT / "release" / ver
    out.mkdir(parents=True, exist_ok=True)
    gh("release", "download", tag, "-R", REPO, "-D", str(out), "--pattern", f"StitchPilot-{ver}*", "--clobber")
    exe = out / f"StitchPilot-{ver}.exe"
    macs = sorted(out.glob(f"StitchPilot-{ver}-mac-*.zip"))
    if not exe.exists():
        sys.exit(f"The {tag} release has no StitchPilot-{ver}.exe. Did the release workflow finish?")
    for f in [exe, *macs]:  # built by this repo's release workflow, from this repo's code
        gh("attestation", "verify", str(f), "-R", REPO, capture=True)
        print(f"  {f.name}: built by {REPO}'s release workflow")
    doc = manifest(ver, args.notes, args.page, exe, macs, key, f"https://github.com/{REPO}/releases/download/{tag}/")
    (out / "latest.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    gh("release", "upload", tag, str(out / "latest.json"), "-R", REPO, "--clobber")
    print(f"\nThe signed latest.json for {ver} is attached to the {tag} release.")
    print(f"Publish it when you're ready (on GitHub, or: gh release edit {tag} -R {REPO} --draft=false).")
    print("Copies of StitchPilot offer the update once it's published.")


def release(args):
    if args.github:
        return from_github(args)
    ver = args.version or version()
    exe = Path(args.exe)
    if not exe.exists():
        sys.exit(f"{exe} not found. Build it first: build\\build_exe.ps1")
    out = Path(args.out) if args.out else ROOT / "release" / ver
    out.mkdir(parents=True, exist_ok=True)
    files = [out / f"StitchPilot-{ver}.exe"]
    shutil.copy2(exe, files[0])
    for mac in args.mac or []:
        files.append(out / f"StitchPilot-{ver}-mac-{mac_arch(Path(mac))}.zip")
        shutil.copy2(mac, files[-1])
    doc = manifest(ver, args.notes, args.page, files[0], files[1:], load_key(Path(args.key)))
    (out / "latest.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    files = [f.name for f in files]
    print(f"Release {ver} written to {out}")
    if args.upload:
        bucket = args.upload.rstrip("/")
        for name in files:  # files first, so latest.json never points at something missing
            subprocess.run(["aws", "s3", "cp", str(out / name), f"{bucket}/{name}"], check=True)
        subprocess.run(["aws", "s3", "cp", str(out / "latest.json"), f"{bucket}/latest.json",
                        "--cache-control", "no-cache", "--content-type", "application/json"], check=True)
        print(f"Uploaded. Copies of StitchPilot will offer {ver} the next time they check.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen", help="make the signing key (once)")
    k.add_argument("--key", default=str(DEFAULT_KEY))
    r = sub.add_parser("release", help="sign and (optionally) upload a release")
    r.add_argument("--notes", required=True, help="what's new, shown in the update window")
    r.add_argument("--github", metavar="TAG", help="sign the draft GitHub release for this tag (e.g. v0.2.0)")
    r.add_argument("--exe", default=str(ROOT / "dist" / "StitchPilot.exe"))
    r.add_argument("--mac", action="append", help="a Mac zip from build_mac.sh (repeat for each processor)")
    r.add_argument("--version", help="defaults to __version__ in stitchpilot/__init__.py")
    r.add_argument("--page", default=PAGE, help="where people can download StitchPilot by hand")
    r.add_argument("--key", default=str(DEFAULT_KEY))
    r.add_argument("--out", help="folder to write the release to (default release/<version>)")
    r.add_argument("--upload", help="s3://bucket-name from the updates stack")
    args = ap.parse_args()
    keygen(Path(args.key)) if args.cmd == "keygen" else release(args)


if __name__ == "__main__":
    main()
