from collections import Counter

from app.errors import FieldError
from app.manifest import json_sha, sha
from app.modules.archive.pipeline import evaluate
from app.modules.audit.reconcile import reconcile
from app.modules.episodes.segment import segment
from app.reasons import mark
from app.timebase import seconds, utc

ROLES = ("first", "second")
ROLE_LABEL = {"first": "先档", "second": "后档"}


def _load_source(package, source):
    sub = package.package(source)
    evidence = evaluate(sub)
    rows = []
    for row in evidence["rows"]:
        merged = dict(row)
        merged["id"] = f"{source.csv}:{row['line']}"
        merged["source"] = source.role
        merged["source_csv"] = source.csv
        rows.append(merged)
    return source, sub, evidence, rows


def _reading_fingerprint(row):
    return tuple(row["celsius"]), tuple(row["relative_humidity"] or [])


def _hour_slot(spec_start, measured):
    start = seconds(spec_start)
    return start + (measured - start) // 3600 * 3600


def detect_overlap_conflicts(spec, source_rows):
    """跨原档同探头同刻读数：等值只留一份，矛盾整组停发（不跨档配对）。"""
    grouped = {}
    for role in ROLES:
        for row in source_rows[role]:
            if row["disposition"] == "accepted":
                grouped.setdefault((row["probe"], row["measured"]), []).append(row)
    conflicts, equal_pairs = [], []
    for (probe, measured), rows in grouped.items():
        roles_present = {row["source"] for row in rows}
        if len(roles_present) < 2:
            continue
        fingerprints = {_reading_fingerprint(row) for row in rows}
        point = {"probe": probe, "measured_utc": utc(measured), "hour": utc(_hour_slot(spec.start, measured))}
        if len(fingerprints) > 1:
            point["rows"] = [{"source": row["source"], "id": row["id"],
                              "temperature_c": row["celsius"][0], "rh_pct": (row["relative_humidity"] or [None])[0]}
                             for row in rows]
            conflicts.append(point)
        else:
            equal_pairs.append(point)
    return conflicts, equal_pairs


def _slot_index(evidence):
    return {(slot["cabinet"], slot["hour"]): slot for slot in evidence["grades"]}


def merge_ledger(spec, source_evidence):
    slots, authority = [], {}
    begin, end = seconds(spec.start), seconds(spec.end)
    indexes = {role: _slot_index(source_evidence[role]) for role in ROLES}
    for cabinet in spec.cabinet_hours:
        for hour in range(begin, end, 3600):
            key = (cabinet.id, hour)
            first_slot, second_slot = indexes["first"].get(key), indexes["second"].get(key)
            coverage = [role for role, slot in (("first", first_slot), ("second", second_slot))
                        if slot is not None and (slot["upper"] or slot["lower"])]
            paired = {role for role, slot in (("first", first_slot), ("second", second_slot))
                      if slot is not None and slot["upper"] and slot["lower"]}
            if "second" in paired:
                owner = "second"
                reason = "OVERLAP_SECOND_AUTHORITATIVE" if "first" in paired else "SECOND_ONLY"
            elif "first" in paired:
                owner = "first"
                reason = "FIRST_ONLY" if not coverage or coverage == ["first"] else "FIRST_FALLBACK"
            elif coverage:
                owner = coverage[-1]
                reason = "FALLBACK_INCOMPLETE"
            else:
                owner, reason = None, "NO_EVIDENCE"
            chosen = indexes[owner][key] if owner else {"cabinet": cabinet.id, "hour": hour,
                                                         "start": utc(hour), "end": utc(hour + 3600),
                                                         "upper": None, "lower": None, "drift": None,
                                                         "code": "MISSING_UPPER", "state": "MISSING",
                                                         "axes": [], "temperature": None, "humidity": None,
                                                         "gap_mm": cabinet.upper_mm - cabinet.lower_mm}
            ledger_slot = {**chosen, "authority": owner, "authority_label": ROLE_LABEL.get(owner),
                           "coverage": coverage, "merge_reason": reason}
            slots.append(ledger_slot)
            authority[key] = ledger_slot
    return slots, authority


def _merge_rows(source_rows, authority):
    rows, row_ids = [], set()
    winning_ids = {row_id for slot in authority.values() for level in ("upper", "lower")
                   if (row_id := slot[level])}
    for role in ROLES:
        for row in source_rows[role]:
            row_ids.add(row["id"])
            if row["disposition"] == "accepted" and row["id"] not in winning_ids:
                mark(row, "duplicate", "CONTINUATION_DUPLICATE")
            rows.append(row)
    return rows, row_ids, winning_ids


def _merge_unused(source_evidence, winning_ids):
    unused = []
    for role in ROLES:
        for item in source_evidence[role]["unused"]:
            if item["id"] in winning_ids:
                continue
            unused.append({**item, "source": role})
    return unused


def _merge_intake(sources, source_evidence, rows):
    source_ledgers = []
    for source, evidence in zip(sources, (source_evidence["first"], source_evidence["second"])):
        ledger = evidence["intake"]
        source_ledgers.append({"role": source.role, "csv": source.csv, "records": source.records,
                               "encoding": ledger["encoding"], "segments": ledger["segments"],
                               "physical": ledger["physical"], "declared": ledger["declared"]})
    counted = Counter(row["disposition"] for row in rows)
    declared = sum(source.records for source in sources)
    return {"sources": source_ledgers, "declared": declared,
            "accepted": counted["accepted"], "duplicate": counted["duplicate"], "isolated": counted["isolated"],
            "blank": counted["blank"], "header": counted["header"], "physical": sum(item["physical"] for item in source_ledgers)}


def build_continuation_evidence(package):
    spec = package.spec
    loaded = {role: _load_source(package, next(item for item in spec.sources if item.role == role)) for role in ROLES}
    sources = [loaded[role][0] for role in ROLES]
    packages = {role: loaded[role][1] for role in ROLES}
    source_evidence = {role: loaded[role][2] for role in ROLES}
    source_rows = {role: loaded[role][3] for role in ROLES}

    conflicts, equal_pairs = detect_overlap_conflicts(spec, source_rows)
    if conflicts:
        blocked = sorted({item["hour"] for item in conflicts})
        raise FieldError("OVERLAP_CONFLICT",
                         f"重叠读数冲突 {len(conflicts)} 处，涉及柜时 {'、'.join(blocked)}；两份原档保留，停止发布", 422)

    slots, authority = merge_ledger(spec, source_evidence)
    rows, row_ids, winning_ids = _merge_rows(source_rows, authority)
    episodes = segment(slots, spec.sustained_hours)
    intake = _merge_intake(sources, source_evidence, rows)
    audit = reconcile(spec, rows, intake, slots, episodes)
    return {"intake": intake, "rows": rows, "deployments": source_evidence["first"]["deployments"],
            "deployment_conflicts": source_evidence["first"]["deployment_conflicts"],
            "pairs": slots, "unused": _merge_unused(source_evidence, winning_ids),
            "grades": slots, "episodes": episodes, "audit": audit,
            "isolation": dict(sorted(Counter(row["code"] for row in rows if row["disposition"] == "isolated").items())),
            "continuation": continuation_ledger(spec, sources, packages, slots, equal_pairs,
                                                source_evidence, row_ids, winning_ids)}


def continuation_ledger(spec, sources, packages, slots, equal_pairs,
                        source_evidence, row_ids, winning_ids):
    overlap_hours = sorted({point["hour"] for point in equal_pairs})
    per_slot = [{"cabinet": slot["cabinet"], "start": slot["start"], "end": slot["end"],
                 "authority": slot["authority"], "authority_label": slot["authority_label"],
                 "coverage": slot["coverage"], "merge_reason": slot["merge_reason"],
                 "upper": slot["upper"], "lower": slot["lower"], "state": slot["state"]}
                for slot in slots]
    source_summary = []
    for role in ROLES:
        source = next(item for item in sources if item.role == role)
        tally = Counter(slot["state"] for slot in source_evidence[role]["grades"])
        source_summary.append({"role": role, "label": ROLE_LABEL[role], "csv": source.csv,
                               "file_sha256": sha(packages[role].blob), "records": source.records,
                               "paired_hours": sum(1 for slot in source_evidence[role]["grades"]
                                                   if slot["upper"] and slot["lower"]),
                               "stable": tally["STABLE"], "abnormal": tally["ABNORMAL"], "missing": tally["MISSING"]})
    return {"shift": spec.shift, "published": True, "blocked_hours": [],
            "conflicts": [], "equal_overlap_points": equal_pairs, "overlap_hours": overlap_hours,
            "sources": source_summary, "slots": per_slot,
            "row_count": len(row_ids), "authoritative_rows": len(winning_ids),
            "ledger_sha256": json_sha(per_slot)}
