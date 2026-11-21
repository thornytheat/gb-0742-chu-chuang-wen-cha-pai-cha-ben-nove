from fastapi import APIRouter

from app.dependencies import Archive, query_time
from app.modules.assignment.windows import DeploymentIndex
from app.modules.intake.contract import Continuation, Shift

router = APIRouter(prefix="/api/runs/{run_id}/assignment")


def _contract(source):
    if "sources" in source["contract"]:
        spec = Continuation.model_validate(source["contract"])
        first = next(item for item in spec.sources if item.role == "first")
        return spec.to_shift(first.csv, first.records)
    return Shift.model_validate(source["contract"])


@router.get("")
def assignments(archive: Archive):
    snapshot = archive[1]
    return {"windows": snapshot["deployments"], "conflicts": snapshot["deployment_conflicts"],
            "issues": [row for row in snapshot["rows"] if row["code"] in {"NO_DEPLOYMENT", "DEPLOYMENT_CONFLICT", "OFF_SHIFT"}],
            "late": [row for row in snapshot["rows"] if "LATE_RECEIPT" in row.get("flags", [])]}


@router.get("/lookup")
def lookup(archive: Archive, probe: str, at: str):
    spec = _contract(archive[2])
    window, reason = DeploymentIndex(spec).resolve(probe, query_time(at))
    return {"deployment": window, "code": reason}
