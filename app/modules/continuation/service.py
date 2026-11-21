from datetime import datetime, timezone
from time import perf_counter

from app.config import POLICY
from app.errors import FieldError
from app.manifest import continuation_identify, json_sha, preserve_continuation
from app.modules.archive.proof import verify_continuation
from app.modules.continuation.merge import build_continuation_evidence
from app.modules.intake.catalog import CONTINUATION_SAMPLES, load_sample


class ContinuationService:
    def __init__(self, store, loader=load_sample):
        self.store = store
        self.loader = loader

    def run(self, sample):
        start = perf_counter()
        package = self.loader(sample)
        evidence = build_continuation_evidence(package)
        spec = package.spec
        key, hashes = continuation_identify(package)
        manifest = {"id": key, "sample": spec.sample, "label": spec.label, "shift": spec.shift, "policy": POLICY,
                    "kind": "continuation",
                    "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "window": [spec.start, spec.end],
                    "cabinets": [cabinet.model_dump(mode="json") for cabinet in spec.cabinet_hours],
                    "tolerance_seconds": spec.tolerance_seconds, "sustained_hours": spec.sustained_hours,
                    "file": "、".join(source.csv for source in spec.sources),
                    "sources": [{"role": source.role, "csv": source.csv, "records": source.records}
                                for source in spec.sources],
                    "hashes": hashes, "result_sha256": json_sha(evidence),
                    "elapsed_ms": round(1000 * (perf_counter() - start)), "summary": evidence["audit"]["hours"],
                    "continuation": {"published": evidence["continuation"]["published"],
                                     "overlap_hours": evidence["continuation"]["overlap_hours"],
                                     "blocked_hours": evidence["continuation"]["blocked_hours"],
                                     "ledger_sha256": evidence["continuation"]["ledger_sha256"]}}
        record, created = self.store.insert(manifest, evidence, preserve_continuation(package))
        if record["result_sha256"] != manifest["result_sha256"]:
            raise FieldError("RULE_DRIFT", "同版结果摘要不一致", 409)
        return {"manifest": record, "reused": not created}

    def replay(self, run_id):
        archive = self.store.fetch(run_id)
        manifest = archive[0]
        if manifest.get("kind") != "continuation":
            raise FieldError("NOT_CONTINUATION", "该归卷不是接续归卷", 409)
        if manifest["policy"] != POLICY:
            raise FieldError("OLD_POLICY", "当前规则无法复算此归卷", 409)
        pinned = verify_continuation(archive)
        if json_sha(build_continuation_evidence(pinned)) != manifest["result_sha256"]:
            raise FieldError("RULE_DRIFT", "复算结果摘要不一致", 409)
        return {"manifest": manifest, "verified": True, "reused": True}


def is_continuation_sample(sample):
    return sample in CONTINUATION_SAMPLES
