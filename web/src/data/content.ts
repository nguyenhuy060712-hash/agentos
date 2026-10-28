// Product links are configurable at build time (PUBLIC_AGENTOS_URL, PUBLIC_CHAT_URL); defaults are the planned domains.
const env = import.meta.env ?? {};
export const LINKS = {
  agentos: env.PUBLIC_AGENTOS_URL || "https://agentos.mozaai.one",
  chat: env.PUBLIC_CHAT_URL || "https://agentoschat.mozaai.one",
  mozaai: "https://mozaai.vn",
};
