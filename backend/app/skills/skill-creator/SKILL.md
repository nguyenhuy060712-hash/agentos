---
name: skill-creator
description: Use when the user wants to turn a way of working into a reusable Skill (viết skill, tạo skill, lưu cách làm này thành skill). Dùng khi cần soạn một Skill mới từ mô tả, từ cuộc trò chuyện hoặc từ một lần sửa lỗi.
compatibility: Text only; no tools or computer needed.
---

# Skill creator / Soạn Skill

A Skill is a folder with one `SKILL.md`: a short YAML header (`name`, `description`) and a Markdown body that tells the agent HOW to do one kind of task.
Một Skill là một thư mục có `SKILL.md`: phần đầu YAML (`name`, `description`) và phần nội dung Markdown hướng dẫn agent làm MỘT loại việc.

## Rules / Quy tắc
1. `name`: lowercase letters, digits and hyphens, 1-64 characters (e.g. `bao-gia`). / Tên: chữ thường, số, gạch ngang.
2. `description` (max 1,024 chars): say WHAT it does and WHEN to use it. Start with "Dùng khi…" and also "Use when…"; include Vietnamese and English keywords so the agent can match it.
3. Body: numbered steps, concrete (what to ask, what to check, what the answer looks like). Under 500 lines. Same language as the user; short sentences.
4. Put long tables or templates in `references/<file>.md` and link them from the body, e.g. `[bảng giá](references/gia.md)`. One level deep only.
5. NEVER put passwords, API keys, tokens or personal data in a Skill.
6. End the body with one example request the user can try ("Thử: …").
7. One Skill = one job. If the material covers two jobs, write the main one and mention the other in the last line.

## Output / Kết quả
Return ONLY JSON: `{"name": "...", "description": "...", "body": "...", "files": {"references/x.md": "..."}}` (`files` may be empty).
