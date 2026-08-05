import { Loader2 } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import {
  isActiveLearningJob,
  learningJobStatusLine,
  newPrCount,
  repoKey,
  type BackfillStatus,
} from "@/components/dashboard/learning-job-status"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { toast } from "@/components/ui/sonner"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { api, type RepoListItem } from "@/lib/api"
import { cn } from "@/lib/utils"

export type { BackfillStatus }
export { newPrCount }

function isGithub(r: RepoListItem) {
  return !r.platform || r.platform === "github"
}

function formatCost(usd: number): string {
  if (usd === 0) return "$0"
  if (usd < 0.01) return "~<$0.01"
  return `~$${usd.toFixed(2)}`
}

export function LearningBackfillDialog({
  open,
  onOpenChange,
  onComplete,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onComplete: () => void
}) {
  const [repos, setRepos] = useState<RepoListItem[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [maxPrs, setMaxPrs] = useState(100)
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [scanning, setScanning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [statuses, setStatuses] = useState<BackfillStatus[]>([])
  const [estimate, setEstimate] = useState<{
    estimated_usd: number
    input_tokens: number
    output_tokens: number
    model: string
    synth_calls: number
    skipped_repos: number
    basis: string
  } | null>(null)
  const [estimateReady, setEstimateReady] = useState(false)

  const selectedKey = useMemo(
    () => [...selected].sort().join(","),
    [selected],
  )

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    setError(null)
    setScanning(false)
    setStatuses([])
    setEstimate(null)
    setEstimateReady(false)
    api
      .listRepos()
      .then(async (data) => {
        if (cancelled) return
        const gh = data.filter(isGithub)
        const keys = new Set(gh.map((r) => repoKey(r.owner, r.repo)))
        setRepos(gh)
        setSelected(keys)
        // Resume polling if a backfill is already running (e.g. reopen mid-run).
        try {
          const all = await api.getLearningsBackfillStatus()
          if (cancelled) return
          const mine = all.filter((s) => keys.has(repoKey(s.owner, s.repo)))
          setStatuses(mine)
          if (mine.some(isActiveLearningJob)) {
            setScanning(true)
          }
        } catch {
          /* strip still shows progress */
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Failed to load repos")
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open])

  useEffect(() => {
    if (!open || loading) return
    let cancelled = false
    const keys = [...selected]
    const timer = window.setTimeout(() => {
      if (cancelled) return
      if (keys.length === 0) {
        setEstimate({
          estimated_usd: 0,
          input_tokens: 0,
          output_tokens: 0,
          model: "",
          synth_calls: 0,
          skipped_repos: 0,
          basis: "none",
        })
        setEstimateReady(true)
        return
      }
      setEstimateReady(false)
      api
        .getLearningsBackfillEstimate({
          repos: keys.map((k) => {
            const [owner, repo] = k.split("/", 2)
            return { owner, repo }
          }),
          max_prs: maxPrs,
        })
        .then((est) => {
          if (cancelled) return
          setEstimate({
            estimated_usd: est.estimated_usd,
            input_tokens: est.input_tokens,
            output_tokens: est.output_tokens,
            model: est.model,
            synth_calls: est.synth_calls ?? keys.length,
            skipped_repos: est.skipped_repos ?? 0,
            basis: est.basis ?? "stored_feedback_and_catalog",
          })
        })
        .catch(() => {
          if (!cancelled) setEstimate(null)
        })
        .finally(() => {
          if (!cancelled) setEstimateReady(true)
        })
    }, 250)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [open, loading, selectedKey, maxPrs, selected])

  useEffect(() => {
    if (!scanning) return
    let cancelled = false
    const tick = async () => {
      try {
        const all = await api.getLearningsBackfillStatus()
        if (cancelled) return
        const mine = all.filter((s) => selected.has(repoKey(s.owner, s.repo)))
        setStatuses(mine)
        // Wait until every selected repo reports a terminal status for this run.
        // `running` is set before the job awaits tokens, so a stale prior
        // "complete" alone is not enough to finish.
        const byKey = new Map(mine.map((s) => [repoKey(s.owner, s.repo), s]))
        const allTerminal =
          selected.size > 0 &&
          [...selected].every((k) => {
            const s = byKey.get(k)
            return s?.status === "complete" || s?.status === "failed"
          })
        const anyActive = mine.some(isActiveLearningJob)
        if (!allTerminal || anyActive) return

        setScanning(false)
        onComplete()
        const failed = mine.filter((s) => s.status === "failed")
        if (failed.length) {
          setError(
            failed
              .map((s) => `${s.owner}/${s.repo}: ${s.error || "failed"}`)
              .join("; ")
          )
        } else {
          const neu = mine.reduce((n, s) => n + newPrCount(s), 0)
          const skipped = mine.reduce((n, s) => n + (s.skipped ?? 0), 0)
          toast.success(`Backfill complete · ${neu} new · ${skipped} skipped`)
          onOpenChange(false)
        }
      } catch {
        /* keep polling */
      }
    }
    tick()
    const id = setInterval(tick, 2000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [scanning, selected, onComplete, onOpenChange])

  const toggle = (key: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const toggleAll = () => {
    if (selected.size === repos.length) setSelected(new Set())
    else setSelected(new Set(repos.map((r) => repoKey(r.owner, r.repo))))
  }

  const handleStart = async () => {
    if (selected.size === 0) {
      setError("Select at least one repository")
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await api.refreshLearnings({
        repos: [...selected].map((k) => {
          const [owner, repo] = k.split("/", 2)
          return { owner, repo }
        }),
        max_prs: maxPrs,
      })
      setScanning(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start backfill")
    } finally {
      setSubmitting(false)
    }
  }

  const statusByKey = useMemo(() => {
    const m = new Map<string, BackfillStatus>()
    for (const s of statuses) m.set(repoKey(s.owner, s.repo), s)
    return m
  }, [statuses])

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Backfill</DialogTitle>
          <DialogDescription>
            {loading
              ? "Loading repositories…"
              : scanning
                ? "Scanning merged PRs…"
                : `${selected.size} of ${repos.length} ${repos.length === 1 ? "repository" : "repositories"} selected`}
          </DialogDescription>
        </DialogHeader>

        {loading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        ) : (
          <div className="space-y-4">
            <div className="flex items-center justify-between gap-2">
              <button
                type="button"
                className="text-xs text-muted-foreground underline-offset-2 hover:underline"
                onClick={toggleAll}
                disabled={scanning}
              >
                {selected.size === repos.length ? "Deselect all" : "Select all"}
              </button>
              <span className="text-xs text-muted-foreground tabular-nums">
                {selected.size} selected
              </span>
            </div>

            <div className="max-h-56 space-y-2 overflow-y-auto rounded-md border p-2">
              {repos.length === 0 ? (
                <p className="p-2 text-sm text-muted-foreground">
                  No GitHub repositories registered.
                </p>
              ) : (
                repos.map((r) => {
                  const key = repoKey(r.owner, r.repo)
                  const st = statusByKey.get(key)
                  return (
                    <label
                      key={key}
                      className={cn(
                        "flex cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted/50",
                        scanning && "cursor-default"
                      )}
                    >
                      <Checkbox
                        checked={selected.has(key)}
                        disabled={scanning}
                        onCheckedChange={() => toggle(key)}
                        className="mt-0.5"
                      />
                      <span className="min-w-0 flex-1">
                        <span className="font-medium">
                          {r.owner}/{r.repo}
                        </span>
                        {st?.status && (
                          <span className="mt-0.5 block text-xs text-muted-foreground">
                            {learningJobStatusLine(st)}
                          </span>
                        )}
                      </span>
                    </label>
                  )
                })
              )}
            </div>

            <div className="space-y-1.5">
              <label htmlFor="max-prs" className="text-sm font-medium">
                Max merged PRs per repo
              </label>
              <Input
                id="max-prs"
                type="number"
                min={1}
                max={5000}
                value={maxPrs}
                disabled={scanning}
                onChange={(e) =>
                  setMaxPrs(Math.max(1, Number(e.target.value) || 1))
                }
              />
            </div>

            <div className="rounded-lg border bg-muted/30 p-3">
              <div className="flex items-center justify-between text-sm">
                <span className="text-muted-foreground">Estimated cost</span>
                {!estimateReady ? (
                  <Badge variant="secondary" className="gap-1.5">
                    <Loader2 className="h-3 w-3 animate-spin" />
                    Calculating
                  </Badge>
                ) : estimate ? (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Badge variant="secondary" className="cursor-help">
                        {formatCost(estimate.estimated_usd)}
                      </Badge>
                    </TooltipTrigger>
                    <TooltipContent className="max-w-xs">
                      <div className="space-y-0.5 text-xs">
                        <div>
                          Synth calls: {estimate.synth_calls}
                          {estimate.skipped_repos > 0
                            ? ` · ${estimate.skipped_repos} repos skip LLM`
                            : ""}
                        </div>
                        <div>
                          In: {estimate.input_tokens.toLocaleString()} tokens
                        </div>
                        <div>
                          Out: {estimate.output_tokens.toLocaleString()} tokens
                        </div>
                        <div className="pt-1 text-muted-foreground">
                          Synth calls = repos with enough feedback for LLM
                          (multi-stage classify/extract/cluster). Cold repos
                          assume a mid-size sample from max PRs. GitHub API free.
                        </div>
                      </div>
                    </TooltipContent>
                  </Tooltip>
                ) : (
                  <Badge variant="secondary">Unavailable</Badge>
                )}
              </div>
              {estimate && estimate.model ? (
                <div className="mt-2 flex items-center justify-between text-sm">
                  <span className="text-muted-foreground">Model</span>
                  <span className="font-mono text-xs">{estimate.model}</span>
                </div>
              ) : null}
            </div>

            {error && (
              <p className="text-sm text-destructive" role="alert">
                {error}
              </p>
            )}
          </div>
        )}

        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={submitting}
          >
            {scanning ? "Close" : "Cancel"}
          </Button>
          <Button
            onClick={handleStart}
            disabled={loading || submitting || scanning || selected.size === 0}
          >
            {(submitting || scanning) && (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            )}
            {scanning ? "Backfilling…" : "Start backfill"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
