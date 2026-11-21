from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from app.dependencies import Archive, Service
from app.modules.archive.proof import export
from app.modules.continuation.service import is_continuation_sample

router = APIRouter(prefix="/api/runs")


class RunRequest(BaseModel):
    sample: str


@router.get("")
def history(service: Service):
    return service.store.history()


@router.post("", status_code=201)
def run(body: RunRequest, request: Request, response: Response):
    if is_continuation_sample(body.sample):
        result = request.app.state.continuations.run(body.sample)
    else:
        result = request.app.state.archives.run(body.sample)
    if result["reused"]:
        response.status_code = 200
    return result


@router.get("/{run_id}")
def manifest(archive: Archive):
    return archive[0]


@router.post("/{run_id}/rerun")
def replay(run_id: str, request: Request):
    record = request.app.state.archives.store.fetch(run_id)[0]
    if record.get("kind") == "continuation":
        return request.app.state.continuations.replay(run_id)
    return request.app.state.archives.replay(run_id)


@router.get("/{run_id}/export")
def evidence(archive: Archive):
    filename = f'museum-{archive[0]["id"][:16]}.json'
    return Response(export(archive), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{filename}"'})
