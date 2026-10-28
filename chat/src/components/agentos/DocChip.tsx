// GD6.8: chip for a document attachment: icon per file type, name, size, page/sheet/slide count when known, error state.
import { AlertCircleIcon, FileJson, FileSpreadsheet, FileText, Loader2Icon, Presentation } from "lucide-react";
import type { FC } from "react";
import { formatSize, unitLabel, type DocKind } from "@/runtime/attachments";

export const DOC_ICONS = { pdf: FileText, docx: FileText, txt: FileText, md: FileText, xlsx: FileSpreadsheet, csv: FileSpreadsheet, pptx: Presentation, json: FileJson } as const;

export type DocChipProps = { name: string; kind: DocKind; size?: number; pages?: number | null; units?: string | null; error?: string | null; uploading?: boolean };

export const DocChip: FC<DocChipProps> = ({ name, kind, size, pages, units, error, uploading }) => {
  const Icon = error ? AlertCircleIcon : uploading ? Loader2Icon : DOC_ICONS[kind];
  const sub = error ?? [size != null ? formatSize(size) : "", unitLabel(units, pages)].filter(Boolean).join(" · ");
  return (
    <div data-testid="doc-chip" data-kind={kind} data-error={error ? "1" : undefined} title={error ?? name}
      className={"bg-muted flex h-14 max-w-56 items-center gap-2 rounded-xl px-3 text-sm ring-1 ring-black/10 dark:ring-white/10" + (error ? " ring-destructive/60" : "")}>
      <Icon className={"size-5 shrink-0 " + (error ? "text-destructive" : "text-muted-foreground") + (uploading ? " animate-spin" : "")} aria-hidden="true" />
      <span className="min-w-0">
        <span className="block truncate font-medium">{name}</span>
        <span className={"block truncate text-xs " + (error ? "text-destructive" : "text-muted-foreground")}>{sub}</span>
      </span>
    </div>
  );
};
