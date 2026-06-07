import json
from pathlib import Path
from typing import Generator


def load_json_catalog(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def stream_submissions(path: str) -> Generator[dict, None, None]:
    with open(path, "r", encoding="utf-8") as f:
        records = json.load(f)
    for record in records:
        yield record
