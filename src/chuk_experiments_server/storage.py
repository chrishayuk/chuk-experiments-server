"""Cloudflare R2 (S3-compatible) presigned URLs for artifacts (spec §9/§4).

Presigned URL generation is pure local computation — an HMAC signature over
the request, no network round-trip — so the synchronous boto3 client is used
directly even in this otherwise-async codebase; there's nothing to block on.
Actual upload/download bytes never pass through this server (spec's whole
point: "Checkpoint bytes never transit the little Fly machine").
"""

import boto3
from botocore.client import BaseClient
from botocore.config import Config

from .config import settings
from .constants import PRESIGN_GET_EXPIRY_SECONDS, PRESIGN_PUT_EXPIRY_SECONDS, R2_REGION, R2_SIGNATURE_VERSION

_client: BaseClient | None = None


def get_client() -> BaseClient:
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            config=Config(signature_version=R2_SIGNATURE_VERSION, region_name=R2_REGION),
        )
    return _client


def build_uri(key: str) -> str:
    return f"s3://{settings.r2_bucket}/{key}"


def key_from_uri(uri: str) -> str:
    prefix = f"s3://{settings.r2_bucket}/"
    if not uri.startswith(prefix):
        raise ValueError(f"URI '{uri}' is not in bucket '{settings.r2_bucket}'")
    return uri.removeprefix(prefix)


def presign_put(key: str, content_type: str | None = None) -> str:
    params = {"Bucket": settings.r2_bucket, "Key": key}
    if content_type:
        params["ContentType"] = content_type
    return get_client().generate_presigned_url("put_object", Params=params, ExpiresIn=PRESIGN_PUT_EXPIRY_SECONDS)


def presign_get(key: str) -> str:
    return get_client().generate_presigned_url(
        "get_object", Params={"Bucket": settings.r2_bucket, "Key": key}, ExpiresIn=PRESIGN_GET_EXPIRY_SECONDS
    )
