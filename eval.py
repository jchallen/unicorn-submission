"""
Generate product_matching_eval.json by comparing pipeline staging output against
the expected outcomes in provided/expected_issues.json.

Run after run.py (Tier 1 only) or after both run.py and run_tier2.py.
Output is written to staging/product_matching_eval.json.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

STAGING = Path("staging")

# Structured expected values derived from provided/expected_issues.json.
# expected_product_id: the correct catalog product, or null for no-match/invalid.
# acceptable_decisions: all pipeline decisions considered correct for this record.
# false_positive_risk: True if a wrong match here would be a false positive.
EXPECTED = {
    "SUB-2001": {
        "expected_product_id": "PROD-1001",
        "acceptable_decisions": ["exact_match", "llm_matched"],
        "false_positive_risk": False,
    },
    "SUB-2002": {
        "expected_product_id": "PROD-1003",
        "acceptable_decisions": ["exact_match", "llm_matched"],
        "false_positive_risk": False,
    },
    "SUB-2003": {
        "expected_product_id": "PROD-1005",
        "acceptable_decisions": ["llm_matched", "no_match", "human_review", "llm_review_queue"],
        "false_positive_risk": True,
    },
    "SUB-2004": {
        "expected_product_id": None,
        "acceptable_decisions": ["no_match", "human_review", "llm_review_queue", "llm_no_match"],
        "false_positive_risk": True,
    },
    "SUB-2005": {
        "expected_product_id": None,
        "acceptable_decisions": ["no_match", "llm_no_match"],
        "false_positive_risk": True,
    },
    "SUB-2006": {
        "expected_product_id": "PROD-1001",
        "acceptable_decisions": ["exact_match", "human_review", "llm_matched", "llm_review_queue"],
        "false_positive_risk": True,
    },
    "SUB-2007": {
        "expected_product_id": None,
        "acceptable_decisions": ["no_match", "human_review", "llm_review_queue"],
        "false_positive_risk": True,
    },
    "SUB-2008": {
        "expected_product_id": None,
        "acceptable_decisions": ["no_match", "human_review", "llm_no_match", "llm_review_queue"],
        "false_positive_risk": True,
    },
    "SUB-2009": {
        "expected_product_id": "PROD-1008",
        "acceptable_decisions": ["llm_matched", "no_match", "human_review", "llm_review_queue"],
        "false_positive_risk": False,
    },
    "SUB-2010": {
        "expected_product_id": None,
        "acceptable_decisions": ["no_match", "human_review", "llm_review_queue", "llm_no_match"],
        "false_positive_risk": True,
    },
    "SUB-2011": {
        "expected_product_id": None,
        "acceptable_decisions": ["invalid"],
        "false_positive_risk": False,
    },
    "SUB-2012": {
        "expected_product_id": "PROD-1002",
        "acceptable_decisions": ["exact_match", "llm_matched"],
        "false_positive_risk": False,
    },
}


def load_jsonl(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def collect_results():
    results = {}

    for record in load_jsonl(STAGING / "matched.json"):
        sid = record["submission_id"]
        results[sid] = {
            "decision": record["decision"],
            "product_id": record["candidate_product_ids"][0] if record["candidate_product_ids"] else None,
            "tier": 1,
        }

    for record in load_jsonl(STAGING / "no_match.json"):
        sid = record["submission_id"]
        results[sid] = {"decision": record["decision"], "product_id": None, "tier": 1}

    for record in load_jsonl(STAGING / "human_review.json"):
        sid = record["submission_id"]
        results[sid] = {"decision": record["decision"], "product_id": None, "tier": 1}

    for record in load_jsonl(STAGING / "invalid.json"):
        sid = record["record"]["submission_id"]
        results[sid] = {"decision": "invalid", "product_id": None, "tier": 1}

    for record in load_jsonl(STAGING / "llm_matched.json"):
        sid = record["submission_id"]
        results[sid] = {
            "decision": record["decision"],
            "product_id": record["candidate_product_ids"][0] if record["candidate_product_ids"] else None,
            "tier": 2,
        }

    for record in load_jsonl(STAGING / "llm_no_match.json"):
        sid = record["submission_id"]
        results[sid] = {"decision": record["decision"], "product_id": None, "tier": 2}

    for record in load_jsonl(STAGING / "llm_review_queue.json"):
        sid = record["submission_id"]
        results[sid] = {"decision": record["decision"], "product_id": None, "tier": 2}

    return results


def main():
    results = collect_results()
    tiers_run = sorted({r["tier"] for r in results.values()})

    records = []
    for sid, exp in sorted(EXPECTED.items()):
        actual = results.get(sid)
        if not actual:
            records.append({
                "submission_id": sid,
                "expected_product_id": exp["expected_product_id"],
                "actual_product_id": None,
                "product_id_correct": False,
                "acceptable_decisions": exp["acceptable_decisions"],
                "actual_decision": None,
                "decision_acceptable": False,
                "false_positive": False,
                "false_positive_risk": exp["false_positive_risk"],
                "note": "not found in staging — has Tier 1 been run?",
            })
            continue

        actual_pid = actual["product_id"]
        actual_dec = actual["decision"]

        product_id_correct = actual_pid == exp["expected_product_id"]
        decision_acceptable = actual_dec in exp["acceptable_decisions"]

        # A false positive is a confident wrong match on a record where mismatching is risky.
        false_positive = (
            exp["false_positive_risk"]
            and actual_pid is not None
            and actual_pid != exp["expected_product_id"]
        )

        records.append({
            "submission_id": sid,
            "expected_product_id": exp["expected_product_id"],
            "actual_product_id": actual_pid,
            "product_id_correct": product_id_correct,
            "acceptable_decisions": exp["acceptable_decisions"],
            "actual_decision": actual_dec,
            "decision_acceptable": decision_acceptable,
            "false_positive": false_positive,
            "false_positive_risk": exp["false_positive_risk"],
        })

    total = len(records)
    pid_correct = sum(1 for r in records if r["product_id_correct"])
    dec_acceptable = sum(1 for r in records if r["decision_acceptable"])
    false_positives = sum(1 for r in records if r["false_positive"])

    # False-positive weighted score: each FP counts double against the denominator.
    fp_penalty = false_positives
    fp_weighted_score = (dec_acceptable - fp_penalty) / (total + fp_penalty) if total else 0.0

    output = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "tiers_run": tiers_run,
        "summary": {
            "total": total,
            "product_id_correct": pid_correct,
            "product_id_accuracy": round(pid_correct / total, 3),
            "decision_acceptable": dec_acceptable,
            "decision_accuracy": round(dec_acceptable / total, 3),
            "false_positives": false_positives,
            "fp_weighted_score": round(fp_weighted_score, 3),
        },
        "records": records,
    }

    STAGING.mkdir(exist_ok=True)
    out_path = STAGING / "product_matching_eval.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"Tiers run:               Tier {', Tier '.join(str(t) for t in tiers_run)}")
    print(f"Product ID accuracy:     {pid_correct}/{total} ({output['summary']['product_id_accuracy']:.1%})")
    print(f"Decision accuracy:       {dec_acceptable}/{total} ({output['summary']['decision_accuracy']:.1%})")
    print(f"False positives:         {false_positives}")
    print(f"FP-weighted score:       {output['summary']['fp_weighted_score']:.1%}")
    print(f"\nFull results written to {out_path}")


if __name__ == "__main__":
    main()
