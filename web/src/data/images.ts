// Screenshot lookup by key: src/assets/f/*.webp (acceptance screenshots, see scripts/import-shots.mjs)
// and src/assets/shots/*.png (chat/admin screenshots from the earlier site).
const f = import.meta.glob<{ default: ImageMetadata }>("../assets/f/*.webp", { eager: true });
const s = import.meta.glob<{ default: ImageMetadata }>("../assets/shots/*.png", { eager: true });
const all: Record<string, ImageMetadata> = {};
for (const [p, m] of [...Object.entries(s), ...Object.entries(f)]) all[p.split("/").pop()!.replace(/\.(webp|png)$/, "")] = m.default;

export function shotSrc(key: string): ImageMetadata {
  const m = all[key];
  if (!m) throw new Error(`missing screenshot: ${key}`);
  return m;
}
export const hasShot = (key: string) => key in all;
