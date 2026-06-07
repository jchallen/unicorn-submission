import sys
from pipeline.pipeline import run

SUBMISSIONS = "provided/product_matching_submissions.json"
CATALOG = "provided/product_catalog.json"

if __name__ == "__main__":
    submissions = sys.argv[1] if len(sys.argv) > 1 else SUBMISSIONS
    catalog = sys.argv[2] if len(sys.argv) > 2 else CATALOG
    run(submissions, catalog)
