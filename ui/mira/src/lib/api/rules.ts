import { deleteJson, fetchJson, patchJson, postJson, putJson } from "./http"
import type {
  LearnedRuleModel,
  OrgLearnedRuleModel,
  RuleModel,
  UnifiedRule,
  UnifiedRuleCreate,
  UnifiedRuleKind,
  UnifiedRuleRef,
} from "./types"

function platformQs(platform?: string) {
  return platform ? `?platform=${encodeURIComponent(platform)}` : ""
}

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

// Custom rules (global + per-repo) and learned rules.
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

  setUnifiedRuleEnabled: (body: UnifiedRuleRef & { enabled: boolean }) =>
    patchJson<UnifiedRule>("/api/rules/enabled", body),

  // Learned rules. status: "approved" | "pending" | "rejected" | "" (all)
  listLearnedRules: (status = "") =>
    fetchJson<OrgLearnedRuleModel[]>(
      status ? `/api/learned-rules?status=${encodeURIComponent(status)}` : `/api/learned-rules`
    ),

  getLearnedRule: (owner: string, repo: string, id: number, platform?: string) =>
    fetchJson<OrgLearnedRuleModel>(
      `/api/learned-rules/${owner}/${repo}/${id}${platformQs(platform)}`
    ),

  approveLearnedRule: (owner: string, repo: string, id: number, platform?: string) =>
    postJson<{ ok: boolean }>(
      `/api/learned-rules/${owner}/${repo}/${id}/approve${platformQs(platform)}`,
      {}
    ),

  rejectLearnedRule: (owner: string, repo: string, id: number, platform?: string) =>
    postJson<{ ok: boolean }>(
      `/api/learned-rules/${owner}/${repo}/${id}/reject${platformQs(platform)}`,
      {}
    ),

  setLearnedRuleActive: (
    owner: string,
    repo: string,
    id: number,
    active: boolean,
    platform?: string
  ) =>
    patchJson<{ ok: boolean }>(
      `/api/learned-rules/${owner}/${repo}/${id}/active${platformQs(platform)}`,
      { active }
    ),

  createLearnedRule: (
    owner: string,
    repo: string,
    body: { rule_text: string; category: string; path_pattern?: string },
    platform?: string
  ) =>
    postJson<LearnedRuleModel>(
      `/api/learned-rules/${owner}/${repo}${platformQs(platform)}`,
      body
    ),

  updateLearnedRule: (
    owner: string,
    repo: string,
    id: number,
    body: { rule_text: string; category: string; path_pattern?: string },
    platform?: string
  ) =>
    putJson<{ ok: boolean }>(
      `/api/learned-rules/${owner}/${repo}/${id}${platformQs(platform)}`,
      body
    ),

  deleteLearnedRule: (owner: string, repo: string, id: number, platform?: string) =>
    deleteJson(`/api/learned-rules/${owner}/${repo}/${id}${platformQs(platform)}`),

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
        accepted?: number
        human_recorded?: number
        deterministic_rules?: number
        llm_rules?: number
        upserted?: number
        classify_done?: number
        classify_total?: number
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

  // Global rules
  listGlobalRules: () => fetchJson<RuleModel[]>("/api/rules/global"),

  createGlobalRule: (title: string, content: string) =>
    postJson<RuleModel>("/api/rules/global", { title, content }),

  updateGlobalRule: (id: number, title: string, content: string) =>
    putJson<RuleModel>(`/api/rules/global/${id}`, { title, content }),

  deleteGlobalRule: (id: number) => deleteJson(`/api/rules/global/${id}`),

  toggleGlobalRule: (id: number) =>
    patchJson<RuleModel>(`/api/rules/global/${id}/toggle`),

  // Per-repo rules
  listRepoRules: (owner: string, repo: string) =>
    fetchJson<RuleModel[]>(`/api/repos/${owner}/${repo}/rules`),

  createRepoRule: (
    owner: string,
    repo: string,
    title: string,
    content: string
  ) =>
    postJson<RuleModel>(`/api/repos/${owner}/${repo}/rules`, {
      title,
      content,
    }),

  updateRepoRule: (
    owner: string,
    repo: string,
    id: number,
    title: string,
    content: string
  ) =>
    putJson<RuleModel>(`/api/repos/${owner}/${repo}/rules/${id}`, {
      title,
      content,
    }),

  deleteRepoRule: (owner: string, repo: string, id: number) =>
    deleteJson(`/api/repos/${owner}/${repo}/rules/${id}`),
}
