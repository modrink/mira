import {
  Check,
  History,
  Inbox,
  Library,
  Pencil,
  Plus,
  Power,
  RefreshCw,
  Search,
  X,
} from "lucide-react"
import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react"
import { useNavigate, useSearchParams } from "react-router"

import { LearningBackfillDialog } from "@/components/dashboard/learning-backfill-dialog"
import { LearningBackfillStrip } from "@/components/dashboard/learning-backfill-strip"
import { LearningJobProgressDialog } from "@/components/dashboard/learning-job-progress-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { ConfirmButton } from "@/components/ui/confirm-button"
import { DataTable, DataTablePagination } from "@/components/ui/data-table"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { toast } from "@/components/ui/sonner"
import { type Column, useDataTable } from "@/components/ui/use-data-table"
import { api, type UnifiedRule, type UnifiedRuleKind } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { useAsync, useDocumentTitle } from "@/lib/hooks"
import { cn } from "@/lib/utils"

const ALL = "__all__"

function ruleKey(r: UnifiedRule) {
  return `${r.kind}:${r.platform}:${r.owner}/${r.repo}#${r.id}`
}

function kindLabel(kind: UnifiedRuleKind) {
  if (kind === "written_global") return "Global"
  if (kind === "written_repo") return "Per-repo"
  return "Learned"
}

function scopeLabel(r: UnifiedRule) {
  if (r.kind === "written_global") return "All repos"
  const repos = r.repos?.filter(Boolean) ?? []
  if (repos.length > 1) return `${repos.length} repos`
  if (repos.length === 1) return repos[0]
  return `${r.owner}/${r.repo}`
}

function formatPath(path: string) {
  if (!path) return ""
  // scope::__human_hash__ → show scope only
  const scoped = path.split("::")
  if (scoped.length === 2 && /^__[\w]+__$/.test(scoped[1])) {
    return scoped[0]
  }
  if (/^__[\w]+__$/.test(path)) return ""
  return path
}

function evidencePrList(rule: UnifiedRule): string[] {
  return (rule.evidence_prs || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
}

function formatDate(ts: number) {
  if (!ts) return "—"
  return new Date(ts * 1000).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  })
}

function StatusBadge({ rule }: { rule: UnifiedRule }) {
  if (rule.status === "pending") {
    return (
      <Badge
        variant="outline"
        className="border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400"
      >
        Pending
      </Badge>
    )
  }
  return rule.enabled ? (
    <Badge
      variant="outline"
      className="border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
    >
      Enabled
    </Badge>
  ) : (
    <Badge variant="outline" className="text-muted-foreground">
      Disabled
    </Badge>
  )
}

function evidenceHost(platform: string): string | null {
  const p = (platform || "github").toLowerCase()
  if (p === "github") return "github.com"
  if (p === "gitlab") return "gitlab.com"
  return null
}

function EvidenceLinks({ rule }: { rule: UnifiedRule }) {
  const nums = evidencePrList(rule)
  if (!nums.length) return <span>—</span>
  // Old synth stamped the whole comment window onto every rule — hide that junk.
  if (nums.length > 8) {
    return (
      <span className="text-sm text-muted-foreground">
        Stale list — Rebuild learnings to refresh
      </span>
    )
  }
  const host = evidenceHost(rule.platform)
  if (!host) {
    return <span className="text-sm">{nums.map((n) => `#${n}`).join(", ")}</span>
  }
  const path =
    (rule.platform || "github").toLowerCase() === "gitlab"
      ? `/-/merge_requests/`
      : `/pull/`
  return (
    <span className="flex flex-wrap gap-x-2 gap-y-1">
      {nums.map((n) => (
        <a
          key={n}
          href={`https://${host}/${rule.owner}/${rule.repo}${path}${n}`}
          target="_blank"
          rel="noreferrer"
          className="text-sm text-primary underline-offset-2 hover:underline"
        >
          #{n}
        </a>
      ))}
    </span>
  )
}

function Meta({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-1 text-sm font-medium">{value}</dd>
    </div>
  )
}

export function RulesPage() {
  useDocumentTitle("Rules")
  const { user } = useAuth()
  const isAdmin = !!user?.is_admin
  const username = user?.username ?? ""
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()

  const mode: "pending" | "active" =
    params.get("mode") === "pending" || params.get("mode") === "inbox"
      ? "pending"
      : "active"
  const setMode = (m: "pending" | "active") => {
    const next = new URLSearchParams(params)
    if (m === "pending") {
      next.set("mode", "pending")
      next.delete("kind")
      setKindFilter(ALL)
    } else {
      next.delete("mode")
    }
    setParams(next)
    setPanelOpen(false)
  }

  const [refreshKey, setRefreshKey] = useState(0)
  const refresh = () => setRefreshKey((k) => k + 1)
  const [query, setQuery] = useState("")
  const [repoFilter, setRepoFilter] = useState(ALL)
  const [kindFilter, setKindFilter] = useState(() => {
    if (params.get("mode") === "pending" || params.get("mode") === "inbox") return ALL
    return params.get("kind") || ALL
  })
  const setKindFilterAndUrl = (v: string) => {
    setKindFilter(v)
    const next = new URLSearchParams(params)
    if (v === ALL) next.delete("kind")
    else next.set("kind", v)
    setParams(next, { replace: true })
  }
  const [enabledFilter, setEnabledFilter] = useState<"all" | "enabled" | "disabled">(
    "all",
  )
  const [scanOpen, setScanOpen] = useState(false)
  const [progressOpen, setProgressOpen] = useState(false)
  const [rebuildBusy, setRebuildBusy] = useState(false)
  const [learningsJobActive, setLearningsJobActive] = useState(false)
  const [selected, setSelected] = useState<UnifiedRule | null>(null)
  const [panelOpen, setPanelOpen] = useState(false)
  const rebuildLocked = rebuildBusy || learningsJobActive
  const onLearningsActiveChange = useCallback((active: boolean) => {
    setLearningsJobActive(active)
    if (active) setRebuildBusy(false)
  }, [])

  const listParams = useMemo(
    () => ({
      mode,
      kind:
        mode === "active" && kindFilter !== ALL ? kindFilter : undefined,
      repo: repoFilter !== ALL ? repoFilter : undefined,
      enabled:
        mode === "active" && enabledFilter !== "all" ? enabledFilter : undefined,
      q: query.trim() || undefined,
    }),
    [mode, kindFilter, repoFilter, enabledFilter, query],
  )

  const { data: rows, loading, error } = useAsync(
    () => api.listUnifiedRules(listParams),
    [
      refreshKey,
      listParams.mode,
      listParams.kind,
      listParams.repo,
      listParams.enabled,
      listParams.q,
    ],
  )

  const { data: pendingProbe } = useAsync(
    () => api.listUnifiedRules({ mode: "pending" }),
    [refreshKey],
  )
  const pendingCount = (pendingProbe ?? []).length

  const { data: repos } = useAsync(api.listRepos, [])

  useEffect(() => {
    if (!selected || !rows) return
    const fresh = rows.find((r) => ruleKey(r) === ruleKey(selected))
    if (fresh) setSelected(fresh)
  }, [rows]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!panelOpen) return
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setPanelOpen(false)
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [panelOpen])

  const openDetail = (r: UnifiedRule) => {
    setSelected(r)
    setPanelOpen(true)
  }

  const editHref = (r: UnifiedRule) => {
    const sp = new URLSearchParams()
    sp.set("kind", r.kind)
    sp.set("id", String(r.id))
    if (r.owner) sp.set("owner", r.owner)
    if (r.repo) sp.set("repo", r.repo)
    if (r.platform) sp.set("platform", r.platform)
    return `/rules/edit?${sp.toString()}`
  }

  const canEdit = (r: UnifiedRule) => {
    if (r.kind !== "learned") return true
    return (
      isAdmin || (!!username && r.created_by === username && r.status === "pending")
    )
  }

  const act = (fn: () => Promise<unknown>, successMsg?: string) =>
    fn()
      .then(() => {
        refresh()
        if (successMsg) toast.success(successMsg)
      })
      .catch((e) =>
        toast.error("Action failed", {
          description: e instanceof Error ? e.message : String(e),
        }),
      )

  const refOf = (r: UnifiedRule) => ({
    kind: r.kind,
    id: r.id,
    owner: r.owner,
    repo: r.repo,
    platform: r.platform,
  })

  const approveSel = () => {
    if (!selected || selected.kind !== "learned") return
    const list = rows ?? []
    const idx = list.findIndex((r) => ruleKey(r) === ruleKey(selected))
    const remaining = list.filter((r) => ruleKey(r) !== ruleKey(selected))
    const next = remaining.length
      ? remaining[Math.min(Math.max(idx, 0), remaining.length - 1)]
      : null
    act(() => api.approveUnifiedRule(refOf(selected)), "Approved").then(() => {
      if (next) setSelected(next)
      else setPanelOpen(false)
    })
  }

  const rejectSel = () => {
    if (!selected || selected.kind !== "learned") return
    const list = rows ?? []
    const idx = list.findIndex((r) => ruleKey(r) === ruleKey(selected))
    const remaining = list.filter((r) => ruleKey(r) !== ruleKey(selected))
    const next = remaining.length
      ? remaining[Math.min(Math.max(idx, 0), remaining.length - 1)]
      : null
    act(() => api.rejectUnifiedRule(refOf(selected)), "Rejected").then(() => {
      if (next) setSelected(next)
      else setPanelOpen(false)
    })
  }

  const toggleSel = (enabled: boolean) => {
    if (!selected) return
    if (selected.kind === "written_repo") {
      toast.message("Per-repo rules are always on")
      return
    }
    act(
      () => api.setUnifiedRuleEnabled({ ...refOf(selected), enabled }),
      enabled ? "Enabled" : "Disabled",
    )
    setSelected({ ...selected, enabled })
  }

  const onRebuildLearnings = async () => {
    if (rebuildLocked) return
    setRebuildBusy(true)
    try {
      if (repoFilter === ALL) {
        const result = await api.synthesizeLearnings()
        toast.success("Rebuild started", {
          description:
            result.repos != null
              ? `${result.repos} repo(s) · progress below`
              : "Progress below",
        })
      } else {
        const [owner, repo] = repoFilter.split("/")
        await api.synthesizeLearningsRepo(owner, repo)
        toast.success(`Rebuild started · ${repoFilter}`, {
          description: "Progress below",
        })
      }
      setProgressOpen(true)
      refresh()
    } catch (e) {
      setRebuildBusy(false)
      toast.error("Rebuild failed", {
        description: e instanceof Error ? e.message : String(e),
      })
    }
  }

  const columns: Column<UnifiedRule>[] = useMemo(
    () => [
      {
        key: "scope",
        header: "Scope",
        sortable: true,
        sortValue: (r) => scopeLabel(r).toLowerCase(),
        cell: (r) => (
          <span className="font-mono text-xs text-muted-foreground">
            {scopeLabel(r)}
          </span>
        ),
        cellClassName: "w-44 align-top",
      },
      {
        key: "rule",
        header: "Rule",
        sortable: true,
        sortValue: (r) => (r.title || r.text).toLowerCase(),
        // TableCell defaults to whitespace-nowrap — override so line-clamp works.
        cellClassName: "max-w-0 align-top whitespace-normal",
        cell: (r) => (
          <div className={cn("min-w-0", !r.enabled && "opacity-50")}>
            {r.title ? (
              <>
                <div className="truncate text-sm font-medium">{r.title}</div>
                <div className="line-clamp-2 break-words text-sm text-muted-foreground">
                  {r.text}
                </div>
              </>
            ) : (
              <div className="line-clamp-2 break-words text-sm">{r.text}</div>
            )}
            <div className="mt-0.5 flex flex-wrap gap-1.5 text-xs text-muted-foreground">
              <span>{kindLabel(r.kind)}</span>
              {r.category && r.category !== "human_review" ? (
                <span>· {r.category}</span>
              ) : null}
              {formatPath(r.path_pattern) ? (
                <span className="truncate">· {formatPath(r.path_pattern)}</span>
              ) : null}
            </div>
          </div>
        ),
      },
      {
        key: "status",
        header: "Status",
        sortable: true,
        sortValue: (r) =>
          r.status === "pending" ? "pending" : r.enabled ? "enabled" : "disabled",
        cell: (r) => <StatusBadge rule={r} />,
        cellClassName: "w-28 align-top",
      },
      {
        key: "updated",
        header: "Updated",
        sortable: true,
        sortValue: (r) => r.updated_at,
        cell: (r) => (
          <span className="text-xs text-muted-foreground">{formatDate(r.updated_at)}</span>
        ),
        cellClassName: "w-28 align-top",
      },
    ],
    [],
  )

  const table = useDataTable({
    rows: rows ?? [],
    columns,
    pageSize: 20,
    initialSort: { key: "updated", dir: "desc" },
  })

  return (
    <div className="flex h-[calc(100svh-3rem)] flex-col gap-4 overflow-hidden p-6">
      <div className="shrink-0 space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Rules</h1>
            <p className="text-sm text-muted-foreground">
              Active rules · Pending approval
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            {isAdmin && (
              <>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={rebuildLocked}
                  onClick={onRebuildLearnings}
                  title={
                    rebuildLocked
                      ? "A learnings job is already running"
                      : repoFilter === ALL
                        ? "Rebuild learned suggestions from stored feedback (no GitHub fetch). New items go to Pending."
                        : `Rebuild learnings for ${repoFilter} from stored feedback.`
                  }
                >
                  <RefreshCw
                    className={cn(
                      "mr-1 h-4 w-4",
                      rebuildLocked && "animate-spin",
                    )}
                  />
                  Rebuild learnings
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => setScanOpen(true)}
                  title="Scan merged PRs for review feedback, then rebuild learnings."
                >
                  <History className="mr-1 h-4 w-4" /> Backfill
                </Button>
              </>
            )}
            <Button size="sm" onClick={() => navigate("/rules/new")}>
              <Plus className="mr-1 h-4 w-4" /> Add rule
            </Button>
          </div>
        </div>

        <Tabs value={mode} onValueChange={(v) => setMode(v as "pending" | "active")}>
          <TabsList>
            <TabsTrigger value="active" className="gap-1.5">
              <Library className="h-3.5 w-3.5" />
              Active
            </TabsTrigger>
            <TabsTrigger value="pending" className="gap-1.5">
              <Inbox className="h-3.5 w-3.5" />
              Pending
              {pendingCount > 0 && (
                <Badge variant="default" className="ml-1 tabular-nums">
                  {pendingCount}
                </Badge>
              )}
            </TabsTrigger>
          </TabsList>
        </Tabs>

        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-[200px] flex-1">
            <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              className="pl-8"
              placeholder="Filter rules…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <Select value={repoFilter} onValueChange={setRepoFilter}>
            <SelectTrigger className="w-[200px]">
              <SelectValue placeholder="All repos" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>All repos</SelectItem>
              {(repos ?? []).map((r) => (
                <SelectItem
                  key={`${r.platform}:${r.owner}/${r.repo}`}
                  value={`${r.owner}/${r.repo}`}
                >
                  {r.owner}/{r.repo}
                  {r.platform !== "github" ? ` (${r.platform})` : ""}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {mode === "active" && (
            <>
              <Select value={kindFilter} onValueChange={setKindFilterAndUrl}>
                <SelectTrigger className="w-[140px]">
                  <SelectValue placeholder="All kinds" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>All kinds</SelectItem>
                  <SelectItem value="written_global">Global</SelectItem>
                  <SelectItem value="written_repo">Per-repo</SelectItem>
                  <SelectItem value="learned">Learned</SelectItem>
                </SelectContent>
              </Select>
              <Select
                value={enabledFilter}
                onValueChange={(v) =>
                  setEnabledFilter(v as "all" | "enabled" | "disabled")
                }
              >
                <SelectTrigger className="w-[130px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All</SelectItem>
                  <SelectItem value="enabled">Enabled</SelectItem>
                  <SelectItem value="disabled">Disabled</SelectItem>
                </SelectContent>
              </Select>
            </>
          )}
          <Button size="sm" variant="ghost" onClick={refresh} title="Refresh">
            <RefreshCw className="h-4 w-4" />
          </Button>
        </div>
      </div>

      <LearningBackfillDialog
        open={scanOpen}
        onOpenChange={setScanOpen}
        onComplete={refresh}
      />
      <LearningJobProgressDialog
        open={progressOpen}
        onOpenChange={setProgressOpen}
        onComplete={refresh}
        onConfigureBackfill={() => setScanOpen(true)}
      />
      {isAdmin && (
        <LearningBackfillStrip
          onOpenProgress={() => setProgressOpen(true)}
          refreshKey={refreshKey}
          onTerminal={refresh}
          onActiveChange={onLearningsActiveChange}
        />
      )}

      <div className="flex min-h-0 flex-1 flex-col gap-2">
        {loading && !rows ? (
          <div className="text-sm text-muted-foreground">Loading…</div>
        ) : error ? (
          <div className="text-sm text-destructive">Couldn't load rules: {error}</div>
        ) : (rows ?? []).length === 0 ? (
          <Card className="flex min-h-0 flex-1 flex-col">
            <CardContent className="flex flex-1 flex-col items-center justify-center gap-2 py-12 text-center">
              {mode === "pending" ? (
                <>
                  <Inbox className="h-8 w-8 text-muted-foreground" />
                  <p className="text-sm font-medium">No pending rules</p>
                  <p className="max-w-sm text-xs text-muted-foreground">
                    Synthesis and @remember land here.
                  </p>
                </>
              ) : (
                <>
                  <Library className="h-8 w-8 text-muted-foreground" />
                  <p className="text-sm font-medium">No rules yet</p>
                  <p className="max-w-sm text-xs text-muted-foreground">
                    Add a rule or backfill past PRs.
                  </p>
                  {isAdmin && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="mt-1"
                      onClick={() => setScanOpen(true)}
                    >
                      <History className="mr-1 h-4 w-4" /> Backfill
                    </Button>
                  )}
                </>
              )}
            </CardContent>
          </Card>
        ) : (
          <>
            <Card className="flex min-h-0 flex-1 flex-col overflow-hidden py-0">
              <div className="themed-scrollbar min-h-0 flex-1 overflow-auto">
                <DataTable
                  table={table}
                  rowKey={ruleKey}
                  onRowClick={openDetail}
                />
              </div>
            </Card>
            <DataTablePagination table={table} />
          </>
        )}
      </div>

      <div
        aria-hidden={!panelOpen}
        className={cn(
          "fixed right-0 top-12 bottom-0 z-30 flex w-full max-w-[560px] flex-col border-l bg-background shadow-2xl transition-transform duration-300 ease-in-out",
          panelOpen ? "translate-x-0" : "pointer-events-none translate-x-full",
        )}
      >
        {selected && (
          <>
            <div className="flex items-center justify-between gap-3 border-b p-6">
              <div className="min-w-0">
                <div className="truncate font-mono text-xs text-muted-foreground">
                  {scopeLabel(selected)} · {kindLabel(selected.kind)}
                </div>
                <div className="mt-1">
                  <StatusBadge rule={selected} />
                </div>
              </div>
              <Button
                variant="ghost"
                size="icon"
                onClick={() => setPanelOpen(false)}
                aria-label="Close"
              >
                <X className="h-4 w-4" />
              </Button>
            </div>

            <div className="flex flex-wrap gap-2 border-b px-6 py-3">
              {isAdmin &&
              selected.kind === "learned" &&
              selected.status === "pending" ? (
                <>
                  <Button size="sm" onClick={approveSel}>
                    <Check className="mr-1 h-4 w-4" /> Approve
                  </Button>
                  <ConfirmButton
                    size="sm"
                    variant="outline"
                    destructive
                    dialogTitle="Reject pending rule?"
                    dialogDescription="Leaves Pending and won't inject into reviews."
                    confirmLabel="Reject"
                    onConfirm={rejectSel}
                  >
                    <X className="mr-1 h-4 w-4" /> Reject
                  </ConfirmButton>
                </>
              ) : null}
              {isAdmin &&
              selected.kind !== "written_repo" &&
              selected.status === "approved" ? (
                selected.enabled ? (
                  <Button size="sm" variant="outline" onClick={() => toggleSel(false)}>
                    <Power className="mr-1 h-4 w-4" /> Disable
                  </Button>
                ) : (
                  <Button size="sm" variant="outline" onClick={() => toggleSel(true)}>
                    <Power className="mr-1 h-4 w-4" /> Enable
                  </Button>
                )
              ) : null}
              {canEdit(selected) && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => navigate(editHref(selected))}
                >
                  <Pencil className="mr-1 h-4 w-4" /> Edit
                </Button>
              )}
            </div>

            <div className="themed-scrollbar flex-1 space-y-6 overflow-auto p-6">
              {selected.title ? (
                <div>
                  <h2 className="text-lg font-semibold">{selected.title}</h2>
                  <p className="mt-2 whitespace-pre-wrap text-sm">{selected.text}</p>
                </div>
              ) : (
                <p className="whitespace-pre-wrap text-sm">{selected.text}</p>
              )}
              <dl className="grid grid-cols-2 gap-4">
                <Meta label="Kind" value={kindLabel(selected.kind)} />
                <Meta
                  label="Scope"
                  value={
                    selected.kind === "written_global"
                      ? "All repos"
                      : (selected.repos?.length
                          ? selected.repos.join(", ")
                          : scopeLabel(selected))
                  }
                />
                {selected.kind === "learned" && (
                  <>
                    {formatPath(selected.path_pattern) ? (
                      <Meta
                        label="Path"
                        value={formatPath(selected.path_pattern)}
                      />
                    ) : null}
                    {evidencePrList(selected).length > 0 ? (
                      <div className="col-span-2">
                        <dt className="text-xs text-muted-foreground">
                          Evidence PRs
                        </dt>
                        <dd className="mt-1">
                          <EvidenceLinks rule={selected} />
                        </dd>
                      </div>
                    ) : null}
                    {selected.created_by ? (
                      <Meta label="Created by" value={selected.created_by} />
                    ) : null}
                  </>
                )}
                <Meta label="Updated" value={formatDate(selected.updated_at)} />
              </dl>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
