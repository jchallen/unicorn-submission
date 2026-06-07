import json
import pytest
from pathlib import Path
from unittest.mock import patch

STAGING = Path("staging")


@pytest.fixture(scope="session", autouse=True)
def run_pipeline():
    from pipeline.pipeline import run
    run(
        "provided/product_matching_submissions.json",
        "provided/product_catalog.json",
    )


def load(filename):
    path = STAGING / filename
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_summary():
    with open(STAGING / "summary.json", encoding="utf-8") as f:
        return json.load(f)


class TestMatched:
    def test_count(self):
        assert len(load("matched.json")) == 4

    def test_decisions(self):
        matched = {r["submission_id"]: r for r in load("matched.json")}
        assert matched["SUB-2001"]["candidate_product_ids"] == ["PROD-1001"]
        assert matched["SUB-2002"]["candidate_product_ids"] == ["PROD-1003"]
        assert matched["SUB-2012"]["candidate_product_ids"] == ["PROD-1002"]

    def test_sub_2006_resolves_to_prod_1001(self):
        # SUB-2006 is "Macallan 18" at 750ml. expected_issues flags this as ambiguous
        # between PROD-1001 and PROD-1007, but only PROD-1001 has "Macallan 18" as an
        # explicit alias. PROD-1007 (Double Cask) does not. Tier 1 resolves it deterministically.
        matched = {r["submission_id"]: r for r in load("matched.json")}
        assert matched["SUB-2006"]["candidate_product_ids"] == ["PROD-1001"]

    def test_all_decisions_are_exact_match(self):
        for r in load("matched.json"):
            assert r["decision"] == "exact_match"

    def test_all_have_required_fields(self):
        for r in load("matched.json"):
            assert "submission_id" in r
            assert "decision" in r
            assert "confidence" in r
            assert "candidate_product_ids" in r
            assert "explanation" in r


class TestNoMatch:
    def test_count(self):
        assert len(load("no_match.json")) == 7

    def test_correct_submissions(self):
        ids = {r["submission_id"] for r in load("no_match.json")}
        assert ids == {
            "SUB-2003",  # "Pappy Van Winkle Fifteen" — "Fifteen" vs "15" requires LLM
            "SUB-2004",  # "Macallan 18" at 700ml — size mismatch
            "SUB-2005",  # "Made Up Estate Reserve" — not in catalog
            "SUB-2007",  # "Chateau Margaux" no vintage — ambiguous between 2005/2006
            "SUB-2008",  # "Reserve Red Wine" — too generic
            "SUB-2009",  # "Screaming Eagle Cabernet" — partial name, no exact alias
            "SUB-2010",  # "Yamazaki 18" at 750ml — size mismatch (catalog is 700ml)
        }


class TestInvalid:
    def test_count(self):
        assert len(load("invalid.json")) == 1

    def test_sub_2011_rejected_for_empty_name(self):
        invalid = load("invalid.json")
        assert invalid[0]["record"]["submission_id"] == "SUB-2011"
        assert any("name" in e for e in invalid[0]["errors"])


class TestDuplicates:
    def test_no_exact_duplicates(self):
        assert len(load("exact_duplicates.json")) == 0

    def test_no_near_duplicates(self):
        assert len(load("near_duplicates.json")) == 0


class TestSummary:
    def test_total_submissions(self):
        assert load_summary()["total_submissions"] == 12

    def test_counts_add_up(self):
        s = load_summary()
        total = (
            s["matched"]
            + s["no_match"]
            + s["human_review"]
            + s["invalid"]
            + s["exact_duplicates"]
            + s["near_duplicates"]
        )
        assert total == s["total_submissions"]

    def test_run_at_is_present(self):
        assert "run_at" in load_summary()


class TestCheckpointResume:
    def test_resumes_correctly_after_crash(self):
        from pipeline.pipeline import run, STAGING, CHECKPOINT_FILE
        from pipeline.report import clear_output_files, append_record

        clear_output_files(STAGING)
        if CHECKPOINT_FILE.exists():
            CHECKPOINT_FILE.unlink()

        call_count = 0
        real_append = append_record

        def fail_after_three(staging_dir, bucket, record):
            nonlocal call_count
            call_count += 1
            if call_count > 3:
                raise RuntimeError("simulated crash")
            real_append(staging_dir, bucket, record)

        try:
            with patch("pipeline.pipeline.append_record", fail_after_three):
                with pytest.raises(RuntimeError):
                    run(
                        "provided/product_matching_submissions.json",
                        "provided/product_catalog.json",
                    )

            assert CHECKPOINT_FILE.exists(), "checkpoint file should exist after crash"
            checkpoint = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
            assert 0 < len(checkpoint["processed_ids"]) < 12, "checkpoint should have partial progress"

            run(
                "provided/product_matching_submissions.json",
                "provided/product_catalog.json",
            )

            assert not CHECKPOINT_FILE.exists(), "checkpoint should be cleaned up after successful run"

            s = load_summary()
            assert s["total_submissions"] == 12
            assert s["matched"] + s["no_match"] + s["human_review"] + s["invalid"] + s["exact_duplicates"] + s["near_duplicates"] == 12

            all_ids = []
            for bucket in ("matched", "no_match", "human_review"):
                all_ids += [r["submission_id"] for r in load(f"{bucket}.json")]
            all_ids += [r["record"]["submission_id"] for r in load("invalid.json")]
            assert len(all_ids) == len(set(all_ids)), "each record should appear in exactly one output file"

        finally:
            run(
                "provided/product_matching_submissions.json",
                "provided/product_catalog.json",
            )
