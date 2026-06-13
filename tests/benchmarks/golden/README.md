# Vendored golden comments (Martian Code Review Bench)

Raw, unmodified golden-comment files from the offline benchmark:
`github.com/withmartian/code-review-benchmark` →
`offline/golden_comments/`.

- **Pinned commit:** `279f279d6ef472a32f8055bae78b29ab8c40ece0`
- **Fetched:** 2026-06-12
- Vendored by `scripts/import_golden_comments.py`, which also converts these
  into `tests/benchmarks/ground_truth.json` (our harness schema).

The benchmark refreshes its PR set monthly to fight overfitting. We pin a
commit and vendor the files so re-importing is a deliberate act — run
`uv run python scripts/import_golden_comments.py --refresh` and bump
`PINNED_SHA` when you actually want the newer set, not silently.

Each file is a list of `{pr_title, url, original_url, comments: [{comment,
severity}]}`. `url` is the exact PR the bench scored against (a frozen
`ai-code-review-evaluation/*` mirror for some repos, the real upstream PR
for others). Severity is Low/Medium/High/Critical.
