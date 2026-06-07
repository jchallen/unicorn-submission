import re


def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def find_name_candidates(submission: dict, catalog: list) -> list:
    norm_name = normalize(submission.get("name", ""))
    candidates = []

    for product in catalog:
        if normalize(product["canonical_name"]) == norm_name:
            candidates.append(product)
            continue
        for alias in product.get("aliases", []):
            if normalize(alias) == norm_name:
                candidates.append(product)
                break

    return candidates


def apply_attribute_gates(submission: dict, candidates: list) -> list:
    sub_category = submission.get("category", "").strip().lower()
    sub_size = submission.get("size_ml")
    sub_vintage = submission.get("vintage")

    passing = []
    for product in candidates:
        if product.get("category", "").lower() != sub_category:
            continue
        if product.get("size_ml") != sub_size:
            continue
        prod_vintage = product.get("vintage")
        if prod_vintage is not None and sub_vintage != prod_vintage:
            continue
        passing.append(product)

    return passing


def match(submission: dict, catalog: list) -> dict:
    submission_id = submission["submission_id"]
    name_candidates = find_name_candidates(submission, catalog)

    if not name_candidates:
        return {
            "submission_id": submission_id,
            "decision": "no_match",
            "confidence": 0.0,
            "candidate_product_ids": [],
            "explanation": "No catalog entry matched by name or alias",
        }

    passing = apply_attribute_gates(submission, name_candidates)

    if len(passing) == 1:
        product = passing[0]
        return {
            "submission_id": submission_id,
            "decision": "exact_match",
            "confidence": 1.0,
            "candidate_product_ids": [product["product_id"]],
            "explanation": f"Matched to {product['product_id']} via normalized name or alias with category, size, and vintage confirmed",
        }
    elif len(passing) > 1:
        return {
            "submission_id": submission_id,
            "decision": "human_review",
            "confidence": 0.0,
            "candidate_product_ids": [p["product_id"] for p in passing],
            "explanation": f"Ambiguous: {len(passing)} candidates passed all attribute gates",
        }
    else:
        return {
            "submission_id": submission_id,
            "decision": "no_match",
            "confidence": 0.0,
            "candidate_product_ids": [c["product_id"] for c in name_candidates],
            "explanation": f"Name matched {len(name_candidates)} candidate(s) but category, size, or vintage did not align",
        }
