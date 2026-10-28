// GĐ3.1: tool UIs as ONE toolkit (assistant-ui toolkit API, replaces makeAssistantToolUI). All entries are render-only
// `type: "backend"`: the tools run in Agent OS (Deep Agents); the browser only draws their calls. No schema is uploaded.
import { defineToolkit, type Toolkit, type ToolCallMessagePartProps } from "@assistant-ui/react";
import { JSONGenerativeUI } from "@assistant-ui/generative-ui/react";
import { library } from "./library";
import { TodoCard } from "./TodoCard";
import { FetchUrlCard, WebSearchCard } from "./SourcesCard";
import { TaskCard } from "@/components/agentos/TaskToolUI";
import { MemoryChip } from "./MemoryChip";
import { AskCard } from "./AskCard";
import { ThinkCard } from "./ThinkCard";
import { CanvasCard, CodeRunCard } from "./CanvasCards";
import { FileDiffCard } from "./FileDiffCard";

const generative = new JSONGenerativeUI({ library });
const present = generative.present();

type P = ToolCallMessagePartProps<Record<string, unknown>, unknown>;

export function makeToolkit(opts: { genui: boolean }): Toolkit {
  return defineToolkit({
    // generative UI: the model's `present` args ARE the UI tree (render-only on the server too, see genui.py)
    present: { type: "backend", display: "standalone",
      render: opts.genui ? present.render : ({ args }: P) => <pre className="aos-gen-off" data-testid="genui-off">{JSON.stringify(args, null, 1).slice(0, 2000)}</pre> },
    write_todos: { type: "backend", display: "standalone", render: ({ args, toolCallId }: P) => <TodoCard args={args as never} toolCallId={toolCallId} /> },
    web_search: { type: "backend", display: "standalone", render: ({ args, result, status }: P) => <WebSearchCard args={args as never} result={result} status={status} /> },
    fetch_url: { type: "backend", render: ({ args, result }: P) => <FetchUrlCard args={args as never} result={result} /> },
    task: { type: "backend", render: ({ args, result, status }: P) => <TaskCard args={args as never} result={result} status={status} /> },
    think_tool: { type: "backend", render: ({ args }: P) => <ThinkCard args={args as never} /> },   // GĐ7.7 deep research
    // GĐ5a: long-term memory chip ("em đã ghi nhớ", forget in place) and the ask-for-info form (interrupt -> resume with answers)
    luu_ghi_nho: { type: "backend", display: "standalone", render: ({ args, result }: P) => <MemoryChip args={args as never} result={result} /> },
    hoi_thong_tin: { type: "backend", display: "standalone", render: ({ args, result, toolCallId }: P) => <AskCard args={args as never} result={result} toolCallId={toolCallId} /> },
    // GĐ5b: canvas (side panel) and Python runs in the gVisor sandbox (output + charts)
    canvas: { type: "backend", display: "standalone", render: ({ args, result, status }: P) => <CanvasCard args={args as never} result={result} status={status} /> },
    // GĐ11.52: file edits in the virtual computer as a coloured line diff
    write_file: { type: "backend", display: "standalone", render: ({ args, result }: P) => <FileDiffCard tool="write_file" args={args as never} result={result} /> },
    edit_file: { type: "backend", display: "standalone", render: ({ args, result }: P) => <FileDiffCard tool="edit_file" args={args as never} result={result} /> },
    chay_code_python: { type: "backend", display: "standalone", render: ({ args, result }: P) => <CodeRunCard args={args as never} result={result} /> },
  } as never);
}
