"""Minimal S3 client for one bucket prefix: PUT, GET, HEAD, DELETE over SigV4.

Only the four verbs the marts publisher needs, stdlib only. No `mc`, no `boto3`, no shell: the
publisher has to prove what landed in the store, and an external binary that silently falls back
to a *local directory* named `s3:` cannot prove anything. Requests are path-style (MinIO's default)
with `UNSIGNED-PAYLOAD`, so a large Parquet body is never hashed twice.

Nothing here logs or prints a secret; the session key exists only inside one Authorization header.
"""

import hashlib
import hmac
import http.client
import os
import ssl
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
UNSIGNED = "UNSIGNED-PAYLOAD"
REGION = "us-east-1"
SERVICE = "s3"


def _ca_bundle():
    """The trust store to use, or None for whatever the interpreter ships with.

    A Python built outside the distro (uv's standalone builds, containers) has no CA file of its
    own, so an otherwise valid endpoint fails verification. Naming the bundle explicitly keeps
    verification ON; disabling it would be the unsafe shortcut.
    """
    for candidate in (
        os.environ.get("S3_CA_BUNDLE"),
        os.environ.get("SSL_CERT_FILE"),
        "/etc/ssl/certs/ca-bundle.crt",
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


class StoreError(RuntimeError):
    """A request the store answered with a non-success status."""

    def __init__(self, status, method, bucket, key, message=""):
        self.status = status
        self.method = method
        self.bucket = bucket
        self.key = key
        detail = f" ({message})" if message else ""
        super().__init__(f"{method} {bucket}/{key} -> {status}{detail}")


def _hmac(key, message):
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def _canonical_uri(bucket, key):
    segments = [bucket, *(part for part in str(key).split("/") if part)]
    return "/" + "/".join(urllib.parse.quote(part, safe="") for part in segments)


def _signing_key(secret, date, region, service):
    key = _hmac(("AWS4" + secret).encode(), date)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


class Store:
    """One object-store endpoint. Each call opens its own connection, so a stale socket cannot
    turn a retry into a silent duplicate."""

    def __init__(self, endpoint, access_key, secret_key, *, region=REGION, timeout=60):
        endpoint = str(endpoint).rstrip("/")
        if "://" in endpoint:
            raise ValueError("endpoint must be host[:port] without a scheme")
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.timeout = timeout

    def _connection(self):
        host, _, port = self.endpoint.partition(":")
        if port:
            return http.client.HTTPConnection(host, int(port), timeout=self.timeout)
        return http.client.HTTPSConnection(
            host, 443, timeout=self.timeout, context=ssl.create_default_context(cafile=_ca_bundle())
        )

    def _authorization(self, method, path, amz_date):
        date = amz_date[:8]
        signed = "host;x-amz-content-sha256;x-amz-date"
        canonical_headers = (
            f"host:{self.endpoint}\nx-amz-content-sha256:{UNSIGNED}\nx-amz-date:{amz_date}\n"
        )
        canonical_request = "\n".join([method, path, "", canonical_headers, signed, UNSIGNED])
        scope = f"{date}/{self.region}/{SERVICE}/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        signature = hmac.new(
            _signing_key(self.secret_key, date, self.region, SERVICE),
            string_to_sign.encode(),
            hashlib.sha256,
        ).hexdigest()
        return (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}"
        )

    def request(self, method, bucket, key, body=None):
        path = _canonical_uri(bucket, key)
        amz_date = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        headers = {
            "Host": self.endpoint,
            "x-amz-content-sha256": UNSIGNED,
            "x-amz-date": amz_date,
            "Authorization": self._authorization(method, path, amz_date),
        }
        if body is not None:
            headers["Content-Length"] = str(len(body))
        connection = self._connection()
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, dict(response.getheaders()), payload
        finally:
            connection.close()

    def put(self, bucket, key, body):
        """Upload bytes. Returns the store's ETag for the new object."""
        status, headers, _ = self.request("PUT", bucket, key, body=bytes(body))
        if status not in (200, 201):
            raise StoreError(status, "PUT", bucket, key)
        return _etag(headers)

    def get(self, bucket, key):
        status, _, payload = self.request("GET", bucket, key)
        if status != 200:
            raise StoreError(status, "GET", bucket, key)
        return payload

    def head(self, bucket, key):
        """Return {'size': int, 'etag': str} for an existing object, else None."""
        status, headers, _ = self.request("HEAD", bucket, key)
        if status == 404:
            return None
        if status != 200:
            raise StoreError(status, "HEAD", bucket, key)
        lowered = {name.lower(): value for name, value in headers.items()}
        return {"size": int(lowered.get("content-length", 0)), "etag": _etag(lowered)}

    def delete(self, bucket, key):
        status, _, _ = self.request("DELETE", bucket, key)
        if status not in (200, 202, 204, 404):
            raise StoreError(status, "DELETE", bucket, key)


def _etag(headers):
    lowered = {name.lower(): value for name, value in headers.items()}
    return str(lowered.get("etag", "")).strip('"')
