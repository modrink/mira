import { Navigate, useSearchParams } from "react-router"

/** Legacy /learnings → Pending/Active Rules. */
export function LearningsRedirect() {
  const [params] = useSearchParams()
  const tab = params.get("tab")
  const next = new URLSearchParams()
  if (tab === "approved") {
    next.set("kind", "learned")
  } else {
    next.set("mode", "pending")
  }
  const qs = next.toString()
  return <Navigate to={qs ? `/rules?${qs}` : "/rules?mode=pending"} replace />
}

/** Legacy /learnings/new|edit → /rules/new|edit (Add = written; learned only via edit). */
export function LearningsFormRedirect({ mode }: { mode: "new" | "edit" }) {
  const [params] = useSearchParams()
  const next = new URLSearchParams(params)
  if (mode === "edit") {
    if (!next.get("kind")) next.set("kind", "learned")
  } else {
    next.delete("kind")
  }
  const qs = next.toString()
  return (
    <Navigate
      to={mode === "new" ? `/rules/new${qs ? `?${qs}` : ""}` : `/rules/edit?${qs}`}
      replace
    />
  )
}
