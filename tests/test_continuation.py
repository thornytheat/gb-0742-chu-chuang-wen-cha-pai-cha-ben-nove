from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import FieldError
from app.manifest import continuation_identify, json_sha
from app.modules.continuation.merge import build_continuation_evidence
from app.modules.intake.catalog import load_continuation, restore_continuation
from app.modules.archive.proof import verify_continuation


@pytest.fixture
def continuation_run(client):
    response = client.post("/api/runs", json={"sample": "continuation"})
    assert response.status_code == 201, response.text
    return response.json()["manifest"]


@pytest.fixture
def cbase(continuation_run):
    return f'/api/runs/{continuation_run["id"]}'


def test_overlap_hour_takes_second_source_and_first_only_hours_keep_first(client, cbase):
    ledger = client.get(cbase + "/continuation").json()
    assert ledger["published"] is True and ledger["blocked_hours"] == []
    by_hour = {slot["start"]: slot for slot in ledger["slots"]}
    # 08、09 点为先档独有；10 点为重叠半小时，后档为准；11 点为后档独有
    assert by_hour["2026-11-20T00:00:00Z"]["authority"] == "first"
    assert by_hour["2026-11-20T01:00:00Z"]["merge_reason"] == "FIRST_ONLY"
    overlap = by_hour["2026-11-20T02:00:00Z"]
    assert (overlap["authority"], overlap["merge_reason"], overlap["coverage"]) == (
        "second", "OVERLAP_SECOND_AUTHORITATIVE", ["first", "second"])
    assert overlap["upper"].startswith("continuation-b.csv:")
    assert by_hour["2026-11-20T03:00:00Z"]["authority"] == "second"
    assert ledger["overlap_hours"] == ["2026-11-20T02:00:00Z"]
    assert len(ledger["equal_overlap_points"]) == 2


def test_single_ledger_feeds_page_and_archive_with_balanced_hours(client, cbase):
    audit = client.get(cbase + "/audit").json()
    assert audit["hours"] == {"expected": 4, "stable": 4, "abnormal": 0, "missing": 0,
                              "missing_keys": [], "extra_keys": [], "duplicate_keys": [], "balanced": True}
    pairing = client.get(cbase + "/pairing/CV-17").json()
    assert len(pairing["slots"]) == 4
    assert all(slot["authority"] for slot in pairing["slots"])
    grades = client.get(cbase + "/gradient/CV-17").json()["slots"]
    assert {slot["start"] for slot in grades} == {slot["start"] for slot in pairing["slots"]}
    manifest = client.get(cbase).json()
    assert manifest["kind"] == "continuation" and len(manifest["hashes"]["files_sha256"]) == 2


def test_both_sources_are_preserved_but_overlap_rows_count_once(client, cbase):
    intake = client.get(cbase + "/intake").json()
    source_files = {item["csv"] for item in intake["continuation"]["sources"]}
    assert source_files == {"continuation-a.csv", "continuation-b.csv"}
    # 先档 6 条 + 后档 4 条 = 10；重叠的 2 条先档读数记重复，采信 8 条，柜时不重复入账
    assert intake["declared"] == 10
    assert {row["id"].split(":")[0] for row in intake["rows"]} == source_files
    dispositions = {}
    for row in intake["rows"]:
        dispositions.setdefault(row["disposition"], 0)
        dispositions[row["disposition"]] += 1
    assert dispositions == {"accepted": 8, "duplicate": 2}
    duplicates = [row for row in intake["rows"] if row["disposition"] == "duplicate"]
    assert all(row["code"] == "CONTINUATION_DUPLICATE" and row["source"] == "first" for row in duplicates)
    row_audit = client.get(cbase + "/audit").json()["row_audit"]
    assert row_audit["balanced"] is True and row_audit["total"] == 10


def test_rerun_same_shift_adds_no_record_and_reuses_manifest(client, continuation_run, cbase):
    before = client.get("/api/runs").json()
    again = client.post("/api/runs", json={"sample": "continuation"})
    assert again.status_code == 200 and again.json() == {"manifest": continuation_run, "reused": True}
    assert client.get("/api/runs").json() == before
    replay = client.post(cbase + "/rerun")
    assert replay.status_code == 200 and replay.json()["verified"] is True
    assert len(client.get("/api/runs").json()) == len(before)


def test_concurrent_continuation_submissions_write_one_row(client):
    service = client.app.state.continuations
    with ThreadPoolExecutor(max_workers=5) as pool:
        outcomes = list(pool.map(service.run, ["continuation"] * 5))
    assert sum(not item["reused"] for item in outcomes) == 1
    assert len(client.get("/api/runs").json()) == 1


def test_conflicting_overlap_aborts_publication_and_keeps_previous_version(client, cbase):
    prior = client.get("/api/runs").json()
    blocked = client.post("/api/runs", json={"sample": "continuation_block"})
    assert (blocked.status_code, blocked.json()["code"]) == (422, "OVERLAP_CONFLICT")
    assert "2026-11-20T02:00:00Z" in blocked.json()["message"]
    # 停发不写入新归卷，上一版仍可回看
    assert client.get("/api/runs").json() == prior
    assert client.get(cbase + "/continuation").status_code == 200
    # 原档不被修改，冲突点可直接定位到两边读数
    package = load_continuation("continuation_block")
    with pytest.raises(FieldError) as exc:
        build_continuation_evidence(package)
    assert exc.value.code == "OVERLAP_CONFLICT"
    assert len(client.get("/api/runs").json()) == len(prior)


def test_conflict_detail_names_both_sources_and_values():
    package = load_continuation("continuation_block")
    try:
        build_continuation_evidence(package)
    except FieldError:
        pass
    from app.modules.continuation.merge import _load_source, detect_overlap_conflicts, ROLES
    source_rows = {role: _load_source(package, next(item for item in package.spec.sources if item.role == role))[3]
                   for role in ROLES}
    conflicts, equal = detect_overlap_conflicts(package.spec, source_rows)
    assert len(conflicts) == 1 and len(equal) == 1
    point = conflicts[0]
    assert point["probe"] == "0001" and point["hour"] == "2026-11-20T02:00:00Z"
    assert {row["source"] for row in point["rows"]} == {"first", "second"}
    assert {row["temperature_c"] for row in point["rows"]} == {"22.0000", "23.0000"}
    assert equal[0]["probe"] == "0012"


def test_export_bundle_is_self_verifiable_and_byte_stable(client, cbase):
    delivery = client.get(cbase + "/export")
    item = delivery.json()
    assert item["format"] == "museum-cabinet-continuation-v1"
    assert len(item["input"]["sources"]) == 2
    pinned = verify_continuation((item["manifest"], item["evidence"], item["input"]))
    assert json_sha(build_continuation_evidence(pinned)) == item["manifest"]["result_sha256"]
    assert client.get(cbase + "/export").content == delivery.content
    # 归卷输入可完整还原两份原档
    restored = restore_continuation(item["input"])
    assert set(restored.blobs) == {"continuation-a.csv", "continuation-b.csv"}


def test_tampered_continuation_refuses_replay_and_export(client, cbase):
    service = client.app.state.continuations
    with service.store.connect() as db:
        db.execute("UPDATE shift_archives SET evidence = '{}' WHERE id = ?", (cbase.rsplit("/", 1)[1],))
    for error in [client.post(cbase + "/rerun"), client.get(cbase + "/export")]:
        assert (error.status_code, error.json()["code"]) == (409, "ARCHIVE_DAMAGED")


def test_identity_changes_when_either_source_or_contract_changes():
    package = load_continuation("continuation")
    spec = package.spec
    blob_a = package.blobs["continuation-a.csv"]
    variants = {
        continuation_identify(package)[0],
        continuation_identify(package, "glass-2.0.0")[0],
        continuation_identify(type(package)(spec.model_copy(update={"tolerance_seconds": 120}), package.blobs))[0],
        continuation_identify(type(package)(spec, {**package.blobs, "continuation-a.csv": blob_a + b"\n"}))[0],
    }
    assert len(variants) == 4


def test_rerun_of_continuation_requires_continuation_archive(client, base):
    # 普通归卷不能走接续复算通道
    with pytest.raises(FieldError) as exc:
        client.app.state.continuations.replay(base.rsplit("/", 1)[1])
    assert exc.value.code == "NOT_CONTINUATION"


def test_continuation_pages_render_with_authority_columns(client, cbase):
    for path in ["/archive", "/intake", "/pairing", "/", "/episodes", "/audit"]:
        page = client.get(path)
        assert page.status_code == 200
    pairing = client.get(cbase + "/pairing/CV-17").json()
    assert all({"authority", "merge_reason"} <= set(slot) for slot in pairing["slots"])
    episodes = client.get(cbase + "/episodes/CV-17").json()
    assert episodes == []
    tickets = client.get(cbase + "/audit").json()["tickets"]
    assert tickets[0]["code"] == "STABLE_AT_ALARM"


def test_overlap_hour_falls_back_to_first_when_second_is_unpaired():
    package = load_continuation("continuation")
    spec = package.spec
    sources = [item.model_copy(update={"records": 3}) if item.role == "second" else item for item in spec.sources]
    blob = b"".join(line for line in package.blobs["continuation-b.csv"].splitlines(keepends=True)
                    if not line.startswith(b"0012,2026-11-20T10:12"))
    weakened = type(package)(spec.model_copy(update={"sources": sources}),
                             {**package.blobs, "continuation-b.csv": blob})
    evidence = build_continuation_evidence(weakened)
    slot = next(item for item in evidence["continuation"]["slots"] if item["start"] == "2026-11-20T02:00:00Z")
    assert (slot["authority"], slot["merge_reason"]) == ("first", "FIRST_FALLBACK")
    assert slot["upper"].startswith("continuation-a.csv:")
    assert evidence["audit"]["hours"]["balanced"] is True


def test_sqlite_failure_rolls_back_without_touching_existing_run(client):
    seed = client.post("/api/runs", json={"sample": "shift"}).json()["manifest"]
    store = client.app.state.continuations.store
    with store.connect() as db:
        db.execute("CREATE TRIGGER injected_cont AFTER INSERT ON shift_archives BEGIN SELECT RAISE(ABORT, 'disk'); END")
    failed = client.post("/api/runs", json={"sample": "continuation"})
    assert (failed.status_code, failed.json()["code"]) == (503, "ARCHIVE_UNAVAILABLE")
    with store.connect() as db:
        db.execute("DROP TRIGGER injected_cont")
    assert client.get("/api/runs").json() == [seed]
    # 故障消除后同一输入重跑生成归卷，且重复提交不新增记录
    assert client.post("/api/runs", json={"sample": "continuation"}).status_code == 201
    second = client.post("/api/runs", json={"sample": "continuation"})
    assert second.status_code == 200 and second.json()["reused"] is True
    assert len(client.get("/api/runs").json()) == 2


def test_service_uses_shared_store_with_single_archive_runs(client):
    archives = client.app.state.archives
    continuations = client.app.state.continuations
    assert archives.store is continuations.store
    first = archives.run("shift")
    merged = continuations.run("continuation")
    history = client.get("/api/runs").json()
    assert {item["id"] for item in history} == {first["manifest"]["id"], merged["manifest"]["id"]}
