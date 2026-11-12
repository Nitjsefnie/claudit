"""S3 client construction for backend.r2."""
from __future__ import annotations

import re
from datetime import datetime

import boto3.session
import botocore.session
from botocore.config import Config
from botocore.utils import parse_timestamp

from backend.env_config import require_env

# fromisoformat reads this as botocore's dateutil does, but rolls hour 24 to the next day.
_S3_XML_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3])(:[0-9]{2}){2}\.[0-9]{3}Z")


def parse_timestamp_fast(value):
    if isinstance(value, str) and _S3_XML_UTC.fullmatch(value):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return parse_timestamp(value)


def build():
    core = botocore.session.get_session()
    core.get_component("response_parser_factory").set_parser_defaults(
        timestamp_parser=parse_timestamp_fast)
    return boto3.session.Session(botocore_session=core).client(
        "s3",
        endpoint_url=require_env("CLAUDIT_R2_ENDPOINT"),
        aws_access_key_id=require_env("CLAUDIT_R2_ACCESS_KEY_ID"),
        aws_secret_access_key=require_env("CLAUDIT_R2_SECRET_ACCESS_KEY"),
        region_name="auto",
        config=Config(max_pool_connections=32, retries={"max_attempts": 3}),
    )
