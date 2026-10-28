// GĐ7.7 deep research: the researcher's think_tool reflection as one collapsed row (open to read it).
import { useTranslation } from "react-i18next";

export function ThinkCard({ args }: { args: { reflection?: string; thought?: string } }) {
  const { t } = useTranslation();
  const text = args.reflection ?? args.thought ?? "";
  return (
    <details data-testid="think-row" className="text-muted-foreground my-1 text-xs">
      <summary className="cursor-pointer select-none">💭 {t("thinkNote")}</summary>
      <p className="mt-1 whitespace-pre-wrap border-l pl-3">{text}</p>
    </details>
  );
}
