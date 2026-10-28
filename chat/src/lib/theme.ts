// Light/dark theme (design tokens in index.css: :root / .dark). Stored per browser; "system" follows prefers-color-scheme.
import { useEffect, useState } from "react";
export type Theme = "light" | "dark" | "system";
const KEY = "aos_theme";
export const getTheme = (): Theme => (localStorage.getItem(KEY) as Theme) || "system";
export function applyTheme(t: Theme = getTheme()) {
  const dark = t === "dark" || (t === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark); document.documentElement.style.colorScheme = dark ? "dark" : "light";
}
export function useTheme(): [Theme, (t: Theme) => void] {
  const [t, setT] = useState<Theme>(getTheme);
  useEffect(() => { applyTheme(t); const m = matchMedia("(prefers-color-scheme: dark)"); const f = () => applyTheme(t); m.addEventListener("change", f); return () => m.removeEventListener("change", f); }, [t]);
  return [t, (n) => { localStorage.setItem(KEY, n); setT(n); }];
}
/** Developer panel (debug log, MOCK badge, cost, mock mic) — off the chat surface unless turned on in Settings > General. */
export const getDev = () => localStorage.getItem("aos_dev") === "1";
export const setDevStored = (v: boolean) => localStorage.setItem("aos_dev", v ? "1" : "0");
