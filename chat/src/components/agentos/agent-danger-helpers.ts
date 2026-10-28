// F-2 danger zone: pure helpers (vitest). The server re-checks everything; the client only keeps the button off until the typed name is exact.
/** numeric agent id from the chat's graph id "agent-<n>" (null = not an agent id) */
export const agentNumId = (graphId: string): number | null => { const m = /^agent-(\d+)$/.exec(graphId); return m ? Number(m[1]) : null; };
/** permanent delete is armed only by the exact agent name (no trimming, case sensitive: the server compares the same way) */
export const nameMatches = (typed: string, name: string) => name.length > 0 && typed === name;
