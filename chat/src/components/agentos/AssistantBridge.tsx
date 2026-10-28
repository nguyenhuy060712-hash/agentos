// Tiny bridge: exposes the aui client (inside AssistantRuntimeProvider) to the component that renders the provider.
import { useMemo, useRef } from "react";
import { useAui } from "@assistant-ui/react";

export function useAuiBridge() {
  const holder = useRef<{ aui: ReturnType<typeof useAui> | null }>({ aui: null }).current;
  return useMemo(() => ({ get aui() { return holder.aui; }, Probe: function Probe() { holder.aui = useAui(); return null; } }), [holder]);
}
