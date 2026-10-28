// Copies the acceptance screenshots used on /tinh-nang/ and /hoc/ from agentos-local/docs/anh_nghiem_thu
// into src/assets/f/<key>.webp (max 1600 px wide, WebP q80). Astro then builds responsive sizes from them.
// Usage: node scripts/import-shots.mjs   (re-run after new screenshots; output is committed)
import sharp from "sharp";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const SRC = process.env.SHOTS_SRC || path.join(root, "..", "agentos-local", "docs", "anh_nghiem_thu");
const OUT = path.join(root, "src", "assets", "f");

// key: source file (Vietnamese UI). "-en" keys hold the English UI variant when one exists.
const map = {
  "login": "1.2_trang_dang_nhap_vi.png",
  "2fa": "1.3_dang_nhap_2_lop_thiet_lap_vi.png",
  "deep-research": "2.4_nghien_cuu_sau_ke_hoach_vi.png",
  "canvas-doc": "2.5_canvas_tai_lieu_vi.png",
  "channel-tab": "8.1-8.2_tab_kenh_mo_phong.png",
  "telegram": "8.2_telegram_tro_chuyen.png",
  "zalo-oa": "8.3_zalo_tro_chuyen_duyet.png",
  "zalo-personal": "8.3b_canh_bao_va_qr.png",
  "slack": "8.4_slack_tro_chuyen_duyet.png",
  "gmail-approve": "8.5_duyet_truoc_khi_gui_mail.png",
  "file-card": "8.6_the_file_trong_chat.png",
  "builder-chat": "8.7_tom_tat_va_da_tao.png",
  "builder-chat-en": "8.P_tao_agent_bang_tro_chuyen_en.png",
  "templates": "8.8_mau_theo_nganh.png",
  "agent-mcp": "8.8_mcp_rieng_agent.png",
  "project": "8.9_du_an_tra_loi_dung_chi_dan_va_tep.png",
  "cap-banner": "8.P_banner_tran_chi_phi_vi.png",
  "cap-banner-en": "8.P_banner_tran_chi_phi_en.png",
  "google-m365": "8.P_cai_dat_google_m365_vi.png",
  "google-m365-en": "8.P_cai_dat_google_m365_en.png",
  "env-compare": "9.10_so_sanh_moi_truong.png",
  "otlp-trace": "9.10_trace_otlp_genai.png",
  "agent-config": "9.1_agent_ghim_tag.png",
  "packs": "9.1_danh_sach_goi.png",
  "pack-history": "9.1_lich_su_commit_tag.png",
  "pack-history-en": "9.1_lich_su_commit_tag_en.png",
  "pack-diff": "9.1_so_sanh_diff.png",
  "prompt-commit": "9.2_canvas_da_luu_commit_moi.png",
  "prompt-commit-en": "9.2_canvas_da_luu_commit_moi_en.png",
  "prompt-canvas": "9.2_canvas_thao_tac_nhanh_va_khac_biet.png",
  "prompt-canvas-en": "9.2_canvas_thao_tac_nhanh_va_khac_biet_en.png",
  "pg-compare": "9.3_cau_hinh_model_so_sanh_song_song.png",
  "pg-compare-en": "9.3_cau_hinh_model_so_sanh_song_song_en.png",
  "pg-schema": "9.3_dau_ra_cau_truc_kiem_tra_schema.png",
  "pg-schema-en": "9.3_dau_ra_cau_truc_kiem_tra_schema_en.png",
  "trace-trajectory": "9.4_trajectory.png",
  "trace-search": "9.4_tim_trong_trace.png",
  "studio-timeline": "9.5_dong_thoi_gian_checkpoint.png",
  "studio-timeline-en": "9.5_dong_thoi_gian_checkpoint_en.png",
  "studio-memory": "9.5_bo_nho.png",
  "studio-memory-en": "9.5_bo_nho_en.png",
  "a2a": "9.6_a2a_the_agent_va_ket_qua.png",
  "agent-chat-ui": "9.6_agent_chat_ui_cho_duyet.png",
  "mcp-tools": "9.6_mcp_danh_sach_tool.png",
  "encryption": "9.7_dong_that_trong_DB_va_man_hinh.png",
  "privacy": "9.8_cai_dat_quyen_rieng_tu.png",
  "my-data": "9.8_du_lieu_cua_toi.png",
  "parquet": "9.9_cai_dat_xuat_parquet.png",
  "share-group": "9.P1_chia_se_cho_nhom_vi.png",
  "share-group-en": "9.P1_chia_se_cho_nhom.png",
  "groups": "9.P1_quan_tri_nhom_vi.png",
  "groups-en": "9.P1_quan_tri_nhom.png",
  "agent-cap": "9.P1_the_gioi_han_chi_phi_agent_vi.png",
  "agent-cap-en": "9.P1_the_gioi_han_chi_phi_agent.png",
  "ocr-mask": "9.P2_ocr_goc_va_da_che.png",
  "eval-composite": "9.Q_danh_gia_composite_builder_vi.png",
  "eval-composite-en": "9.Q_danh_gia_composite_builder.png",
  "eval-dataset": "9.Q_danh_gia_dataset_schema_bien_doi_vi.png",
  "eval-dataset-en": "9.Q_danh_gia_dataset_schema_bien_doi.png",
  "eval-gate": "9.Q_danh_gia_decision_gate_vi.png",
  "eval-gate-en": "9.Q_danh_gia_decision_gate.png",
  "eval-graph": "9.Q_danh_gia_graph_experiment_vi.png",
  "eval-graph-en": "9.Q_danh_gia_graph_experiment.png",
  "eval-rag": "9.Q_danh_gia_rag_mau_hop.png",
  "eval-retry": "9.Q_danh_gia_retry_sau_vi.png",
  "eval-retry-en": "9.Q_danh_gia_retry_sau.png",
  "eval-results": "9.Q_danh_gia_composite_ket_qua_vi.png",
  "eval-results-en": "9.Q_danh_gia_composite_ket_qua.png",
  "sso": "9.Q_may_chu_dang_nhap_nha_cung_cap.png",
  "agent-identity": "9.Q_may_chu_danh_tinh_agent_audit.png",
  "gw-policy": "9.Q_may_chu_gateway_chinh_sach.png",
  "gw-routing": "9.Q_may_chu_gateway_dinh_tuyen.png",
  "server-headers": "9.Q_may_chu_header_va_nhat_ky.png",
  "sandbox": "9.Q_may_chu_sandbox_snapshot.png",
  "custom-apps": "9.Q_may_chu_ung_dung_tuy_chinh.png",
  "custom-auth": "9.Q_may_chu_xac_thuc_tuy_chinh.png",
};

fs.mkdirSync(OUT, { recursive: true });
let n = 0, bytes = 0;
for (const [key, file] of Object.entries(map)) {
  const src = path.join(SRC, file);
  if (!fs.existsSync(src)) { console.error("missing", file); process.exitCode = 1; continue; }
  const dst = path.join(OUT, key + ".webp");
  await sharp(src).resize({ width: 1600, withoutEnlargement: true }).webp({ quality: 80, effort: 6 }).toFile(dst);
  bytes += fs.statSync(dst).size; n++;
}
console.log(`${n} images -> ${path.relative(root, OUT)} (${(bytes / 1048576).toFixed(1)} MB)`);
