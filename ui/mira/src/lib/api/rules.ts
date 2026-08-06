import { fetchJson, patchJson, postJson, putJson } from "./http"
import type {
  UnifiedRule,
  UnifiedRuleCreate,
  UnifiedRuleKind,
  UnifiedRuleRef,
} from "./types"

function unifiedListQs(params: {
  mode: "pending" | "active"
  kind?: string
  repo?: string
  enabled?: string
  q?: string
}) {
  const sp = new URLSearchParams()
  sp.set("mode", params.mode)
  if (params.kind) sp.set("kind", params.kind)
  if (params.repo) sp.set("repo", params.repo)
  if (params.enabled) sp.set("enabled", params.enabled)
  if (params.q) sp.set("q", params.q)
  return `?${sp.toString()}`
}

// Custom rules (global + per-repo) and learned rules — unified surface only.
export const rulesApi = {
  listUnifiedRules: (params: {
    mode: "pending" | "active"
    kind?: string
    repo?: string
    enabled?: string
    q?: string
  }) => fetchJson<UnifiedRule[]>(`/api/rules${unifiedListQs(params)}`),

  getUnifiedRule: (params: {
    kind: UnifiedRuleKind
    id: number
    owner?: string
    repo?: string
    platform?: string
  }) => {
    const sp = new URLSearchParams()
    sp.set("kind", params.kind)
    sp.set("id", String(params.id))
    if (params.owner) sp.set("owner", params.owner)
    if (params.repo) sp.set("repo", params.repo)
    if (params.platform) sp.set("platform", params.platform)
    return fetchJson<UnifiedRule>(`/api/rules/item?${sp.toString()}`)
  },

  createUnifiedRule: (body: UnifiedRuleCreate) =>
    postJson<UnifiedRule>("/api/rules", body),

  updateUnifiedRule: (
    body: import("./types").UnifiedRuleUpdate
  ) => putJson<UnifiedRule>("/api/rules", body),

  deleteUnifiedRule: (body: UnifiedRuleRef) =>
    postJson<{ ok: boolean }>("/api/rules/delete", body),

  approveUnifiedRule: (body: UnifiedRuleRef) =>
    postJson<{ ok: boolean }>("/api/rules/approve", body),

  rejectUnifiedRule: (body: UnifiedRuleRef) =>
    postJson<{ ok: boolean }>("/api/rules/reject", body),

  /** Delete auto-synth Pending learnings (keeps @remember). Optional owner/repo. */
  clearPendingLearnings: (repo?: string) => {
    const qs =
      repo && repo !== "__all__"
        ? `?repo=${encodeURIComponent(repo)}`
        : ""
    return postJson<{ cleared: number }>(`/api/rules/clear-pending${qs}`, {})
  },

  setUnifiedRuleEnabled: (body: UnifiedRuleRef & { enabled: boolean }) =>
    patchJson<UnifiedRule>("/api/rules/enabled", body),

  refreshLearnings: (body?: {
    repos?: { owner: string; repo: string }[]
    max_prs?: number
  }) =>
    postJson<{ status: string }>("/api/learnings/refresh", body ?? {}),

  synthesizeLearnings: (body?: {
    repos?: { owner: string; repo: string }[]
  }) =>
    postJson<{
      status: string
      repos?: number
      deterministic_rules?: number
      llm_rules?: number
    }>("/api/learnings/synthesize", body ?? {}),

  synthesizeLearningsRepo: (owner: string, repo: string) =>
    postJson<{
      status: string
      deterministic_rules?: number
      llm_rules?: number
    }>(`/api/learnings/${owner}/${repo}/synthesize`, {}),

  getLearningsBackfillStatus: () =>
    fetchJson<
      {
        owner: string
        repo: string
        status?: string
        phase?: string
        job?: string
        error?: string
        prs_done?: number
        total?: number
        max_prs?: number
        skipped?: number
        human_recorded?: number
        deterministic_rules?: number
        llm_rules?: number
        extract_done?: number
        extract_total?: number
        updated_at?: number
      }[]
    >("/api/learnings/backfill/status"),

  getLearningsBackfillEstimate: (params: {
    repos: { owner: string; repo: string }[]
    max_prs?: number
  }) => {
    const sp = new URLSearchParams()
    if (params.max_prs != null) sp.set("max_prs", String(params.max_prs))
    for (const r of params.repos) {
      sp.append("repo", `${r.owner}/${r.repo}`)
    }
    if (params.repos.length === 0) sp.set("repos", "0")
    return fetchJson<{
      estimated_usd: number
      input_tokens: number
      output_tokens: number
      repo_count: number
      synth_calls: number
      skipped_repos: number
      max_prs: number
      model: string
      basis: string
    }>(`/api/learnings/backfill/estimate?${sp.toString()}`)
  },
}
