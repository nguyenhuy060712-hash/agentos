// GĐ5b: header label of the agent's chat model (pure, unit-tested)
/** "gpt-5.4-nano" -> "GPT-5.4 nano", "gemini-3.6-flash" -> "Gemini 3.6 Flash" (header label of the agent's chat model) */
export const modelName = (id: string | null | undefined) => {
  if (!id) return "";
  const m = /^gpt-([\d.]+)(?:-(.+))?$/i.exec(id); if (m) return `GPT-${m[1]}${m[2] ? ` ${m[2]}` : ""}`;
  return id.split(/[-_]/).map((p) => (/^\d/.test(p) ? p : p[0].toUpperCase() + p.slice(1))).join(" ").replace(/(\d) (\d)/g, "$1.$2");
};
