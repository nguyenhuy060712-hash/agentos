// Writes the `present` tool contract (description + JSON schema) for the Agent OS backend tool `local:present` (genui.py).
// Run: npx tsx scripts/export-present-schema.ts <out.json>. The UI and the server then share ONE schema.
import { writeFileSync } from "node:fs";
import { JSONGenerativeUI } from "@assistant-ui/generative-ui/react";
import { library } from "../src/genui/library";
const tool = new JSONGenerativeUI({ library }).present();
const out = { generated_by: "agentos-chat scripts/export-present-schema.ts", package: "@assistant-ui/generative-ui@0.0.6",
  components: Object.keys(library), description: tool.description, parameters: tool.parameters };
writeFileSync(process.argv[2] ?? "present_schema.json", JSON.stringify(out, null, 1));
console.log(`components=${out.components.length} bytes=${JSON.stringify(out).length}`);
