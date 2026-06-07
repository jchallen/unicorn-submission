import json
from pathlib import Path

from .ingest import stream_submissions, load_json_catalog
from .validate import validate
from .deduplicate import DuplicateTracker
from .match import match
from .report import append_record, write_summary, clear_output_files

STAGING = Path(__file__).parent.parent / "staging"
CHECKPOINT_FILE = STAGING / "checkpoint.json"

EMPTY_COUNTS = {
    "matched": 0,
    "no_match": 0,
    "human_review": 0,
    "invalid": 0,
    "exact_duplicates": 0,
    "near_duplicates": 0,
}


def _load_checkpoint() -> tuple:
    if CHECKPOINT_FILE.exists():
        with open(CHECKPOINT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return set(data["processed_ids"]), data["counts"]
    return set(), dict(EMPTY_COUNTS)


def _save_checkpoint(processed_ids: set, counts: dict) -> None:
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump({"processed_ids": list(processed_ids), "counts": counts}, f)


def run(submissions_path: str, catalog_path: str) -> None:
    STAGING.mkdir(exist_ok=True)
    catalog = load_json_catalog(catalog_path)
    processed_ids, counts = _load_checkpoint()

    if not processed_ids:
        clear_output_files(STAGING)

    tracker = DuplicateTracker()

    for record in stream_submissions(submissions_path):
        sub_id = record.get("submission_id")
        already_done = sub_id in processed_ids

        result = validate(record)
        if result["status"] == "invalid":
            if not already_done:
                append_record(STAGING, "invalid", result)
                counts["invalid"] += 1
                processed_ids.add(sub_id)
                _save_checkpoint(processed_ids, counts)
            continue

        dup_type, original_id = tracker.check(record)

        if dup_type == "exact":
            if not already_done:
                append_record(STAGING, "exact_duplicates", {
                    "record": record,
                    "duplicate_of": original_id,
                    "reason": f"submission_id {sub_id} already seen in this import",
                })
                counts["exact_duplicates"] += 1
                processed_ids.add(sub_id)
                _save_checkpoint(processed_ids, counts)
            continue
        elif dup_type == "near":
            if not already_done:
                append_record(STAGING, "near_duplicates", {
                    "record": record,
                    "duplicate_of": original_id,
                    "reason": "same name, producer, category, size, and vintage as an earlier record",
                })
                counts["near_duplicates"] += 1
                processed_ids.add(sub_id)
                _save_checkpoint(processed_ids, counts)
            continue

        match_result = match(record, catalog)
        if not already_done:
            decision = match_result["decision"]
            if decision == "exact_match":
                append_record(STAGING, "matched", match_result)
                counts["matched"] += 1
            elif decision == "human_review":
                append_record(STAGING, "human_review", match_result)
                counts["human_review"] += 1
            else:
                append_record(STAGING, "no_match", match_result)
                counts["no_match"] += 1
            processed_ids.add(sub_id)
            _save_checkpoint(processed_ids, counts)

    write_summary(STAGING, counts)
    CHECKPOINT_FILE.unlink(missing_ok=True)
