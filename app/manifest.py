import base64
import hashlib
import json

from app.config import POLICY


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_sha(data):
    return sha(canonical(data).encode("utf-8"))


def identify(package, policy=POLICY):
    hashes = {"file_sha256": sha(package.blob), "contract_sha256": json_sha(package.spec.model_dump(mode="json"))}
    return json_sha([policy, hashes]), hashes


def preserve(package):
    return {"contract": package.spec.model_dump(mode="json"), "csv_base64": base64.b64encode(package.blob).decode("ascii")}


def continuation_identify(package, policy=POLICY):
    blobs = [package.blobs[source.csv] for source in package.spec.sources]
    hashes = {"files_sha256": [sha(blob) for blob in blobs],
              "contract_sha256": json_sha(package.spec.model_dump(mode="json"))}
    return json_sha([policy, "continuation", hashes]), hashes


def preserve_continuation(package):
    return {"contract": package.spec.model_dump(mode="json"),
            "sources": [{"role": source.role, "csv": source.csv,
                         "csv_base64": base64.b64encode(package.blobs[source.csv]).decode("ascii")}
                        for source in package.spec.sources]}
