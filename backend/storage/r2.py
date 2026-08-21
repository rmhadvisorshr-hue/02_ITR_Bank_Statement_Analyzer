"""Cloudflare R2 storage. Python port of r2.mjs - R2 speaks the S3 API, so
boto3 works unmodified against it, same as the AWS SDK did on the Node side.
This is the only file that knows that detail; callers only ever use
upload_report()/get_download_url().
"""
import os
from functools import lru_cache

import boto3

REQUIRED_ENV = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@lru_cache(maxsize=1)
def _client():
    missing = [key for key in REQUIRED_ENV if not os.environ.get(key)]
    if missing:
        raise RuntimeError(f"R2 storage is not configured. Missing env vars: {', '.join(missing)}")
    return boto3.client(
        "s3",
        region_name="auto",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    )


def upload_report(local_path, key):
    s3 = _client()
    with open(local_path, "rb") as f:
        s3.upload_fileobj(f, os.environ["R2_BUCKET"], key, ExtraArgs={"ContentType": XLSX_CONTENT_TYPE})


def get_download_url(key, download_filename):
    s3 = _client()
    safe_filename = download_filename.replace('"', "")
    return s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": os.environ["R2_BUCKET"],
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{safe_filename}"',
        },
        ExpiresIn=300,
    )
