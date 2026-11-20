"""Creates the artifacts bucket and its lifecycle rule (README.md, Artifacts).

The user, its policy and the policy's attachment are made by `rc`, in the init
containers before this one. Run with the root credentials, which nothing else
uses.
"""

import os
import sys

from s3 import Client, md5_header

BUCKET = "matchmark-artifacts"
LIFECYCLE = (
    b"<LifecycleConfiguration><Rule><ID>abort-incomplete-multipart</ID><Status>Enabled</Status>"
    b"<Filter><Prefix></Prefix></Filter>"
    b"<AbortIncompleteMultipartUpload><DaysAfterInitiation>1</DaysAfterInitiation></AbortIncompleteMultipartUpload>"
    b"</Rule></LifecycleConfiguration>"
)

root = Client(os.environ["ROOT_ACCESS_KEY"], os.environ["ROOT_SECRET_KEY"])
failed = []


def step(name, ok, detail=""):
    print(f"{name}: {'ok' if ok else 'FAILED'} ({detail})", flush=True)
    if not ok:
        failed.append(name)


status, body, _ = root.request("PUT", BUCKET)
step("bucket", status == 200 or (status == 409 and b"BucketAlreadyOwnedByYou" in body), f"status {status}")

status, body, _ = root.request(
    "PUT", BUCKET, query={"lifecycle": ""}, body=LIFECYCLE, headers={"content-md5": md5_header(LIFECYCLE)}
)
step("lifecycle rule put", status == 200, f"status {status} {body[:200].decode(errors='replace')}")

status, body, _ = root.request("GET", BUCKET, query={"lifecycle": ""})
text = body.decode(errors="replace")
step(
    "lifecycle rule read back",
    status == 200 and "AbortIncompleteMultipartUpload" in text and "<DaysAfterInitiation>1<" in text,
    f"status {status} {text[:300]}",
)

if failed:
    print(f"FAILED: {', '.join(failed)}", flush=True)
    sys.exit(1)
print("all steps ok", flush=True)
