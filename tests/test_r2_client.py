"""The S3 client built for backend.r2: S3's XML UTC timestamps read directly, the rest by botocore."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from time import thread_time

import pytest
from botocore import utils as botocore_utils
from botocore.awsrequest import AWSResponse
from dateutil.tz import tzutc
from psycopg.adapt import PyFormat, Transformer

from backend import r2, r2_client

_FAST = [
    "2026-11-11T19:22:00.000Z", "2026-01-02T00:00:00.000Z", "2026-11-11T23:59:59.999Z",
    "0001-01-01T00:00:00.000Z", "9999-12-31T23:59:59.999Z",
]
# fromisoformat accepts each, and reads the first two differently from botocore.
_FALLBACK = [
    "2026-11-11T24:00:00.000Z", "2026-W46-3T19:22:00.000Z", "1731352920", "1731352920.5",
    "2026-11-11", "20261111T192200Z", "2026-11-11T19:22:00.000+01:00",
    "2026-11-11T19:22:00.000+00:00", "2026-11-11T19:22:00.000", "2026-11-11T19:22:00Z",
    "2026-11-11T19:22:00.123456Z", "2026-11-11 19:22:00.000Z", "2026-11-11T19:22:00,000Z",
    "2026-11-11t19:22:00.000z", "2026-11-11T19:22:00.000Z\n", "٢٠٢٦-11-11T19:22:00.000Z",
    "Wed, 11 Nov 2026 19:22:00 GMT", "2026-02-30T00:00:00.000Z", "2026-11-11T23:59:60.000Z",
    1731352920, 1731352920.5,
]


def _outcome(parse, value):
    try:
        return parse(value)
    except ValueError as err:
        return type(err)


@pytest.mark.parametrize("value", _FAST + _FALLBACK)
def test_parser_agrees_with_botocore_and_reads_only_s3s_shape_itself(monkeypatch, value) -> None:
    delegated: list = []

    def oracle(given):
        delegated.append(given)
        return botocore_utils.parse_timestamp(given)
    monkeypatch.setattr(r2_client, "parse_timestamp", oracle)
    got, want = _outcome(r2_client.parse_timestamp_fast, value), _outcome(botocore_utils.parse_timestamp, value)
    if value in _FAST:
        assert not delegated
        assert isinstance(got, datetime) and got == want and got.tzinfo is timezone.utc
    else:
        assert delegated == [value]
        assert repr(got) == repr(want)


def test_fast_path_costs_under_half_of_botocores_cpu() -> None:
    stamps = [f"2026-11-{day:02d}T{hour:02d}:22:00.000Z" for day in range(1, 29) for hour in range(24)]

    def cpu(parse) -> float:
        started = thread_time()
        for stamp in stamps:
            parse(stamp)
        return thread_time() - started
    assert cpu(r2_client.parse_timestamp_fast) < cpu(botocore_utils.parse_timestamp) / 2


def test_utc_tzinfo_reads_the_same_to_every_consumer() -> None:
    """Listed timestamps go to ==/</hash, str (json default), isoformat and psycopg."""
    fast, slow = datetime(2026, 11, 11, 19, 22, tzinfo=timezone.utc), datetime(2026, 11, 11, 19, 22, tzinfo=tzutc())
    later = datetime(2026, 11, 11, 19, 23, tzinfo=tzutc())
    assert fast == slow and hash(fast) == hash(slow) and str(fast) == str(slow)
    assert fast < later and fast.isoformat() == slow.isoformat()
    transformer = Transformer()
    for fmt in (PyFormat.TEXT, PyFormat.BINARY):
        assert transformer.get_dumper(fast, fmt).dump(fast) == transformer.get_dumper(slow, fmt).dump(slow)


class _Raw:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def stream(self, **_kwargs):
        yield self.body


_LISTING = (b'<?xml version="1.0" encoding="UTF-8"?><ListBucketResult '
            b'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>claude</Name><KeyCount>1</KeyCount>'
            b'<MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated><Contents><Key>p/s.jsonl</Key>'
            b'<LastModified>2026-11-11T19:22:00.000Z</LastModified><ETag>"e"</ETag><Size>3</Size>'
            b'<StorageClass>STANDARD</StorageClass></Contents></ListBucketResult>')


def test_built_client_parses_listings_with_the_fast_parser(monkeypatch) -> None:
    monkeypatch.setattr(r2, "_tls", threading.local())
    monkeypatch.setenv("CLAUDIT_R2_ENDPOINT", "https://r2.invalid/s3")
    monkeypatch.setenv("CLAUDIT_R2_ACCESS_KEY_ID", "fixture-id")
    monkeypatch.setenv("CLAUDIT_R2_SECRET_ACCESS_KEY", "fixture-secret")
    client = r2._boto_client()  # pylint: disable=protected-access
    answers = {"ListObjectsV2": (_LISTING, {}),
               "HeadObject": (b"", {"Last-Modified": "Wed, 11 Nov 2026 19:22:00 GMT",
                                    "Content-Length": "3", "ETag": '"e"'})}
    for operation, (body, headers) in answers.items():
        client.meta.events.register(
            f"before-send.s3.{operation}",
            lambda request, body=body, headers=headers, **_: AWSResponse(
                request.url, 200, headers, _Raw(body)))
    try:
        listed = client.list_objects_v2(Bucket="claude")["Contents"][0]["LastModified"]
        header = client.head_object(Bucket="claude", Key="p/s.jsonl")["LastModified"]
    finally:
        client.close()
    when = datetime(2026, 11, 11, 19, 22, tzinfo=timezone.utc)
    assert listed == when and listed.tzinfo is timezone.utc
    assert header == when and isinstance(header.tzinfo, tzutc)
