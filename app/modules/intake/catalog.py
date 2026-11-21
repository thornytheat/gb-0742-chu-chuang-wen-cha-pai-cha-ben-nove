import base64
import binascii

from pydantic import ValidationError

from app.config import MAX_FILE_BYTES, ROOT
from app.errors import FieldError
from app.modules.intake.contract import Continuation, ContinuationPackage, Package, Shift

CATALOG = {"shift": "值班混合样例", "clean": "全稳校验样例", "truncated": "截断图档样例",
           "continuation": "夜班断网接续样例", "continuation_block": "夜班接续冲突停发样例"}
CONTINUATION_SAMPLES = {"continuation", "continuation_block"}


def load_sample(sample, root=ROOT / "sample_data"):
    if sample not in CATALOG:
        raise FieldError("UNKNOWN_SAMPLE", "样例未登记", 404)
    try:
        if sample in CONTINUATION_SAMPLES:
            return load_continuation(sample, root)
        spec = Shift.model_validate_json((root / f"{sample}.json").read_bytes())
        if spec.sample != sample:
            raise ValueError("样例标识不一致")
        with (root / spec.csv).open("rb") as stream:
            blob = stream.read(MAX_FILE_BYTES + 1)
    except (OSError, ValidationError, ValueError) as exc:
        raise FieldError("BAD_INPUT", "样例契约或点位图档无法读取") from exc
    if len(blob) > MAX_FILE_BYTES:
        raise FieldError("TOO_LARGE", "点位图档超过 1 MiB", 413)
    return Package(spec, blob)


def load_continuation(sample, root=ROOT / "sample_data"):
    try:
        spec = Continuation.model_validate_json((root / f"{sample}.json").read_bytes())
        if spec.sample != sample:
            raise ValueError("样例标识不一致")
        blobs = {}
        for source in spec.sources:
            with (root / source.csv).open("rb") as stream:
                blob = stream.read(MAX_FILE_BYTES + 1)
            if len(blob) > MAX_FILE_BYTES:
                raise FieldError("TOO_LARGE", "点位图档超过 1 MiB", 413)
            blobs[source.csv] = blob
    except (OSError, ValidationError, ValueError) as exc:
        raise FieldError("CONTINUATION_INPUT", "接续契约或原档无法读取") from exc
    return ContinuationPackage(spec, blobs)


def restore(data):
    try:
        return Package(Shift.model_validate(data["contract"]), base64.b64decode(data["csv_base64"], validate=True))
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise FieldError("ARCHIVE_DAMAGED", "归卷输入无法读取", 409) from exc


def restore_continuation(data):
    try:
        spec = Continuation.model_validate(data["contract"])
        blobs = {item["csv"]: base64.b64decode(item["csv_base64"], validate=True) for item in data["sources"]}
        if len(blobs) != len(data["sources"]):
            raise ValueError("原档文件名重复")
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise FieldError("ARCHIVE_DAMAGED", "接续归卷输入无法读取", 409) from exc
    return ContinuationPackage(spec, blobs)
