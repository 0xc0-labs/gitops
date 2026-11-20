"""A minimal S3 client over the standard library, signing with SigV4.

Shared by the bucket's setup and the worker's proof (README.md, Artifacts):
the pods that run them have no way to install a library.
"""

import base64
import datetime
import hashlib
import hmac
import http.client
import urllib.parse

HOST = "rustfs.matchmark.svc.cluster.local"
PORT = 9000
REGION = "us-east-1"


def md5_header(body):
    return base64.b64encode(hashlib.md5(body).digest()).decode()  # nosemgrep: python.lang.security.insecure-hash-algorithms-md5.insecure-hash-algorithm-md5 -- S3's Content-MD5 is an integrity check, not a secret


def _sign(key, message):
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


class Client:
    def __init__(self, access_key, secret_key):
        self.access_key = access_key
        self.secret_key = secret_key

    def request(self, method, bucket="", key="", query=None, body=b"", headers=None):
        """(status, body, response headers). Path-style, like the worker's adapter."""
        path = urllib.parse.quote("/" + "/".join(part for part in (bucket, key) if part), safe="/-_.~")
        pairs = sorted(
            (urllib.parse.quote(name, safe="-_.~"), urllib.parse.quote(value, safe="-_.~"))
            for name, value in (query or {}).items()
        )
        canonical_query = "&".join(f"{name}={value}" for name, value in pairs)
        now = datetime.datetime.now(datetime.timezone.utc)
        stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        signed = {
            "host": f"{HOST}:{PORT}",
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": stamp,
            **{name.lower(): value for name, value in (headers or {}).items()},
        }
        names = ";".join(sorted(signed))
        canonical = "\n".join([
            method, path, canonical_query,
            "".join(f"{name}:{signed[name].strip()}\n" for name in sorted(signed)),
            names, payload_hash,
        ])
        scope = f"{day}/{REGION}/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        derived = _sign(_sign(_sign(_sign(("AWS4" + self.secret_key).encode(), day), REGION), "s3"), "aws4_request")
        signature = hmac.new(derived, to_sign.encode(), hashlib.sha256).hexdigest()
        send = {name: value for name, value in signed.items() if name != "host"}
        send["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, SignedHeaders={names}, Signature={signature}"
        )
        connection = http.client.HTTPConnection(HOST, PORT, timeout=30)  # nosemgrep: python.lang.security.audit.insecure-transport.http-client.http-client-insecure-transport -- plain HTTP inside the namespace, where its network policies stand in for TLS
        try:
            connection.request(method, path + ("?" + canonical_query if canonical_query else ""), body=body or None, headers=send)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()
