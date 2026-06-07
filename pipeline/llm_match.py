import anthropic
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

STAGING = Path(__file__).parent.parent / "staging"
CACHE_FILE = STAGING / "llm_cache.json"
AUDIT_FILE = STAGING / "llm_audit.jsonl"
CONFIDENCE_THRESHOLD = 0.85
MODEL = "claude-opus-4-8"

MATCH_TOOL = {
    "name": "record_match_decision",
    "description": (
        "Record the product matching decision for this submission. "
        "Use this to report whether the submission refers to a known catalog entry."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "product_id": {
                "type": ["string", "null"],
                "description": (
                    "The product_id of the best matching catalog entry, "
                    "or null if no catalog entry describes this product."
                ),
            },
            "confidence": {
                "type": "number",
                "description": "Confidence that this decision is correct, from 0.0 (uncertain) to 1.0 (certain).",
            },
            "reason": {
                "type": "string",
                "description": "One sentence explaining why this is or is not a match.",
            },
        },
        "required": ["product_id", "confidence", "reason"],
    },
}


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def _append_audit(entry: dict) -> None:
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _cache_key(submission: dict, candidates: list) -> str:
    payload = json.dumps(
        {"submission": submission, "candidates": candidates},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _build_prompt(submission: dict, candidates: list) -> str:
    lines = [
        "You are a product matching expert for a fine spirits and wine catalog.",
        "A new product submission has been submitted. Determine whether it refers to a known catalog entry.",
        "",
        "## Submission",
        f"Name:     {submission.get('name')}",
        f"Producer: {submission.get('producer')}",
        f"Category: {submission.get('category')}",
        f"Size ml:  {submission.get('size_ml')}",
        f"Vintage:  {submission.get('vintage')}",
        "",
        "## Catalog entries in this category",
    ]
    if candidates:
        for p in candidates:
            lines.append(
                f"  {p['product_id']}: {p['canonical_name']}"
                f" | aliases: {p.get('aliases', [])}"
                f" | size_ml: {p.get('size_ml')}"
                f" | vintage: {p.get('vintage')}"
            )
    else:
        lines.append("  (none — this category is not represented in the catalog)")
    lines += [
        "",
        "Rules:",
        "- Match on the substance of the product: name variations, common abbreviations, and known aliases are fine.",
        "- A size mismatch (e.g. 700ml vs 750ml) is a real difference; lower your confidence unless the name and producer are unmistakable.",
        "- Do NOT match if the producer is clearly different or the product is clearly a different item.",
        "- False positives are worse than false negatives: when uncertain, lower your confidence and let the human review.",
        "- Set confidence >= 0.85 only when you are genuinely certain of the decision.",
        "",
        "Call record_match_decision with your decision.",
    ]
    return "\n".join(lines)


def _build_result(
    sub_id: str,
    product_id,
    confidence: float,
    reason: str,
    cache_hit: bool,
) -> dict:
    if confidence >= CONFIDENCE_THRESHOLD:
        decision = "llm_matched" if product_id else "llm_no_match"
    else:
        decision = "llm_review_queue"
    return {
        "submission_id": sub_id,
        "decision": decision,
        "confidence": confidence,
        "candidate_product_ids": [product_id] if product_id else [],
        "explanation": reason,
        "cache_hit": cache_hit,
    }


def llm_match(submission: dict, catalog: list) -> dict:
    sub_id = submission.get("submission_id", "UNKNOWN")
    category = submission.get("category", "").lower()
    candidates = [p for p in catalog if p.get("category", "").lower() == category]

    cache_key = _cache_key(submission, candidates)
    cache = _load_cache()

    if cache_key in cache:
        cached = cache[cache_key]
        _append_audit({
            "run_at": datetime.now(timezone.utc).isoformat(),
            "submission_id": sub_id,
            "cache_hit": True,
            "product_id": cached["product_id"],
            "confidence": cached["confidence"],
        })
        return _build_result(sub_id, cached["product_id"], cached["confidence"], cached["reason"], True)

    prompt = _build_prompt(submission, candidates)

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            tools=[MATCH_TOOL],
            tool_choice={"type": "tool", "name": "record_match_decision"},
            messages=[{"role": "user", "content": prompt}],
        )

        tool_use = next(b for b in response.content if b.type == "tool_use")
        inp = tool_use.input
        product_id = inp.get("product_id")
        confidence = float(inp.get("confidence", 0.0))
        reason = inp.get("reason", "")

        cache[cache_key] = {"product_id": product_id, "confidence": confidence, "reason": reason}
        _save_cache(cache)

        _append_audit({
            "run_at": datetime.now(timezone.utc).isoformat(),
            "submission_id": sub_id,
            "cache_hit": False,
            "model": MODEL,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "product_id": product_id,
            "confidence": confidence,
        })

        return _build_result(sub_id, product_id, confidence, reason, False)

    except anthropic.APIError as exc:
        _append_audit({
            "run_at": datetime.now(timezone.utc).isoformat(),
            "submission_id": sub_id,
            "cache_hit": False,
            "error": str(exc),
        })
        return {
            "submission_id": sub_id,
            "decision": "llm_review_queue",
            "confidence": 0.0,
            "candidate_product_ids": [],
            "explanation": f"LLM API error: {exc}",
            "cache_hit": False,
        }
