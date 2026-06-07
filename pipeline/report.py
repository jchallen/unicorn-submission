import json
from datetime import datetime, timezone
from pathlib import Path

OUTPUT_BUCKETS = [
    "invalid", "exact_duplicates", "near_duplicates",
    "matched", "no_match", "human_review",
]


def clear_output_files(staging_dir: Path) -> None:
    for name in OUTPUT_BUCKETS:
        path = staging_dir / f"{name}.json"
        if path.exists():
            path.unlink()


def append_record(staging_dir: Path, bucket: str, record: dict) -> None:
    with open(staging_dir / f"{bucket}.json", "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def write_summary(staging_dir: Path, counts: dict) -> None:
    total = sum(counts.values())
    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "total_submissions": total,
        **counts,
    }
    with open(staging_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"Run at:           {summary['run_at']}")
    print(f"Total:            {total}")
    print(f"Matched:          {counts.get('matched', 0)}")
    print(f"No match:         {counts.get('no_match', 0)}")
    print(f"Human review:     {counts.get('human_review', 0)}")
    print(f"Invalid:          {counts.get('invalid', 0)}")
    print(f"Exact duplicates: {counts.get('exact_duplicates', 0)}")
    print(f"Near duplicates:  {counts.get('near_duplicates', 0)}")
    print(f"\nOutput written to {staging_dir}/")
