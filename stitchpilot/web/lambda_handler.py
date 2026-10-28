"""AWS Lambda entry point: the same Starlette app, behind a Lambda Function URL (deploy/aws).

Set STITCHPILOT_MODE=lambda and STITCHPILOT_BUCKET in the function's environment: jobs then run inline
(one request per instance) and uploads and converted designs live in S3.
"""
import asyncio
import os

os.environ.setdefault("STITCHPILOT_MODE", "lambda")

from mangum import Mangum  # noqa: E402

from . import jobs  # noqa: E402
from .server import app  # noqa: E402

jobs.init_worker()  # Qt offscreen, picture size limits
_mangum = Mangum(app, lifespan="off")  # nothing to start: storage is created on first use
_loop = asyncio.new_event_loop()  # Mangum asks for the current loop; newer Pythons no longer make one


def handler(event, context):
    asyncio.set_event_loop(_loop)
    return _mangum(event, context)
