import json
from pathlib import Path

from .ingest import stream_submissions, load_json_catalog
from .report import append_record
from .llm_match import llm_match

STAGING = Path(__file__).parent.parent / "staging"
TIER2_BUCKETS = ["llm_matched", "llm_no_match", "llm_review_queue"]


def _load_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _clear_tier2_files() -> None:
    for name in TIER2_BUCKETS:
        path = STAGING / f"{name}.json"
        if path.exists():
            path.unlink()


def run(submissions_path: str, catalog_path: str) -> None:
    STAGING.mkdir(exist_ok=True)
    catalog = load_json_catalog(catalog_path)

    submissions_by_id = {}
    for record in stream_submissions(submissions_path):
        submissions_by_id[record["submission_id"]] = record

    tier1_results = []
    for bucket in ("no_match", "human_review"):
        tier1_results.extend(_load_jsonl(STAGING / f"{bucket}.json"))

    if not tier1_results:
        print("No Tier 1 records to process. Run run.py first.")
        return

    _clear_tier2_files()

    counts = {"llm_matched": 0, "llm_no_match": 0, "llm_review_queue": 0}

    for tier1_result in tier1_results:
        sub_id = tier1_result.get("submission_id")
        submission = submissions_by_id.get(sub_id)
        if not submission:
            continue

        result = llm_match(submission, catalog)
        decision = result["decision"]
        append_record(STAGING, decision, result)
        counts[decision] += 1
        print(f"  {sub_id}: {decision} (confidence={result['confidence']:.2f})")

    total = sum(counts.values())
    print(f"\nTier 2 complete — {total} records processed")
    print(f"  LLM matched:   {counts['llm_matched']}")
    print(f"  LLM no match:  {counts['llm_no_match']}")
    print(f"  Review queue:  {counts['llm_review_queue']}")
