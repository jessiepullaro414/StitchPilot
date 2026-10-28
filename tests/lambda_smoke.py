"""The web app as AWS Lambda, without AWS: the real Lambda handler (Mangum) gets Function URL events,
and an in-memory stand-in for S3 checks what is stored, uploaded straight from the "browser" and
handed out as download links. Each request runs as if on a fresh instance."""
import base64
import io
import json
import os
import sys
import zipfile
from pathlib import Path

here = Path(__file__).parent
sys.path.insert(0, str(here.parent))
os.environ.update(STITCHPILOT_MODE="lambda", STITCHPILOT_BUCKET="stitchpilot-test", QT_QPA_PLATFORM="offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")


class FakeS3:
    """Just the S3 calls StitchPilot makes."""

    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.posts: list[dict] = []

    def put_object(self, Bucket, Key, Body, Metadata=None, ContentType=None):
        assert Bucket == "stitchpilot-test"
        self.objects[Key] = {"Body": bytes(Body), "Metadata": Metadata or {}, "ContentType": ContentType}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey(Key)
        o = self.objects[Key]
        return {"Body": io.BytesIO(o["Body"]), "Metadata": o["Metadata"], "ContentLength": len(o["Body"])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def generate_presigned_post(self, Bucket, Key, ExpiresIn, Conditions):
        self.posts.append({"Key": Key, "Conditions": Conditions, "ExpiresIn": ExpiresIn})
        return {"url": f"https://{Bucket}.s3.amazonaws.com/", "fields": {"key": Key, "policy": "x", "x-amz-signature": "y"}}

    def generate_presigned_url(self, op, ExpiresIn, Params):
        return f"https://{Params['Bucket']}.s3.amazonaws.com/{Params['Key']}?signed=1"


def main():
    from stitchpilot.web import lambda_handler, server, storage

    s3 = FakeS3()

    def call(method, path, body=None, headers=None, ip="203.0.113.7"):
        server.store = storage.S3Storage("stitchpilot-test", client=s3)  # a fresh instance every time
        event = {
            "version": "2.0", "routeKey": "$default", "rawPath": path, "rawQueryString": "",
            "headers": {"content-type": "application/json", **(headers or {})},
            "requestContext": {"accountId": "anonymous", "apiId": "abc", "domainName": "abc.lambda-url.us-east-1.on.aws",
                               "domainPrefix": "abc", "requestId": "r1", "routeKey": "$default", "stage": "$default",
                               "time": "27/Oct/2026:12:00:00 +0000", "timeEpoch": 0,
                               "http": {"method": method, "path": path, "protocol": "HTTP/1.1", "sourceIp": ip,
                                        "userAgent": "test"}},
            "body": json.dumps(body) if body is not None else None, "isBase64Encoded": False,
        }
        out = lambda_handler.handler(event, type("Ctx", (), {"aws_request_id": "r1"})())
        raw = base64.b64decode(out["body"]) if out.get("isBase64Encoded") else out["body"].encode()
        ctype = out["headers"].get("content-type", "")
        return out["statusCode"], (json.loads(raw) if ctype.startswith("application/json") else raw), out["headers"]

    status, opt, _ = call("GET", "/api/options")
    assert status == 200 and opt["uploads"] == "direct", opt.get("uploads")
    state = dict(opt["defaults"])

    # ---- upload: ask where, "the browser" puts the file there, then the server checks it
    status, target, _ = call("POST", "/api/upload-url", {"name": "logo.jpg", "size": 70_000})
    key = target["key"]
    assert status == 200 and key.startswith("uploads/in-") and target["fields"]["key"] == key
    assert ["content-length-range", 1, 20 * 1024 * 1024] in s3.posts[-1]["Conditions"], "S3 enforces the size"
    assert call("POST", "/api/upload-url", {"name": "x.png", "size": 30_000_000})[0] == 413
    s3.put_object(Bucket="stitchpilot-test", Key=key, Body=(here / "samples" / "logo.jpg").read_bytes())
    status, up, _ = call("POST", "/api/upload-done", {"key": key, "name": "logo.jpg"})
    assert status == 200 and up["width"] == 800, up
    assert key not in s3.objects and f"uploads/{up['image_id']}" in s3.objects, "kept under its own id"
    assert call("POST", "/api/upload-done", {"key": key, "name": "logo.jpg"})[0] == 400, "a key works once"
    assert call("POST", "/api/upload-done", {"key": "uploads/../cache/x", "name": "x"})[0] == 400
    assert call("POST", "/api/upload", None)[0] == 400, "no uploads through Lambda"

    # ---- convert: the design is cached in S3, so the next instance doesn't digitize again
    status, r, _ = call("POST", "/api/convert", {"image_id": up["image_id"], "state": state})
    assert status == 200 and r["colours"] == 3 and r["shape_list"], r.get("error")
    cached = [k for k in s3.objects if k.startswith("cache/")]
    assert len(cached) == 1, cached
    body = json.dumps(r)
    print(f"converted: {r['stitches']} stitches; response {len(body) / 1024:.0f} KB (Lambda allows 6 MB)")
    assert len(body) < 5 * 1024 * 1024
    yellow = next(t for t in r["threads"] if t["source"][0] > 200 and t["source"][2] < 120)
    status, r2, _ = call("POST", "/api/convert", {"image_id": up["image_id"],
                                                  "state": {**state, "overrides": [[yellow["source"], [1, 2, 3]]]}})
    assert status == 200 and len([k for k in s3.objects if k.startswith("cache/")]) == 1, "recolour reused the cache"

    # ---- downloads: small files in the response, big ones as a link
    job = {"image_id": up["image_id"], "state": state, "name": "logo"}
    status, pes, h = call("POST", "/api/download", {**job, "kind": "embroidery", "ext": "pes"})
    assert status == 200 and pes.startswith(b"#PES") and 'filename="logo.pes"' in h["content-disposition"]
    status, pdf, _ = call("POST", "/api/download", {**job, "kind": "template", "paper": "A4"})
    assert status == 200 and pdf.startswith(b"%PDF")
    status, proj, _ = call("POST", "/api/download", {**job, "kind": "project"})
    assert status == 200 and zipfile.ZipFile(io.BytesIO(proj)).read("source/logo.jpg")
    server.MAX_RESPONSE = 1000  # pretend the project is too big for a Lambda response
    status, link, _ = call("POST", "/api/download", {**job, "kind": "project"})
    server.MAX_RESPONSE = 4 * 1024 * 1024
    assert status == 200 and link["url"].startswith("https://stitchpilot-test.s3.amazonaws.com/cache/dl-"), link
    dl_key = link["url"].split(".com/")[1].split("?")[0]
    assert s3.objects[dl_key]["Body"] == proj

    # ---- the rest of the page's calls
    status, png, _ = call("POST", "/api/text-preview", {"item": {"text": "Hi", "arc": 90}})
    assert status == 200 and png.startswith(b"\x89PNG")
    status, err, _ = call("POST", "/api/convert", {"image_id": "f" * 32, "state": state})
    assert status == 410 and err["error"] == "upload-again"
    status, err, _ = call("POST", "/api/ai/text", {"image_id": up["image_id"]})
    assert status == 400 and "API key" in err["error"]
    status, pic, _ = call("GET", f"/api/picture/{up['image_id']}")
    assert status == 200 and pic[:2] == b"\xff\xd8"
    print("lambda ok")


if __name__ == "__main__":
    main()
