// GĐ8.6 file cards: files the agent wrote to /workspace/out in the sandbox (Word / Excel / PowerPoint / PDF / images / data), stored by
// the server as chat files (/api/files/<id>). Card = icon, name, size, Download; inline preview for PDF (iframe ?inline=1) and images.
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { DownloadIcon, EyeIcon, EyeOffIcon, FileIcon, FileImageIcon, FileSpreadsheetIcon, FileTextIcon, FileTypeIcon, PresentationIcon } from "lucide-react";
import { baseName, canPreview, fileKind, fileUrl, fmtSize, type FileKind, type OutFile } from "./files-helpers";

const ICON: Record<FileKind, typeof FileIcon> = { pdf: FileTypeIcon, image: FileImageIcon, word: FileTextIcon, excel: FileSpreadsheetIcon, slides: PresentationIcon, data: FileTextIcon, other: FileIcon };
const TINT: Record<FileKind, string> = { pdf: "bg-red-500/10 text-red-600", image: "bg-violet-500/10 text-violet-600", word: "bg-blue-500/10 text-blue-600",
  excel: "bg-emerald-500/10 text-emerald-600", slides: "bg-orange-500/10 text-orange-600", data: "bg-muted text-muted-foreground", other: "bg-muted text-muted-foreground" };

export function FileCard({ f, compact = false }: { f: OutFile; compact?: boolean }) {
  const { t, i18n } = useTranslation();
  const [show, setShow] = useState(false);
  const kind = fileKind(f); const Icon = ICON[kind]; const name = baseName(f.name);
  const btn = "hover:bg-muted text-muted-foreground hover:text-foreground inline-flex size-8 shrink-0 items-center justify-center rounded-lg";
  return (
    <div data-testid="file-card" data-kind={kind} data-file={f.id} className={`bg-background w-full overflow-hidden rounded-xl border ${compact ? "" : "max-w-md"}`}>
      <div className="flex items-center gap-3 p-2.5">
        <span className={`flex size-9 shrink-0 items-center justify-center rounded-lg ${TINT[kind]}`}><Icon className="size-4.5" aria-hidden /></span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium" title={f.name} data-testid="file-name">{name}</span>
          <span className="text-muted-foreground block truncate text-xs" data-testid="file-size">{fmtSize(f.size, i18n.language === "en" ? "en" : "vi")}{name !== f.name ? ` · ${f.name}` : ""}</span>
        </span>
        {canPreview(f) && (
          <button type="button" className={btn} aria-pressed={show} aria-label={show ? t("fileHide") : t("filePreview")} title={show ? t("fileHide") : t("filePreview")}
            data-testid="file-preview" onClick={() => setShow((x) => !x)}>{show ? <EyeOffIcon className="size-4" /> : <EyeIcon className="size-4" />}</button>)}
        <a className={btn} href={fileUrl(f)} download={name} aria-label={`${t("fileDownload")} ${name}`} title={t("fileDownload")} data-testid="file-download"><DownloadIcon className="size-4" /></a>
      </div>
      {show && kind === "pdf" && <iframe src={fileUrl(f, true)} title={name} className="block h-96 w-full border-t bg-white" data-testid="file-pdf" />}
      {show && kind === "image" && <img src={fileUrl(f)} alt={name} loading="lazy" className="block h-auto max-h-96 w-full border-t bg-white object-contain" data-testid="file-img" />}
    </div>);
}

export function FileCards({ files }: { files: OutFile[] }) {
  if (!files.length) return null;
  return <div className="grid gap-2" data-testid="file-cards">{files.map((f) => <FileCard key={f.id} f={f} />)}</div>;
}
