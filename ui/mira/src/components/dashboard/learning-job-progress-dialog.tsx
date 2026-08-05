import { Loader2, RefreshCw } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import {
  isActiveLearningJob,
  isSynthJob,
  jobKindLabel,
  learningJobStatusLine,
  newPrCount,
  pickPrimaryJob,
  repoKey,
  type LearningJobStatus,
} from "@/components/dashboard/learning-job-status"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { toast } from "@/components/ui/sonner"
import { api } from "@/lib/api"
import { cn } from "@/lib/utils"

function relativeTime(epochSeconds: number) {
  if (!epochSeconds) return "—"
  const s = Math.floor(Date.now() / 1000 - epochSeconds)
  if (s < 60) return "just now"
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  if (d < 30) return `${d}d ago`
  return new Date(epochSeconds * 1000).toLocaleDateString()
}

function pillFor(st: LearningJobStatus): {
  label: string
  className: string
} {
  if (st.status === "queued") {
    return {
      label: "Queued",
      className:
        "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400",
    }
  }
  if (st.status === "running") {
    return {
      label: "Running",
      className:
        "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400",
    }
  }
  if (st.status === "failed") {
    return {
      label: "Failed",
      className: "border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-400",
    }
  }
  if (st.status === "complete") {
    return {
      label: "Complete",
      className:
        "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
    }
  }
  return { label: st.status || "—", className: "" }
}

export function LearningJobProgressDialog({
  open,
  onOpenChange,
  onComplete,
  onConfigureBackfill,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onComplete?: () => void
  /** Open the Backfill setup dialog (repo picker / max PRs). */
  onConfigureBackfill?: () => void
}) {
  const [statuses, setStatuses] = useState<LearningJobStatus[]>([])
  const [loading, setLoading] = useState(true)
  const [retrying, setRetrying] = useState(false)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoading(true)
    const tick = async () => {
      try {
        const all = await api.getLearningsBackfillStatus()
        if (cancelled) return
        setStatuses(all)
        setLoading(false)
      } catch {
        if (!cancelled) setLoading(false)
      }
    }
    tick()
    const id = setInterval(tick, 2000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [open])

  const primary = useMemo(() => pickPrimaryJob(statuses), [statuses])
  const kind = jobKindLabel(primary)
  const anyActive = statuses.some(isActiveLearningJob)
  const sorted = useMemo(() => {
    return [...statuses].sort((a, b) => {
      const rank = (s: LearningJobStatus) =>
        s.status === "running" ? 0 : s.status === "queued" ? 1 : 2
      const d = rank(a) - rank(b)
      if (d !== 0) return d
      return (b.updated_at ?? 0) - (a.updated_at ?? 0)
    })
  }, [statuses])

  const failedSynth = sorted.filter(
    (s) => isSynthJob(s) && s.status === "failed",
  )

  const handleRetryRebuild = async () => {
    if (!failedSynth.length || retrying) return
    setRetrying(true)
    try {
      if (failedSynth.length === 1) {
        const s = failedSynth[0]
        await api.synthesizeLearningsRepo(s.owner, s.repo)
        toast.success(`Rebuild started · ${s.owner}/${s.repo}`)
      } else {
        const result = await api.synthesizeLearnings()
        toast.success("Rebuild started", {
          description:
            result.repos != null ? `${result.repos} repo(s)` : undefined,
        })
      }
      onComplete?.()
    } catch (e) {
      toast.error("Rebuild failed", {
        description: e instanceof Error ? e.message : String(e),
      })
    } finally {
      setRetrying(false)
    }
  }

  const title = kind === "rebuild" ? "Rebuild learnings" : "Backfill progress"
  const description =
    kind === "rebuild"
      ? anyActive
        ? "Rebuilding rules from stored feedback (no GitHub fetch)."
        : "Last rebuild from stored feedback."
      : anyActive
        ? "Fetching merged PRs and synthesizing learnings."
        : "Last backfill run."

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        {loading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        ) : sorted.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">
            No learnings job status yet.
          </p>
        ) : (
          <div className="max-h-72 space-y-2 overflow-y-auto rounded-md border p-2">
            {sorted.map((st) => {
              const key = repoKey(st.owner, st.repo)
              const pill = pillFor(st)
              const spinning =
                st.status === "running" || st.status === "queued"
              return (
                <div
                  key={key}
                  className="flex items-start gap-2 rounded-md px-2 py-1.5 text-sm"
                >
                  {spinning ? (
                    <Loader2
                      className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin text-amber-600 dark:text-amber-400"
                      aria-hidden
                    />
                  ) : (
                    <span className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="font-medium">
                        {st.owner}/{st.repo}
                      </span>
                      <Badge
                        variant="outline"
                        className={cn("h-5 px-1.5 text-[10px]", pill.className)}
                      >
                        {isSynthJob(st) ? "Rebuild" : "Backfill"} · {pill.label}
                      </Badge>
                    </div>
                    <div className="mt-0.5 text-xs text-muted-foreground">
                      {learningJobStatusLine(st)}
                      {st.updated_at
                        ? ` · ${relativeTime(st.updated_at)}`
                        : ""}
                    </div>
                    {st.status === "failed" && st.error ? (
                      <div className="mt-0.5 text-xs text-destructive">
                        {st.error}
                      </div>
                    ) : null}
                    {st.status === "complete" && !isSynthJob(st) ? (
                      <div className="mt-0.5 text-xs text-muted-foreground">
                        {newPrCount(st)} new · {st.skipped ?? 0} skipped
                      </div>
                    ) : null}
                  </div>
                </div>
              )
            })}
          </div>
        )}

        <DialogFooter className="flex-col gap-2 sm:flex-row sm:justify-between">
          <div className="flex flex-wrap gap-2">
            {kind === "backfill" && onConfigureBackfill ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={anyActive}
                onClick={() => {
                  onOpenChange(false)
                  onConfigureBackfill()
                }}
              >
                Configure backfill
              </Button>
            ) : null}
            {failedSynth.length > 0 ? (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={retrying || anyActive}
                onClick={handleRetryRebuild}
              >
                {retrying ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                ) : (
                  <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
                )}
                Retry rebuild
              </Button>
            ) : null}
          </div>
          <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
