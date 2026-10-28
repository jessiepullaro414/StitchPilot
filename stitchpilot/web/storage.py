"""Where the web app keeps uploads and converted designs between requests.

- MemoryStorage: the local / Docker server (one long-running process). Nothing is written to disk.
- S3Storage: AWS Lambda, where every request may land on a fresh instance. Uploads go from the browser
  straight to S3 (a presigned POST), because Lambda requests are limited to about 6 MB. The bucket's
  lifecycle rules delete everything after a day (see deploy/aws/template.yaml).

Converted designs are cached by a hash of everything they depend on, so a recolour, a shape click or
a download doesn't re-digitize the picture.
"""
from __future__ import annotations

import hashlib
import os
import pickle
import secrets
from collections import OrderedDict
from urllib.parse import quote, unquote

MAX_UPLOAD = 20 * 1024 * 1024


def cache_key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class _LRU:
    def __init__(self, max_items: int, max_bytes: int | None = None):
        self.items: OrderedDict = OrderedDict()
        self.max_items, self.max_bytes, self.bytes = max_items, max_bytes, 0

    def get(self, key):
        if key in self.items:
            self.items.move_to_end(key)
            return self.items[key][0]
        return None

    def put(self, key, value, size: int = 0):
        if key in self.items:
            self.bytes -= self.items.pop(key)[1]
        self.items[key] = (value, size)
        self.bytes += size
        while len(self.items) > self.max_items or (self.max_bytes and self.bytes > self.max_bytes and len(self.items) > 1):
            self.bytes -= self.items.popitem(last=False)[1][1]


class MemoryStorage:
    direct_uploads = False  # the browser posts files to the server itself

    def __init__(self):
        self.uploads = _LRU(300, 768 * 1024 * 1024)
        self.designs = _LRU(24)

    def put_upload(self, image_id: str, name: str, data: bytes) -> None:
        self.uploads.put(image_id, (name, data), len(data))

    def get_upload(self, image_id: str) -> tuple[str, bytes] | None:
        return self.uploads.get(image_id)

    def put_design(self, key: str, design) -> None:
        self.designs.put(key, design)

    def get_design(self, key: str):
        return self.designs.get(key)


class S3Storage:
    direct_uploads = True  # the browser uploads straight to S3, then tells the server

    def __init__(self, bucket: str, client=None):
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client("s3", config=Config(signature_version="s3v4"))
        self.s3, self.bucket = client, bucket
        self.memo = _LRU(8)  # a warm Lambda instance keeps its last few designs

    def _get(self, key: str):
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)
        except self.s3.exceptions.NoSuchKey:
            return None

    def put_upload(self, image_id: str, name: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=f"uploads/{image_id}", Body=data,
                           Metadata={"name": quote(name)})

    def get_upload(self, image_id: str) -> tuple[str, bytes] | None:
        if not image_id or not image_id.isalnum():
            return None
        obj = self._get(f"uploads/{image_id}")
        if obj is None:
            return None
        return unquote(obj.get("Metadata", {}).get("name", "picture.png")), obj["Body"].read()

    def presigned_upload(self) -> dict:
        """Where the browser sends a file: a one-off key, at most MAX_UPLOAD bytes, valid 10 minutes."""
        key = f"uploads/in-{secrets.token_hex(16)}"
        post = self.s3.generate_presigned_post(Bucket=self.bucket, Key=key, ExpiresIn=600,
                                               Conditions=[["content-length-range", 1, MAX_UPLOAD]])
        return {"url": post["url"], "fields": post["fields"], "key": key}

    def take_incoming(self, key: str) -> bytes | None:
        """The file the browser uploaded (removed from its temporary key)."""
        if not key.startswith("uploads/in-") or not key[len("uploads/in-"):].isalnum():
            return None
        obj = self._get(key)
        if obj is None:
            return None
        if int(obj.get("ContentLength", 0)) > MAX_UPLOAD:
            data = None
        else:
            data = obj["Body"].read()
        self.s3.delete_object(Bucket=self.bucket, Key=key)
        return data

    def put_design(self, key: str, design) -> None:
        self.memo.put(key, design)
        self.s3.put_object(Bucket=self.bucket, Key=f"cache/{key}.pkl",
                           Body=pickle.dumps(design, protocol=pickle.HIGHEST_PROTOCOL))

    def get_design(self, key: str):
        d = self.memo.get(key)
        if d is not None:
            return d
        obj = self._get(f"cache/{key}.pkl")
        if obj is None:
            return None
        # only this app can write to the (private) bucket, so its pickles are trusted
        d = pickle.loads(obj["Body"].read())
        self.memo.put(key, d)
        return d

    def put_download(self, name: str, data: bytes, media: str) -> str:
        """Files too big for a Lambda response: stored for an hour's worth of download link."""
        key = f"cache/dl-{secrets.token_hex(16)}"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=media)
        return self.s3.generate_presigned_url(
            "get_object", ExpiresIn=3600,
            Params={"Bucket": self.bucket, "Key": key,
                    "ResponseContentDisposition": f'attachment; filename="{name}"'})


def from_env():
    if os.environ.get("STITCHPILOT_MODE") == "lambda":
        bucket = os.environ.get("STITCHPILOT_BUCKET")
        if not bucket:
            raise RuntimeError("STITCHPILOT_BUCKET isn't set.")
        return S3Storage(bucket)
    return MemoryStorage()
