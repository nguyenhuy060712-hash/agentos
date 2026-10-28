import { defineConfig } from "astro/config";
import tailwindcss from "@tailwindcss/vite";
import sitemap from "@astrojs/sitemap";

export default defineConfig({
  // Canonical/OG/sitemap base. Set SITE_URL at build time (e.g. the quick-tunnel URL until the domain exists).
  site: process.env.SITE_URL || "https://show-agentos.mozaai.one",
  trailingSlash: "always",
  build: { format: "directory", inlineStylesheets: "auto" },
  integrations: [sitemap({ filter: (page) => !/\/dao-tao\/(giang-vien|in|slide\/tat-ca)\//.test(page), i18n: { defaultLocale: "vi", locales: { vi: "vi-VN", en: "en-US" } } })],
  vite: { plugins: [tailwindcss()] },
});
