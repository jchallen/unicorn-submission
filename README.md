# Product Matching Pipeline

A two-tier pipeline that ingests product submissions, validates them, deduplicates,
and matches them against a canonical catalog. Tier 1 uses deterministic rules; Tier 2
uses Claude (claude-opus-4-8) to resolve the records Tier 1 could not confidently match.

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running

**Tier 1 — deterministic matching:**

```bash
python run.py
```

**Tier 2 — LLM-assisted matching** (run after Tier 1):

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
python run_tier2.py
```

Both scripts read from `provided/` and write output to `staging/`. You can pass custom
paths as positional arguments:

```bash
python run.py path/to/submissions.json path/to/catalog.json
python run_tier2.py path/to/submissions.json path/to/catalog.json
```

## Running Tests

```bash
pytest
```

## Generating Eval Report

```bash
python eval.py
```

Reads current staging output and writes `staging/product_matching_eval.json`.

---

## Deliverable 1 — Approach & Tradeoffs

### Tier 1 — Deterministic Core

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

### What Tier 1 leaves unmatched (Tier 2 input)

| Submission | Reason left unmatched by Tier 1 |
|---|---|
| SUB-2003 | "Fifteen" vs "15" — no alias match |
| SUB-2004 | Size mismatch (700ml submitted, 750ml in catalog) |
| SUB-2005 | Not in catalog at all |
| SUB-2007 | Vintage missing — ambiguous between two catalog entries |
| SUB-2008 | Too generic — "Reserve Red Wine" matches no alias |
| SUB-2009 | Partial name — "Screaming Eagle Cabernet" vs full canonical name |
| SUB-2010 | Size mismatch (750ml submitted, 700ml in catalog) |

### Tier 2 — LLM-Assisted Matching

Tier 2 reads `staging/no_match.json` and `staging/human_review.json` and attempts
to resolve each record using Claude.

**Model and structured output**: `claude-opus-4-8` with tool use. A single tool,
`record_match_decision`, defines the exact output schema the model must return:
`product_id` (catalog ID or null), `confidence` (0–1), and `reason`. Using
`tool_choice: {type: "tool", name: "..."}` forces exactly one call per request,
giving a guaranteed structured response with no post-processing.

**Pre-filtering**: before building the prompt, the catalog is filtered to only the
entries in the same category as the submission. This reduces prompt length and
focuses the model on the relevant candidates.

**Confidence threshold**: 0.85. This is deliberately high, consistent with the
false-positive policy. Records below the threshold are routed to
`llm_review_queue.json` rather than committed. Routing:
- `confidence >= 0.85` and a product ID found → `llm_matched.json`
- `confidence >= 0.85` and no product ID → `llm_no_match.json` (LLM is sure it's absent)
- `confidence < 0.85` → `llm_review_queue.json`
- API error → `llm_review_queue.json`

**Idempotency via SHA256 cache**: every call is keyed by a SHA256 hash of the
submission fields plus the candidate list serialised with sorted keys. The result is
stored in `staging/llm_cache.json`. Re-running Tier 2 on the same data makes zero
additional API calls.

---

## Deliverable 2 — Data Flow Diagram

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
      │
      ▼
  [ report ]        writes summary.json and prints counts to terminal


  staging/no_match.json ──────┐
                              ▼
  staging/human_review.json ──► [ tier2 / llm_match ]
                                        │
                    ┌───────────────────┼───────────────────┐
                    ▼                   ▼                   ▼
          llm_matched.json    llm_no_match.json   llm_review_queue.json
```

---

## Deliverable 3 — Scaling Plan

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
- **LLM throughput** — Tier 2 makes one sequential API call per record. At scale,
  use the Anthropic Batch API (`/v1/messages/batches`) to submit all records in one
  request; it processes them asynchronously at 50% cost and removes per-request
  latency from the critical path.

---

## Deliverable 4 — Failure Handling

| Scenario | Behaviour |
|---|---|
| Invalid record in input | Caught at validation, written to `invalid.json`, pipeline continues |
| Duplicate submission_id | Flagged, written to `exact_duplicates.json`, pipeline continues |
| Catalog file missing | `FileNotFoundError` raised at startup before any processing begins |
| Pipeline crash mid-run | Checkpoint preserves progress; re-run resumes from last saved record |
| LLM API unavailable | Caught as `anthropic.APIError`, logged to `llm_audit.jsonl`, record routed to `llm_review_queue.json`, pipeline continues |

**Staging boundary**: all output is written to `staging/`. The catalog is opened
read-only and never modified.

**Idempotency**: running either pipeline script twice on the same input produces
identical output. Tier 1 uses a checkpoint file (`staging/checkpoint.json`) that
records each processed `submission_id`; on restart it skips any already-written
record. Tier 2 uses the SHA256 content-addressed cache (`staging/llm_cache.json`);
re-running makes zero additional API calls for records already in the cache.

**Output format**: all bucket files are written in JSON Lines format (one object
per line) so records can be appended incrementally without loading the whole file
into memory.

---

## Deliverable 5 — AI Usage: Authoring

This pipeline was built with Claude Code as an interactive coding assistant. The
testing approach went through three iterations.

**First version:** No tests at all. The pipeline ran and produced output, but there
was no automated way to verify correctness after changes.

**What was wrong:** Any edit to matching logic, validation rules, or output format
could silently break expected behaviour with no safety net.

**Second version:** Integration tests using pytest (`tests/test_pipeline.py`). A
session-scoped fixture runs the full pipeline once against the provided data, then
individual test classes assert on counts, specific decisions, required fields, and
summary totals. This covered the golden path — correct inputs producing correct
outputs.

**What was still missing:** The pipeline had a stated constraint that partial failure
must be safe to resume. The integration tests passed on a clean run but said nothing
about crash recovery. When the checkpoint system was implemented, there was no test
to verify it actually worked.

**Third version (what ships):** `TestCheckpointResume` was added. It patches
`append_record` to raise a `RuntimeError` after three writes, verifies the checkpoint
file exists with partial progress, then re-runs the pipeline and asserts that the
final output is complete with no record appearing twice. This is the test that
actually exercises the constraint the code was written to satisfy.

---

## Deliverable 6 — AI Usage: System Component

**Where it runs:** `pipeline/llm_match.py`, called once per record that Tier 1
could not deterministically resolve. Typically 50–60% of submissions reach Tier 2.

**Model:** `claude-opus-4-8` with adaptive thinking (`thinking: {type: "adaptive"}`)
and `output_config: {effort: "high"}`.

**Output schema** (enforced via tool use):

```json
{
  "product_id": "PROD-1005" | null,
  "confidence": 0.0–1.0,
  "reason": "one sentence"
}
```

**Audit log**: every request (cache hit or miss, success or error) appends one JSON
line to `staging/llm_audit.jsonl` with `submission_id`, `model`, `input_tokens`,
`output_tokens`, `product_id`, `confidence`, and `run_at`. This records what
decision was made, when, and at what cost. The full prompt text is not stored in
the log — it can be reconstructed deterministically from the original submission
and catalog using `_build_prompt()` in `llm_match.py`. For a fully self-contained
audit trail that requires no external files, the prompt text should also be written
to the log entry.

**Fallback behaviour:** any `anthropic.APIError` is caught without re-raising.
The record is written to `llm_review_queue.json` and processing continues. The
error is logged to `llm_audit.jsonl` with a full error message for diagnosis.

**Estimated cost per 1,000 submissions** (first run, no cache):

Assuming ~580 records reach the LLM (58% pass-through rate from Tier 1):
- Input tokens per call: ~500 (prompt + filtered candidates)
- Thinking + output tokens per call: ~350
- 580 × 500 input = 290,000 tokens × $5.00/1M = **$1.45**
- 580 × 350 output = 203,000 tokens × $25.00/1M = **$5.08**
- **Total ≈ $6.50 per 1,000 submissions**

Re-runs against the same data cost ~$0 due to the SHA256 cache. At scale, switching
to the Batch API halves the per-token cost to approximately **$3.25 per 1,000 submissions**.

---

## Deliverable 7 — One Thing the LLM Got Wrong

The Tier 1 constraint requires that a mid-run crash leaves partial progress safe to
resume. When asked to implement this, the coding assistant (Claude Code) produced a
pipeline that loaded all records into memory and wrote all output at the end — then
noted in the README that "for production at scale, a streaming approach would be
preferable" and marked the partial-failure case as a future improvement.

This was wrong in a specific way: it correctly identified the gap but treated
writing about it as a substitute for fixing it. The requirement was stated
explicitly ("import may fail midway — partial progress should be safe"), not
implied, and acknowledgement in a README does not satisfy a stated constraint.

The error was caught by reading the requirement back against the implementation and
noticing the mismatch. The fix was to implement the checkpoint system: after each
record is written, its `submission_id` is appended to `staging/checkpoint.json`.
On restart the pipeline re-reads the input from the top (to rebuild deduplication
state) but skips writing output for any ID already in the checkpoint. The checkpoint
is deleted on clean completion. The test in `tests/test_pipeline.py`
(`TestCheckpointResume`) simulates a crash mid-run and verifies that a restart
produces a complete, deduplicated output with no record appearing twice.

The lesson: when a model flags something as a "potential improvement" rather than
implementing it, check whether it is actually optional or whether it is a
requirement being quietly deferred.

---

## Deliverable 8 — Eval Results

Run `python eval.py` after `run.py` (and optionally `run_tier2.py`) to generate
`staging/product_matching_eval.json` and print a summary. Results against the 12
product-matching submissions, Tier 1 only:

| Metric | Result |
|---|---|
| Product ID accuracy | 10 / 12 — 83.3% |
| Decision accuracy | 12 / 12 — 100.0% |
| False positives | 0 |
| FP-weighted score | 100.0% |

**Product ID accuracy** counts a submission correct if the pipeline returned the
right `product_id` (or null for a confirmed no-match). The two misses are
SUB-2003 ("Pappy Van Winkle Fifteen" → expected PROD-1005) and SUB-2009
("Screaming Eagle Cabernet" → expected PROD-1008). Both were correctly deferred
to Tier 2 rather than guessed wrong — the product_id is not yet resolved, not
incorrectly resolved.

**Decision accuracy** counts a submission correct if the pipeline's decision
(e.g. `exact_match`, `no_match`, `invalid`) was in the set of acceptable decisions
for that record. 12/12 because every Tier 1 decision was appropriate: the two
deferred records have `no_match` in their acceptable set.

**False positives: 0.** No record was confidently matched to the wrong product.
The false-positive policy (route to no_match when uncertain) held in every case.

**FP-weighted score** penalises false positives double: `(acceptable - FP_count) /
(total + FP_count)`. With zero false positives the score equals decision accuracy.

After Tier 2 runs, SUB-2003 and SUB-2009 are expected to resolve to PROD-1005 and
PROD-1008 respectively, bringing product ID accuracy to 12/12 if both match
at high confidence.

---

## Tier 3 — Scale, Cost, and Evaluation (written response)

### 1. Cost — $50 budget for 1M records

At current rates (~$6.50 per 1,000 submissions, 58% pass-through to Tier 2), 1M records
would cost ~$3,770 — 75× over budget. The fix is a cascade of progressively cheaper
signals that eliminate obvious cases before they reach the LLM:

1. **Rules (free)** — extend Tier 1 with edit-distance gating. If the submission name is
   edit-distance > N from every catalog entry, it is a guaranteed no-match. Skip the LLM.
2. **N-gram similarity (free)** — split names into overlapping character windows and compute
   overlap scores. "Pappy Van Winkle Fifteen" and "Pappy Van Winkle 15" share most n-grams
   even though the words differ. Records below a similarity floor are definite no-matches;
   records above a ceiling are near-certain matches. Only the middle band reaches the LLM.
3. **Embeddings (cheap)** — generate vector embeddings for every catalog entry at startup
   (one-time cost). For each submission, embed the name and compute cosine similarity against
   the catalog. If the highest similarity score is below a threshold, the product is not in
   the catalog — skip the LLM. Embedding costs ~$0.02/1M tokens vs $5/1M for LLM input.
4. **LLM via Batch API (expensive, last resort)** — only genuinely ambiguous records reach
   this stage. The Anthropic Batch API processes requests asynchronously at 50% of the
   standard per-token cost.

With this cascade, LLM-bound records drop from ~58% to ~1-2%. At 1% = 10,000 LLM calls
via the Batch API: ~$32.50 total. Under the $50 budget.

---

### 2. Evaluation — accuracy and regression catching

Current accuracy (Tier 1 only, 12 submissions):
- Product ID accuracy: 10/12 — 83.3%
- Decision accuracy: 12/12 — 100%
- False positives: 0

The two product ID misses (SUB-2003, SUB-2009) are correct deferrals to Tier 2, not wrong
answers. Full results are in `staging/product_matching_eval.json` (run `python eval.py`).

**Catching regressions after a prompt change:** Monitoring the human review queue size is
a useful signal — if the LLM degrades in confidence, more records pile up in
`llm_review_queue.json`. However this catches only one failure direction. An overconfident
LLM making false positives would cause the review queue to shrink, which looks like an
improvement. That is the worse failure.

The complement: maintain a small golden test set of ~50 hand-labeled records and run it
before any prompt change. Compare outputs to the known-good baseline. This is snapshot
testing for the LLM and catches both degradation and overconfidence. The prompt lives in
version control, so the diff between any two versions is always visible.

---

### 3. Drift — detecting the LLM getting worse over time

The primary signal is bucket distribution over time: graph the counts of `llm_matched`,
`llm_no_match`, and `llm_review_queue` across imports. A stable pipeline on stable data
produces stable ratios. A significant shift — more records going to review, or a sudden
spike in matched records — warrants investigation.

Two additions improve this:

- **Confidence score distribution** — track the distribution of confidence scores, not just
  bucket counts. A shift from an average match confidence of 0.93 down to 0.87 is an early
  warning sign before bucket counts visibly change.
- **Cache-bypassing probe set** — the SHA256 cache means re-running the same data always
  produces the same decisions, masking any model change. To isolate model drift from data
  drift, maintain a small fixed probe set that always bypasses the cache. Run it periodically
  and compare decisions to a known baseline. If the probe set shifts but live data does not,
  it is model drift. If live data shifts but the probe set does not, it is data drift.

---

### 4. Reversibility — rolling back a bad import

The SHA256 cache in `staging/llm_cache.json` records every decision keyed by content hash,
which makes it possible to identify exactly which records got bad decisions via
`staging/llm_audit.jsonl`. However the cache cannot be used to replay back to a good state:
if the prompt changed and many records got wrong decisions, those wrong decisions are cached
under the original content hashes. Re-running without clearing the cache would replay the
same bad decisions.

The correct rollback procedure:
1. **Identify** — use `llm_audit.jsonl` to find affected records and the timestamp of the
   bad run.
2. **Delete** — remove the bad Tier 2 output files (`llm_matched.json`, `llm_no_match.json`,
   `llm_review_queue.json`).
3. **Clear** — delete or selectively evict the affected entries from `llm_cache.json`.
4. **Fix** — correct the prompt in `pipeline/llm_match.py`.
5. **Re-run** — `python run_tier2.py`. The pipeline is idempotent and the Tier 1 output is
   unchanged.

The key safety property is that `staging/` is the boundary. Bad LLM decisions never reach
the production catalog. At scale, versioning staging output by import run ID means a bad run
can simply be identified and not promoted, with no destructive deletion required.
