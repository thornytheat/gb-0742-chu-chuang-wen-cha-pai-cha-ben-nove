from fastapi import APIRouter

from app.dependencies import Archive

router = APIRouter(prefix="/api/runs/{run_id}/continuation")


@router.get("")
def ledger(archive: Archive):
    manifest, evidence, _ = archive
    record = evidence.get("continuation")
    if record is None:
        return {"kind": manifest.get("kind", "single"), "published": True, "slots": [], "sources": [],
                "overlap_hours": [], "blocked_hours": [], "conflicts": [], "equal_overlap_points": [],
                "row_count": 0, "authoritative_rows": 0, "ledger_sha256": None}
    return record
