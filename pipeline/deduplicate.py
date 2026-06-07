class DuplicateTracker:
    def __init__(self):
        self._seen_ids = {}
        self._seen_content = {}

    def check(self, record: dict) -> tuple:
        sub_id = record["submission_id"]

        if sub_id in self._seen_ids:
            return "exact", sub_id

        content_key = (
            record.get("name", "").strip().lower(),
            record.get("producer", "").strip().lower(),
            record.get("category", "").strip().lower(),
            record.get("size_ml"),
            record.get("vintage"),
        )

        if content_key in self._seen_content:
            return "near", self._seen_content[content_key]

        self._seen_ids[sub_id] = True
        self._seen_content[content_key] = sub_id
        return None, None
