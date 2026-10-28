// GĐ9.P1: a closed chat tab must not cancel a running run.
// Root cause: the stream was opened with onDisconnect:"cancel", so ANY disconnect of the SSE (Stop button, tab closed, network drop) made the server cancel the run.
// Now the stream uses onDisconnect:"continue" (the server keeps running and stores every event, 9.6 resumable streams); only an explicit Stop calls cancel,
// and a re-opened thread waits for the still-running run (join) so the user sees its result.
type RunsApi = {
  cancel: (threadId: string, runId: string, wait?: boolean, action?: "interrupt" | "rollback") => Promise<unknown>;
  list: (threadId: string, o?: { limit?: number }) => Promise<Array<{ run_id?: string; status?: string }>>;
  join: (threadId: string, runId: string, o?: { signal?: AbortSignal }) => Promise<unknown>;
};
export type RunsClient = { runs: RunsApi };

/** Explicit Stop: cancel (interrupt) the run on the server. Never throws (the run may already be finished). */
export async function stopRun(client: RunsClient, threadId: string, runId: string | undefined): Promise<boolean> {
  if (!threadId) return false;
  try {
    // run id not seen yet (Stop pressed before the first event): cancel the thread's active run(s)
    const ids = runId ? [runId] : (await client.runs.list(threadId, { limit: 3 })).filter((r) => r.status === "running" || r.status === "pending").map((r) => r.run_id!);
    for (const id of ids) await client.runs.cancel(threadId, id, false, "interrupt");
    return ids.length > 0;
  } catch { return false; }
}

/** After re-opening a thread: if its latest run is still pending/running, wait for it (bounded) so the state that is loaded next contains the result. */
export async function awaitActiveRun(client: RunsClient, threadId: string, maxMs = 60_000): Promise<boolean> {
  try {
    const r = (await client.runs.list(threadId, { limit: 1 }))[0];
    if (!r?.run_id || (r.status !== "running" && r.status !== "pending")) return false;
    await client.runs.join(threadId, r.run_id, { signal: AbortSignal.timeout(maxMs) });
    return true;
  } catch { return false; }
}
