import { ChevronLeft, Loader2, Trash2 } from "lucide-react"
import { useEffect, useState } from "react"
import { useNavigate, useSearchParams } from "react-router"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { ConfirmButton } from "@/components/ui/confirm-button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { toast } from "@/components/ui/sonner"
import { api, type UnifiedRuleKind } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { useDocumentTitle } from "@/lib/hooks"
import { cn } from "@/lib/utils"

/** Split learned `path_pattern` into editable scope + hidden identity. */
function splitLearnedPath(path: string): { scope: string; identity: string } {
  const raw = (path || "").trim()
  if (!raw) return { scope: "", identity: "" }
  const parts = raw.split("::")
  if (parts.length === 2 && /^__[\w]+__$/.test(parts[1])) {
    return { scope: parts[0], identity: parts[1] }
  }
  if (/^__[\w]+__$/.test(raw)) return { scope: "", identity: raw }
  return { scope: raw, identity: "" }
}

function joinLearnedPath(scope: string, identity: string): string {
  const s = scope.trim()
  const id = identity.trim()
  if (s && id) return `${s}::${id}`
  return s || id
}

function parseDetail(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e)
  try {
    const parsed = JSON.parse(raw.replace(/^API error \d+: /, ""))
    if (parsed?.detail)
      return typeof parsed.detail === "string"
        ? parsed.detail
        : JSON.stringify(parsed.detail)
  } catch {
    /* ignore */
  }
  return raw
}

type ScopeMode = "global" | "repos"

export function RuleFormPage() {
  const { user } = useAuth()
  const isAdmin = !!user?.is_admin
  const username = user?.username ?? ""
  const navigate = useNavigate()
  const [params] = useSearchParams()

  const editKind = (params.get("kind") as UnifiedRuleKind | null) ?? null
  const editId = params.get("id")
  const editOwner = params.get("owner") ?? ""
  const editRepo = params.get("repo") ?? ""
  const editPlatform = params.get("platform") ?? "github"
  const isEdit = Boolean(editId && editKind)
  useDocumentTitle(isEdit ? "Edit rule" : "Add rule")

  const [loading, setLoading] = useState(isEdit)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [canEdit, setCanEdit] = useState(true)
  const [canDelete, setCanDelete] = useState(false)
  const [status, setStatus] = useState("approved")

  // Add = written only. Learned arrives via Pending (synth / @remember / backfill).
  const [kind, setKind] = useState<UnifiedRuleKind>(() => {
    if (isEdit && editKind) return editKind
    if (editKind === "written_repo" || editKind === "written_global") return editKind
    return "written_global"
  })
  const [repos, setRepos] = useState<{ value: string; label: string }[]>([])
  const [repoKey, setRepoKey] = useState("")
  const [selectedRepoKeys, setSelectedRepoKeys] = useState<string[]>([])
  const [scopeMode, setScopeMode] = useState<ScopeMode>("global")
  const [title, setTitle] = useState("")
  const [text, setText] = useState("")
  const [category, setCategory] = useState("other")
  const [pathPattern, setPathPattern] = useState("")
  const [internalPathKey, setInternalPathKey] = useState("")
  const [platform, setPlatform] = useState(editPlatform)

  const rulesHome =
    kind === "learned" && status === "pending"
      ? "/rules?mode=pending"
      : "/rules"

  useEffect(() => {
    const reposP = api.listRepos().catch(() => [])
    const ruleP =
      isEdit && editKind && editId
        ? api.getUnifiedRule({
            kind: editKind,
            id: Number(editId),
            owner: editOwner,
            repo: editRepo,
            platform: editPlatform,
          })
        : Promise.resolve(null)
    Promise.all([reposP, ruleP])
      .then(([list, rule]) => {
        const options = list.map((r) => {
          const plat = r.platform || "github"
          const slug = `${r.owner}/${r.repo}`
          return {
            value: `${plat}|${slug}`,
            label: plat === "github" ? slug : `${slug} (${plat})`,
          }
        })
        setRepos(options)
        if (rule) {
          setKind(rule.kind)
          setTitle(rule.title || "")
          setText(rule.text || "")
          setCategory(rule.category || "other")
          setPlatform(rule.platform || "github")
          setStatus(rule.status || "approved")
          if (rule.kind === "written_global") {
            setScopeMode("global")
            setSelectedRepoKeys([])
          } else {
            setScopeMode("repos")
            const fromRepos = (rule.repos || []).map((slug) => {
              const match = options.find((o) => o.value.endsWith(`|${slug}`))
              return match?.value || `${rule.platform || "github"}|${slug}`
            })
            if (fromRepos.length) {
              setSelectedRepoKeys(fromRepos)
              setRepoKey(fromRepos[0])
            } else if (rule.owner && rule.repo) {
              const key = `${rule.platform || "github"}|${rule.owner}/${rule.repo}`
              setSelectedRepoKeys([key])
              setRepoKey(key)
            }
          }
          const { scope, identity } = splitLearnedPath(rule.path_pattern || "")
          setInternalPathKey(identity)
          setPathPattern(scope)
          const ownPending =
            rule.kind === "learned" &&
            rule.created_by === username &&
            rule.status === "pending"
          setCanEdit(isAdmin || ownPending)
          setCanDelete(isAdmin || ownPending)
        } else if (!isEdit) {
          setScopeMode(kind === "written_global" ? "global" : "repos")
        }
      })
      .catch((e) => setError(parseDetail(e)))
      .finally(() => setLoading(false))
  }, [
    username,
    isAdmin,
    isEdit,
    editKind,
    editId,
    editOwner,
    editRepo,
    editPlatform,
    kind,
  ])

  if (isEdit && !loading && !canEdit) {
    return (
      <div className="p-6 text-sm text-muted-foreground">
        You can only edit your own pending rules.
      </div>
    )
  }

  const toggleRepo = (key: string) => {
    setSelectedRepoKeys((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    )
  }

  const parseRepoKey = (key: string) => {
    const [plat, slug] = key.split("|")
    const [owner, repo] = (slug || "").split("/")
    return { platform: plat || "github", owner: owner || "", repo: repo || "" }
  }

  const save = async () => {
    if (!text.trim()) {
      setError("Enter the rule text.")
      return
    }

    if (!isEdit && kind === "learned") {
      setError("Learned rules come from Pending — pick Global or Per-repo.")
      return
    }

    // Create path: written Global | single Per-repo
    if (!isEdit) {
      if (kind === "written_repo" && !repoKey) {
        setError("Pick a repo.")
        return
      }
      const ref = kind === "written_repo" ? parseRepoKey(repoKey) : null
      setSaving(true)
      setError(null)
      try {
        await api.createUnifiedRule({
          kind,
          title: title.trim(),
          text: text.trim(),
          owner: ref?.owner,
          repo: ref?.repo,
          platform: ref?.platform,
        })
        toast.success("Rule added")
        navigate(rulesHome)
      } catch (e) {
        setError(parseDetail(e))
        toast.error("Couldn't save rule", { description: parseDetail(e) })
      } finally {
        setSaving(false)
      }
      return
    }

    // Edit path: scope + text
    if (scopeMode === "repos") {
      if (kind === "learned" && selectedRepoKeys.length < 1) {
        setError("Pick at least one repo.")
        return
      }
      if (kind !== "learned" && selectedRepoKeys.length !== 1) {
        setError("Written rules need Global or exactly one repo.")
        return
      }
    }

    const path_pattern = joinLearnedPath(pathPattern, internalPathKey)

    const scope_repos =
      scopeMode === "repos"
        ? (kind === "learned" ? selectedRepoKeys : selectedRepoKeys.slice(0, 1)).map(
            (key) => {
              const r = parseRepoKey(key)
              return { owner: r.owner, repo: r.repo, platform: r.platform }
            },
          )
        : undefined

    setSaving(true)
    setError(null)
    try {
      await api.updateUnifiedRule({
        kind: editKind!,
        id: Number(editId),
        title: title.trim(),
        text: text.trim(),
        owner: editOwner,
        repo: editRepo,
        platform: editPlatform || platform,
        category: category.trim() || "other",
        path_pattern,
        scope: scopeMode,
        scope_repos,
      })
      toast.success("Rule saved")
      navigate(rulesHome)
    } catch (e) {
      setError(parseDetail(e))
      toast.error("Couldn't save rule", { description: parseDetail(e) })
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    if (!editId || !editKind) return
    setError(null)
    try {
      await api.deleteUnifiedRule({
        kind: editKind,
        id: Number(editId),
        owner: editOwner,
        repo: editRepo,
        platform: editPlatform,
      })
      toast.success("Rule deleted")
      navigate(rulesHome)
    } catch (e) {
      setError(parseDetail(e))
      toast.error("Couldn't delete rule", { description: parseDetail(e) })
    }
  }

  const isWritten = kind === "written_global" || kind === "written_repo"
  const showScopeEditor = isEdit || !isEdit

  return (
    <div className="mx-auto max-w-2xl space-y-6 p-6">
      <button
        onClick={() => navigate(rulesHome)}
        className="flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ChevronLeft className="h-4 w-4" /> Rules
      </button>

      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          {isEdit ? "Edit rule" : "Add rule"}
        </h1>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading…
        </div>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Details</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {!isEdit && (
                <div className="space-y-2">
                  <label className="text-sm font-medium">Kind</label>
                  <Select
                    value={kind}
                    onValueChange={(v) => {
                      const next = v as UnifiedRuleKind
                      setKind(next)
                      setScopeMode(next === "written_global" ? "global" : "repos")
                    }}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="written_global">Global</SelectItem>
                      <SelectItem value="written_repo">Per-repo</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              )}

              {isEdit && showScopeEditor && (
                <div className="space-y-2">
                  <label className="text-sm font-medium">Scope</label>
                  <Select
                    value={scopeMode}
                    onValueChange={(v) => setScopeMode(v as ScopeMode)}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="global">All repos (Global)</SelectItem>
                      <SelectItem value="repos">
                        {kind === "learned" ? "Selected repos" : "One repo"}
                      </SelectItem>
                    </SelectContent>
                  </Select>
                  {scopeMode === "global" && kind === "learned" ? (
                    <p className="text-xs text-muted-foreground">
                      Promotes to a Global written rule and removes learned copies.
                    </p>
                  ) : null}
                </div>
              )}

              {!isEdit && kind === "written_repo" && (
                <div className="space-y-2">
                  <label className="text-sm font-medium">Repo</label>
                  <Select
                    value={repoKey}
                    onValueChange={(key) => {
                      setRepoKey(key)
                      setPlatform(key.split("|")[0])
                    }}
                  >
                    <SelectTrigger>
                      <SelectValue placeholder="Select a repo" />
                    </SelectTrigger>
                    <SelectContent>
                      {repos.map((r) => (
                        <SelectItem key={r.value} value={r.value}>
                          {r.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              )}

              {isEdit && scopeMode === "repos" && (
                <div className="space-y-2">
                  <label className="text-sm font-medium">
                    {kind === "learned" ? "Repos" : "Repo"}
                  </label>
                  {kind === "learned" ? (
                    <div className="max-h-48 space-y-1 overflow-auto rounded-md border p-2">
                      {repos.map((r) => {
                        const checked = selectedRepoKeys.includes(r.value)
                        return (
                          <label
                            key={r.value}
                            className={cn(
                              "flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-muted/60",
                              checked && "bg-muted/40",
                            )}
                          >
                            <input
                              type="checkbox"
                              className="accent-primary"
                              checked={checked}
                              onChange={() => toggleRepo(r.value)}
                            />
                            <span className="font-mono text-xs">{r.label}</span>
                          </label>
                        )
                      })}
                    </div>
                  ) : (
                    <Select
                      value={selectedRepoKeys[0] || ""}
                      onValueChange={(key) => {
                        setSelectedRepoKeys([key])
                        setPlatform(key.split("|")[0])
                      }}
                    >
                      <SelectTrigger>
                        <SelectValue placeholder="Select a repo" />
                      </SelectTrigger>
                      <SelectContent>
                        {repos.map((r) => (
                          <SelectItem key={r.value} value={r.value}>
                            {r.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                </div>
              )}

              {(isWritten || (isEdit && scopeMode === "global")) && (
                <div className="space-y-2">
                  <label className="text-sm font-medium" htmlFor="rule-title">
                    Title
                  </label>
                  <Input
                    id="rule-title"
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                    placeholder="Short name"
                  />
                </div>
              )}

              <div className="space-y-2">
                <label className="text-sm font-medium" htmlFor="rule-text">
                  Rule
                </label>
                <Textarea
                  id="rule-text"
                  rows={4}
                  placeholder={
                    isWritten || scopeMode === "global"
                      ? "What reviewers should enforce…"
                      : "e.g. Don't flag missing docstrings on internal helpers."
                  }
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                />
              </div>

              {kind === "learned" && scopeMode === "repos" && (
                <div className="space-y-2">
                  <label className="text-sm font-medium" htmlFor="lr-path">
                    Path
                  </label>
                  <Input
                    id="lr-path"
                    placeholder="src/** (empty = all)"
                    value={pathPattern}
                    onChange={(e) => setPathPattern(e.target.value)}
                  />
                </div>
              )}
            </CardContent>
          </Card>

          {error && (
            <p className="break-words text-sm text-destructive">{error}</p>
          )}

          <div className="flex items-center justify-between">
            <div className="flex gap-2">
              <Button
                onClick={save}
                disabled={
                  saving ||
                  !text.trim() ||
                  (!isEdit && kind === "written_repo" && !repoKey)
                }
              >
                {saving && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                {isEdit ? "Save changes" : "Add rule"}
              </Button>
              <Button variant="ghost" onClick={() => navigate(rulesHome)}>
                Cancel
              </Button>
            </div>
            {isEdit && canDelete && (
              <ConfirmButton
                variant="ghost"
                className="text-destructive"
                destructive
                dialogTitle="Delete rule?"
                dialogDescription={
                  kind === "learned"
                    ? "Deletes this rule in every selected repo."
                    : "This permanently removes the rule."
                }
                confirmLabel="Delete"
                onConfirm={remove}
              >
                <Trash2 className="mr-2 h-4 w-4" /> Delete
              </ConfirmButton>
            )}
          </div>
        </>
      )}
    </div>
  )
}
