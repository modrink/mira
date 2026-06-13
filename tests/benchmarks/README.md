# F1 benchmark harness

Scores Mira end-to-end against labeled PR fixtures with an LLM judge,
reporting precision/recall/F1 overall and per language. This replaces
substring matching (`tests/eval_regressions/`, kept as a cheap canary)
for quality A/B work.

## Running

```bash
# Full scorecard via pytest
OPENROUTER_API_KEY=... GITHUB_TOKEN=... uv run pytest -m benchmark -v

# A/B work: N runs with mean/σ
uv run python scripts/run_benchmark.py --runs 3 --label my-experiment
```

Each run writes a diffable artifact to `tests/benchmarks/runs/` with
per-PR TP/FP/FN, the full comment dump, judge reasons, config snapshot,
tokens and durations.

## The variance rule

LLM reviews are stochastic. Before trusting any A/B, establish the noise
floor: run the unchanged baseline N=5 (`--runs 5 --label baseline`) and
note σ. A change is accepted only if its mean-of-3 F1 beats baseline by
more than 2σ. If σ exceeds ~2 F1 points, add fixtures before concluding
anything.

## Ground truth (`ground_truth.json`)

The 50 offline-bench PRs (10 each: sentry/grafana/keycloak/discourse/cal.com,
136 findings) imported from Martian's published golden comments — see
`golden/README.md`. Regenerate with:

```bash
uv run python scripts/import_golden_comments.py            # vendored copies
uv run python scripts/import_golden_comments.py --refresh  # refetch + repin
GITHUB_TOKEN=... uv run python scripts/import_golden_comments.py  # also pin head_sha
```

Schema, one entry per PR; one finding per golden comment:

```json
{
  "pr_url": "...", "head_sha": "<pinned or null>", "language": "go",
  "source": "martian-bench", "pr_title": "...",
  "findings": [{
    "id": "grafana-79265-1", "path": null, "line_start": null, "line_end": null,
    "description": "The golden comment text — the root cause the judge matches on.",
    "category": "bug", "severity": "blocker"
  }]
}
```

- `id` is `<repo>-<pr#>-<idx>` and stable across re-imports of the same SHA.
- `description` is the verbatim golden comment; severity maps
  Critical/High→blocker, Medium→warning, Low→suggestion.
- `path`/lines are null (the golden text rarely pins a location); the judge
  matches on root cause and file-agreement only when a path is present.
- `head_sha` is null unless imported with a `GITHUB_TOKEN` (50 API calls).

## Fine-tuning data flywheel (deferred)

The path to the F1 50+ tier (where Cubic sits) is fine-tuning on curated
review data, not more architecture. This harness is also that dataset's
labeling engine: every judged run yields (diff-context, comment,
TP/FP verdict) triples, and production adds more — comments humans
resolved-as-fixed are positives, dismissed/argued-down comments are hard
negatives. At ~500-1000 verified triples there's enough to train either
a critic-reranker that replaces the rule-based noise filter + critique
stack, or a DPO-style tune of the review model on accepted-vs-rejected
pairs. Until then: accumulate triples, don't train.
