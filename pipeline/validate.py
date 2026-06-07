from datetime import datetime

ALLOWED_SIZES_ML = {50, 100, 200, 375, 500, 700, 750, 1000, 1500, 3000, 6000}
CURRENT_YEAR = datetime.now().year


def validate(record: dict) -> dict:
    errors = []

    for field in ("submission_id", "name", "producer", "category"):
        value = record.get(field)
        if not value or not str(value).strip():
            errors.append(f"{field} is required and must be non-empty")

    vintage = record.get("vintage")
    if vintage is not None:
        if not isinstance(vintage, int):
            errors.append(f"vintage must be an integer or null, got {type(vintage).__name__}")
        elif not (1800 <= vintage <= CURRENT_YEAR):
            errors.append(f"vintage {vintage} is out of range (1800-{CURRENT_YEAR})")

    size_ml = record.get("size_ml")
    if size_ml is None:
        errors.append("size_ml is required")
    elif size_ml not in ALLOWED_SIZES_ML:
        errors.append(f"size_ml {size_ml} is not an allowed value")

    return {
        "record": record,
        "status": "invalid" if errors else "valid",
        "errors": errors,
    }
