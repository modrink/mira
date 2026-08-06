/** Shared learnings job status shapes (backfill + rebuild/synth). */

export type LearningJobStatus = {
  owner: string
  repo: string
  status?: string
  phase?: string
  job?: string
  error?: string
  prs_done?: number
  prs?: number
  total?: number
  max_prs?: number
  skipped?: number
  updated_at?: number
  extract_done?: number
  extract_total?: number
  llm_rules?: number
  deterministic_rules?: number
}

export function repoKey(owner: string, repo: string) {
  return `${owner}/${repo}`
}

export function newPrCount(st: LearningJobStatus): number {
  const done = st.prs_done ?? st.prs ?? 0
  const skipped = st.skipped ?? 0
  return Math.max(0, done - skipped)
}

export function isActiveLearningJob(st: LearningJobStatus): boolean {
  return st.status === "running" || st.status === "queued"
}

export function isSynthJob(st: LearningJobStatus): boolean {
  return st.job === "synth"
}

export function synthPhaseLine(st: LearningJobStatus): string {
  const phase = st.phase || ""
  if (phase === "extract") {
    const done = st.extract_done ?? 0
    const total = st.extract_total ?? 0
    return total > 0 ? `Extracting… ${done}/${total}` : "Extracting…"
  }
  if (phase === "cluster") {
    return "Clustering…"
  }
  if (phase === "complete") {
    const rules = st.llm_rules
    return rules != null ? `Complete · ${rules} rules` : "Complete"
  }
  if (phase === "synth" || phase === "queued") {
    return phase === "queued" ? "Queued" : "Synthesizing…"
  }
  return phase ? `${phase}…` : "Synthesizing…"
}

export function learningJobStatusLine(st: LearningJobStatus): string {
  const done = st.prs_done ?? st.prs ?? 0
  const skipped = st.skipped ?? 0
  const neu = newPrCount(st)
  const maxHint = st.max_prs ? `max ${st.max_prs}` : ""
  const phase = st.phase || ""
  const synth = isSynthJob(st)

  if (st.status === "queued") {
    if (synth) return "Queued · rebuild"
    return maxHint ? `Queued · ${maxHint}` : "Queued"
  }
  if (st.status === "running") {
    if (synth || phase === "extract" || phase === "cluster") {
      return synthPhaseLine(st)
    }
    if (phase === "listing") {
      return maxHint
        ? `Listing merged PRs (up to ${st.max_prs})…`
        : "Listing merged PRs…"
    }
    if (phase === "synth") {
      return `Synthesizing… · ${done} PRs ingested`
    }
    const denom =
      st.total && st.total > 0 ? String(st.total) : st.max_prs ? `≤${st.max_prs}` : "?"
    return `Backfilling… ${done}/${denom} · ${neu} new · ${skipped} skipped`
  }
  if (st.status === "complete") {
    if (synth) {
      const rules = st.llm_rules ?? 0
      return `Rebuild complete · ${rules} rules`
    }
    return `Complete · ${done} PRs · ${neu} new · ${skipped} skipped`
  }
  if (st.status === "failed") {
    return `Failed: ${st.error || "error"}`
  }
  return st.status ?? ""
}

export function pickPrimaryJob(
  statuses: LearningJobStatus[],
): LearningJobStatus | null {
  if (!statuses.length) return null
  const running = statuses.find((s) => s.status === "running")
  if (running) return running
  const queued = statuses.find((s) => s.status === "queued")
  if (queued) return queued
  return [...statuses].sort(
    (a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0),
  )[0]
}

export function jobKindLabel(st: LearningJobStatus | null): "rebuild" | "backfill" {
  return st && isSynthJob(st) ? "rebuild" : "backfill"
}
