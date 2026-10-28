import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const API = process.env.AOS_API ?? "http://127.0.0.1:8020";

// Static SPA served under /chat/ (same origin as Agent OS). No Node server at runtime.
export default defineConfig({
  base: "/chat/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: API, changeOrigin: false },
      "/login": { target: API, changeOrigin: false },
    },
  },
  build: {
    sourcemap: !!process.env.AOS_SOURCEMAP, chunkSizeWarningLimit: 1500,
    // vendor chunks (cache-friendly, none > 1.5 MB); voice + Settings are lazy chunks (React.lazy in App.tsx)
    rolldownOptions: { output: { codeSplitting: { groups: [
      // zod + src/lib/zod-csp.ts in one chunk: inlined into the entry it would run AFTER the vendor chunks (assistant-ui builds
      // zod schemas at module load); here jitless is set as soon as zod loads, before any schema exists
      { name: "vendor-zod", test: /(node_modules[\\/]zod[\\/]|src[\\/]lib[\\/]zod-csp\.ts)/, priority: 35 },
      { name: "vendor-react", test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/, priority: 30 },
      { name: "vendor-markdown", test: /node_modules[\\/](@assistant-ui[\\/]react-markdown|react-markdown|remark-|micromark|mdast-|unified|hast-|unist-|vfile|property-information|space-separated|comma-separated|decode-named|character-|ccount|markdown-table|trim-lines|devlop|bail|is-plain-obj|zwitch|longest-streak|html-url|estree|style-to)/, priority: 25 },
      { name: "vendor-assistant-ui", test: /node_modules[\\/](@assistant-ui|assistant-stream|assistant-cloud)[\\/]/, priority: 20 },
      { name: "vendor-langgraph", test: /node_modules[\\/]@langchain[\\/]/, priority: 20 },
      { name: "vendor-ui", test: /node_modules[\\/](@base-ui|@floating-ui|lucide-react|radix-ui|@radix-ui)[\\/]/, priority: 15 },
      // GĐ3: lazy-only libraries (Mermaid, Shiki, Recharts and their deps) stay out of the eager vendor chunk -> own lazy chunks
      { name: "vendor", test: /node_modules[\\/](?!(mermaid|@mermaid-js|shiki|@shikijs|recharts|d3|d3-[a-z-]+|internmap|delaunator|robust-predicates|dagre-d3-es|cytoscape[a-z-]*|khroma|langium|chevrotain|@chevrotain|roughjs|lodash-es|dayjs|stylis|marked|ts-dedent|@iconify|@braintree|dompurify|vscode-[a-z-]+|es-toolkit|immer|reselect|redux|@reduxjs|react-redux|eventemitter3|victory-vendor|decimal\.js-light|tiny-invariant|@upsetjs|layout-base|cose-base|katex|oniguruma-to-es|regex|regex-recursion|regex-utilities|hast-util-to-html|@antfu|@types|elkjs|oniguruma-parser|redux-thunk)[\\/])/, priority: 10 },
    ] } } },
  },
});
