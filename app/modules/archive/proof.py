from app.errors import FieldError
from app.manifest import canonical, continuation_identify, identify, json_sha
from app.modules.intake.catalog import restore, restore_continuation


def verify(archive):
    manifest, evidence, source = archive
    package = restore(source)
    identity, hashes = identify(package, manifest["policy"])
    if (identity != manifest["id"] or hashes != manifest["hashes"] or json_sha(evidence) != manifest["result_sha256"]):
        raise FieldError("ARCHIVE_DAMAGED", "归卷摘要不符", 409)
    return package


def verify_continuation(archive):
    manifest, evidence, source = archive
    package = restore_continuation(source)
    identity, hashes = continuation_identify(package, manifest["policy"])
    if (identity != manifest["id"] or hashes != manifest["hashes"] or json_sha(evidence) != manifest["result_sha256"]):
        raise FieldError("ARCHIVE_DAMAGED", "接续归卷摘要不符", 409)
    return package


def export(archive):
    manifest = archive[0]
    if manifest.get("kind") == "continuation":
        verify_continuation(archive)
        bundle_format = "museum-cabinet-continuation-v1"
    else:
        verify(archive)
        bundle_format = "museum-cabinet-evidence-v1"
    manifest, evidence, source = archive
    return canonical({"format": bundle_format, "manifest": manifest,
                      "input": source, "evidence": evidence}).encode("utf-8")
