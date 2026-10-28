#!/usr/bin/env python3
"""Build the StitchPilot web page for AWS Amplify Hosting.

    python deploy/aws/build_site.py --api-url https://abc123.lambda-url.us-east-1.on.aws \
        --bucket my-stitchpilot-bucket --region us-east-1

Writes (default folder dist/site):
    index.html              the page, at the site root
    static/*                app.js, style.css, icon.png, ... and config.js pointing at your API
    customHttp.yml          Amplify's custom headers file (security headers with the real CSP)
and dist/site.zip with the same contents, index.html at the zip root, ready for Amplify's manual
"Deploy without Git" upload. No third-party packages needed (Python 3.9 or newer).
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "stitchpilot" / "web" / "static"

BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")


def clean_api_url(value: str) -> str:
    """https://host with no path, query or fragment; returns it without a trailing slash."""
    u = urlsplit(value.strip())
    if u.scheme != "https":
        raise ValueError("--api-url must start with https://")
    if not u.hostname or u.username or u.password:
        raise ValueError("--api-url must look like https://<id>.lambda-url.<region>.on.aws")
    if u.path not in ("", "/") or u.query or u.fragment:
        raise ValueError("--api-url must be just the address, with no path (drop anything after the host)")
    try:
        port = u.port
    except ValueError:
        raise ValueError("--api-url has an invalid port")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", u.hostname):
        raise ValueError("--api-url has unexpected characters in the host name")
    return f"https://{u.hostname.lower()}" + (f":{port}" if port else "")


def clean_bucket(value: str) -> str:
    v = value.strip()
    if not BUCKET_RE.fullmatch(v) or ".." in v or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", v):
        raise ValueError("--bucket doesn't look like an S3 bucket name (3-63 lowercase letters, digits, dots, hyphens)")
    return v


def clean_region(value: str) -> str:
    v = value.strip()
    if not REGION_RE.fullmatch(v):
        raise ValueError("--region should look like us-east-1")
    return v


def content_security_policy(api_url: str, bucket: str, region: str) -> str:
    s3 = f"https://{bucket}.s3.{region}.amazonaws.com https://{bucket}.s3.amazonaws.com"
    return (
        "default-src 'self'; "
        f"img-src 'self' data: blob: {api_url} {s3}; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; "
        f"connect-src 'self' {api_url} {s3}; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )


def custom_http_yml(csp: str) -> str:
    headers = [
        ("Content-Security-Policy", csp),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
    ]
    out = ["customHeaders:", "  - pattern: '**'", "    headers:"]
    for key, value in headers:
        assert '"' not in value and "\\" not in value
        out += [f"      - key: '{key}'", f'        value: "{value}"']
    return "\n".join(out) + "\n"


def build(api_url: str, bucket: str, region: str, out: Path, zip_path: Path) -> None:
    if not STATIC.is_dir() or not (STATIC / "index.html").is_file():
        raise SystemExit(f"Can't find the page in {STATIC}. Run this from a full copy of the project.")
    out = out.resolve()
    zip_path = zip_path.resolve()
    if out == ROOT or out in ROOT.parents or out in STATIC.parents or STATIC in out.parents or out == STATIC:
        raise SystemExit(f"Refusing to use {out} as the output folder: it would overwrite the project.")

    if out.exists():
        shutil.rmtree(out)
    (out / "static").mkdir(parents=True)
    shutil.copy2(STATIC / "index.html", out / "index.html")
    for f in sorted(STATIC.iterdir()):
        if f.is_file() and f.name != "index.html" and not f.name.startswith("."):
            shutil.copy2(f, out / "static" / f.name)
    (out / "static" / "config.js").write_text(f'window.STITCHPILOT_API = "{api_url}";\n', encoding="utf-8")
    csp = content_security_policy(api_url, bucket, region)
    (out / "customHttp.yml").write_text(custom_http_yml(csp), encoding="utf-8")

    html = (out / "index.html").read_text(encoding="utf-8")
    if "/static/config.js" not in html:
        print("warning: index.html doesn't load /static/config.js, so the page will call its own address.",
              file=sys.stderr)

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    files = sorted((p for p in out.rglob("*") if p.is_file()), key=lambda p: (p.name != "index.html", str(p)))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, p.relative_to(out).as_posix())  # index.html at the zip root, forward slashes

    print(f"Site folder: {out}")
    print(f"Zip for Amplify manual deploy: {zip_path}")
    print(f"API: {api_url}")
    print(f"Uploads allowed to: https://{bucket}.s3.{region}.amazonaws.com")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Build the StitchPilot page for AWS Amplify Hosting.")
    ap.add_argument("--api-url", required=True, help="Lambda Function URL, e.g. https://abc.lambda-url.us-east-1.on.aws")
    ap.add_argument("--bucket", required=True, help="S3 bucket name (the BucketName output of the stack)")
    ap.add_argument("--region", required=True, help="AWS region of the bucket, e.g. us-east-1")
    ap.add_argument("--out", default="dist/site", help="output folder (default dist/site)")
    ap.add_argument("--zip", default=None, help="zip file to write (default: next to the folder, as site.zip)")
    a = ap.parse_args(argv)
    try:
        api_url, bucket, region = clean_api_url(a.api_url), clean_bucket(a.bucket), clean_region(a.region)
    except ValueError as e:
        ap.error(str(e))
    out = Path(a.out).resolve()
    zip_path = Path(a.zip) if a.zip else out.parent / f"{out.name}.zip"
    build(api_url, bucket, region, out, zip_path)


if __name__ == "__main__":
    main()
