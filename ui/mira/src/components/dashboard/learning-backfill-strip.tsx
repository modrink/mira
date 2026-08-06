import { Check, Loader2, X } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"

import {
  isSynthJob,
  newPrCount,
  pickPrimaryJob,
  synthPhaseLine,
  type LearningJobStatus,
} from "@/components/dashboard/learning-job-status"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
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

export function LearningBackfillStrip({
  onOpenProgress,
  refreshKey = 0,
  onTerminal,
  onActiveChange,
}: {
  /** Open job-aware progress modal (rebuild vs backfill). */
  onOpenProgress: () => void
  refreshKey?: number
  /** Fires once when an active job batch becomes fully terminal. */
  onTerminal?: () => void
  /** True while any repo is queued or running. */
  onActiveChange?: (active: boolean) => void
}) {
  const [statuses, setStatuses] = useState<LearningJobStatus[]>([])
  const wasActive = useRef(false)

  useEffect(() => {
    let cancelled = false
    const tick = async () => {
      try {
        const all = await api.getLearningsBackfillStatus()
        if (!cancelled) setStatuses(all)
      } catch {
        /* ignore */
      }
    }
    tick()
    return () => {
      cancelled = true
    }
  }, [refreshKey])

  const primary = useMemo(() => pickPrimaryJob(statuses), [statuses])
  const anyActive = useMemo(
    () => statuses.some((s) => s.status === "running" || s.status === "queued"),
    [statuses],
  )

  useEffect(() => {
    onActiveChange?.(anyActive)
  }, [anyActive, onActiveChange])

  useEffect(() => {
    if (anyActive) {
      wasActive.current = true
      return
    }
    if (wasActive.current) {
      wasActive.current = false
      onTerminal?.()
    }
  }, [anyActive, onTerminal])

  useEffect(() => {
    if (!anyActive) return
    let cancelled = false
    const id = setInterval(async () => {
      try {
        const all = await api.getLearningsBackfillStatus()
        if (!cancelled) setStatuses(all)
      } catch {
        /* keep polling */
      }
    }, 2000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [anyActive])

  if (!primary?.status) return null

  const done = primary.prs_done ?? primary.prs ?? 0
  const skipped = primary.skipped ?? 0
  const neu = newPrCount(primary)
  const slug = `${primary.owner}/${primary.repo}`
  const status = primary.status
  const phase = primary.phase || ""
  const rebuild = isSynthJob(primary)
  const synthPhases = phase === "extract" || phase === "cluster"

  let title = rebuild ? `Last rebuild · ${slug}` : `Last backfill · ${slug}`
  let meta = ""
  let actionLabel = "Details"
  let Icon = Check
  let iconClass = "text-emerald-600 dark:text-emerald-400"
  let pillClass =
    "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
  let pillLabel = "Complete"

  if (status === "queued") {
    title = rebuild ? `Rebuild queued · ${slug}` : `Backfill queued · ${slug}`
    meta = rebuild
      ? "Waiting…"
      : primary.max_prs
        ? `Waiting · up to ${primary.max_prs} PRs`
        : "Waiting…"
    actionLabel = "Open"
    Icon = Loader2
    iconClass = "animate-spin text-amber-600 dark:text-amber-400"
    pillClass =
      "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400"
    pillLabel = "Queued"
  } else if (status === "running") {
    title = rebuild ? `Rebuild running · ${slug}` : `Backfill running · ${slug}`
    if (rebuild || synthPhases) {
      meta = synthPhaseLine(primary)
    } else if (phase === "listing") {
      meta = primary.max_prs
        ? `Listing merged PRs (up to ${primary.max_prs})…`
        : "Listing merged PRs…"
    } else if (phase === "synth") {
      meta = `Synthesizing · ${done} PRs ingested`
    } else {
      const denom =
        primary.total && primary.total > 0
          ? String(primary.total)
          : primary.max_prs
            ? `≤${primary.max_prs}`
            : "?"
      meta = `${done} / ${denom} PRs · ${neu} new so far · ${skipped} skipped`
    }
    actionLabel = "Open"
    Icon = Loader2
    iconClass = "animate-spin text-amber-600 dark:text-amber-400"
    pillClass =
      "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400"
    pillLabel = "Running"
  } else if (status === "failed") {
    title = rebuild ? `Last rebuild · ${slug}` : `Last backfill · ${slug}`
    meta = rebuild
      ? `Failed ${relativeTime(primary.updated_at ?? 0)}`
      : `Failed ${relativeTime(primary.updated_at ?? 0)} · scanned ${done}${
          primary.total != null ? ` / ${primary.total}` : ""
        } PRs before stop`
    actionLabel = "Details"
    Icon = X
    iconClass = "text-red-600 dark:text-red-400"
    pillClass = "border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-400"
    pillLabel = "Failed"
  } else if (status === "complete") {
    if (rebuild) {
      const rules = primary.llm_rules ?? 0
      meta = `Finished ${relativeTime(primary.updated_at ?? 0)} · ${rules} rules`
    } else {
      meta = `Finished ${relativeTime(primary.updated_at ?? 0)} · scanned ${done} PRs · ${neu} new · ${skipped} skipped`
    }
  } else {
    return null
  }

  return (
    <div className="flex shrink-0 items-start justify-between gap-3 rounded-lg border bg-muted/30 px-3 py-2.5 text-xs">
      <div className="flex min-w-0 items-start gap-2">
        <Icon className={cn("mt-0.5 h-3.5 w-3.5 shrink-0", iconClass)} aria-hidden />
        <div className="min-w-0">
          <div className="font-medium text-foreground">{title}</div>
          <div className="mt-0.5 text-muted-foreground">{meta}</div>
          {status === "failed" && primary.error && (
            <div className="mt-0.5 text-red-600 dark:text-red-400">{primary.error}</div>
          )}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        <Badge variant="outline" className={pillClass}>
          {pillLabel}
        </Badge>
        <Button
          type="button"
          variant={status === "failed" ? "outline" : "ghost"}
          size="sm"
          className="h-6 px-2 text-xs"
          onClick={onOpenProgress}
        >
          {actionLabel}
        </Button>
      </div>
    </div>
  )
}
