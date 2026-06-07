# Product Matching Pipeline

A deterministic pipeline that ingests product submissions, validates them, deduplicates,
and matches them against a canonical catalog. Ambiguous records are routed to a human
review queue for LLM-assisted resolution in Tier 2.

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running

```bash
python run.py
```

By default this reads from `provided/product_matching_submissions.json` and
`provided/product_catalog.json`. You can pass custom paths as arguments:

```bash
python run.py path/to/submissions.json path/to/catalog.json
```

Output is written to `staging/`. Nothing in the catalog or source files is modified.

## Running Tests

```bash
pytest
```

---

## Data Flow

```
submissions.json
      │
      ▼
  [ ingest ]        stream_submissions() yields one record at a time
      │
      ▼
  [ validate ]      checks required fields, allowed sizes, vintage range
      │
      ├── invalid ──────────────────────────────► staging/invalid.json
      │
      ▼
  [ deduplicate ]   exact (same submission_id) and near (same content)
      │
      ├── exact_duplicates ─────────────────────► staging/exact_duplicates.json
      ├── near_duplicates ──────────────────────► staging/near_duplicates.json
      │
      ▼
  [ match ]         normalised string + alias lookup, then attribute gates
      │
      ├── exact_match ──────────────────────────► staging/matched.json
      ├── no_match ─────────────────────────────► staging/no_match.json
      └── human_review ─────────────────────────► staging/human_review.json
                                                   (input queue for Tier 2)
      │
      ▼
  [ report ]        writes summary.json and prints counts to terminal
```

---

## Approach and Tradeoffs

### Tier 1 — Deterministic Core (implemented)

The pipeline is split into five stages, each in its own module, with no stage
reaching into another's concerns. Validation never writes files. Matching never
reads from staging.

**Ingestion** reads submissions as a generator so the calling code processes one
record at a time. The current implementation uses `json.load()` internally, which
still loads the raw JSON into memory before iterating. For production at scale this
would be swapped for a true streaming parser (e.g. `ijson`) without changing any
downstream code — the generator interface is the important design decision.

**Validation** encodes the rules from `product_matching_rules.json` directly:
required non-empty string fields, an allowed set of sizes, and a vintage range of
1800 to the current year. Two deliberate decisions:

- *Vintage type coercion*: a vintage supplied as the string `"2005"` is rejected
  rather than silently coerced to an integer. The field contract says integer or
  null; accepting strings would hide upstream data quality problems.
- *Size matching*: exact match against the 11 allowed values. A submission with
  `size_ml: 755` is invalid, not rounded to 750.

**Deduplication** checks two things: exact duplicate `submission_id` within the
import file, and near-duplicates defined as records with the same normalised name,
producer, category, size, and vintage under a different ID. The first occurrence
wins; all subsequent matches are flagged and excluded from matching.

**Matching** uses two steps: name lookup and attribute gating.

*Name lookup* normalises both the submission name and every catalog alias
(lowercase, punctuation stripped, whitespace collapsed) and checks for exact
equality. This catches entries like `"Ch. Margaux 2005"` matching the alias
`"Ch. Margaux 2005"` without fuzzy logic.

*Attribute gating* then filters candidates by category, size, and vintage. If the
catalog product carries a vintage, the submission must supply a matching one. A
missing vintage on the submission causes the record to fall through to `no_match`
rather than guessing — consistent with the false-positive policy.

Decision rules:
- Exactly one candidate passes all gates → `exact_match`
- Multiple candidates pass all gates → `human_review` (genuinely ambiguous)
- Name matched but attribute gates failed → `no_match`
- No name match → `no_match`

**False positives vs false negatives**: the rules file states explicitly that false
positives are worse than false negatives. When in doubt the pipeline routes to
`no_match` rather than committing to a candidate. SUB-2009 ("Screaming Eagle
Cabernet") is a good example — the catalog entry is "Screaming Eagle Cabernet
Sauvignon" and no alias matches exactly, so it is left unmatched for Tier 2 rather
than risk a wrong commit.

### What Tier 1 leaves unmatched (for Tier 2)

| Submission | Reason |
|---|---|
| SUB-2003 | "Fifteen" vs "15", producer alias mismatch |
| SUB-2004 | Size mismatch (700ml submitted, 750ml in catalog) |
| SUB-2007 | Vintage missing — ambiguous between two catalog entries |
| SUB-2009 | Partial name, no exact alias in catalog |
| SUB-2010 | Size mismatch (750ml submitted, 700ml in catalog) |

---

## Staging Boundary

All output is written to `staging/`. The catalog file is opened read-only and
never modified. This satisfies the requirement to treat the catalog as production
data and keep a clear boundary between ingestion and persistence.

---

## Idempotency and Partial Failure

Running the pipeline twice on the same input produces identical output. Every
decision is deterministic given the same submissions and catalog.

Partial failure is handled via a checkpoint file written to `staging/checkpoint.json`.
After each record is successfully written, its `submission_id` is added to the
checkpoint along with the running counts. If the process dies mid-run, restarting
it will resume from where it left off rather than reprocessing from the beginning.

On resume the pipeline re-reads the input file from the top to rebuild in-memory
deduplication state, but skips writing output for any record already in the
checkpoint. The checkpoint is deleted on successful completion.

Output files are written in **JSON Lines** format (one JSON object per line) so
records can be appended incrementally without loading the whole file into memory.
The summary file remains standard JSON as it is a single object written at the end.

---

## Scaling Plan

At 1M records the following break:

- **In-memory list materialisation** in `pipeline.py` — the full validated and
  deduplicated record sets are held in RAM simultaneously. Fix: process and write
  one record at a time as it streams through, as described in the data flow above.
- **Deduplication dictionaries** — `seen_ids` and `seen_content` grow with the
  number of unique records. At scale these move to a database or a probabilistic
  structure (Bloom filter for a fast first-pass, database for authoritative check).
- **Catalog lookup** — iterating the full catalog for every submission is O(n×m).
  Fix: build a normalised alias index at startup (dict keyed by normalised alias)
  for O(1) lookups per submission.
- **Single-process throughput** — the pipeline is currently single-threaded. Fix:
  batch submissions and process in parallel workers, with each worker writing to
  its own staging partition.

---

## Failure Handling

| Scenario | Behaviour |
|---|---|
| Invalid record in input | Caught at validation, written to `invalid.json`, pipeline continues |
| Duplicate submission_id | Flagged, written to `exact_duplicates.json`, pipeline continues |
| Catalog file missing | `FileNotFoundError` raised at startup before any processing begins |
| Pipeline crash mid-run | Staging output may be incomplete; re-run from scratch is safe |
| LLM API unavailable (Tier 2) | Not yet implemented; design intent is records fall to `human_review.json` |

---

## Tier 2 — LLM-Assisted Matching (not yet implemented)

`staging/human_review.json` and `staging/no_match.json` are the input queues for
Tier 2. The LLM stage will consume these, attempt resolution, and emit structured
output with a confidence score and reason. Records below the confidence threshold
remain in the human review queue.

---

## Tier 3 — Scale, Cost, and Evaluation (written response)

*To be completed.*

---

## AI Usage — Authoring

*To be completed: one prompt iterated on, showing the first version, what was wrong,
and the revised version.*

---

## AI Usage — System Component

*To be completed: where in the pipeline the LLM runs, the output schema, the
fallback behaviour, and estimated cost per 1k records.*
