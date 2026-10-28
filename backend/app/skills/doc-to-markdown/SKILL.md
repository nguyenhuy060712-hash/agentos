---
name: doc-to-markdown
description: Use when the user gives a document (Word .doc/.docx/.odt/.rtf, Excel .xls/.xlsx/.ods, PowerPoint .ppt/.pptx/.odp, PDF, EPUB, HTML) and you need its text as Markdown - to read, summarise, compare or search it. Dùng khi người dùng đưa tệp tài liệu (Word, Excel, PowerPoint, PDF, EPUB, HTML) và cần đọc nội dung dạng Markdown để tóm tắt, so sánh, tìm kiếm.
compatibility: Python 3 with MarkItDown, LibreOffice and pandoc (installed in the Agent OS virtual computer image). Run with execute in the virtual computer; no internet needed.
---

# Tài liệu -> Markdown

Tệp người dùng gửi nằm trong `/workspace/uploads/`. Chạy một lệnh bằng `execute`:

```bash
python /memories/skills/doc-to-markdown/scripts/to_md.py /workspace/uploads/bao-cao.doc bao-cao
# -> /workspace/out/bao-cao.md  (pages=12, tables=3, chars=20456)
```

- Tham số thứ hai (tên tệp ra) có thể bỏ; mặc định lấy tên tệp gốc. Kết quả luôn là `/workspace/out/<tên>.md`; đọc nó bằng `read_file`, đừng in cả tệp dài ra.
- Loại tệp: `.doc .odt .rtf .xls .ods .ppt .odp` được LibreOffice đổi sang định dạng mới rồi mới sang Markdown; `.docx .xlsx .pptx .pdf .epub .html` đi thẳng; RTF/EPUB hỏng thì dùng pandoc.
- Dòng cuối là tóm tắt: số trang (PDF), số bảng, số ký tự. Hãy báo lại cho người dùng.
- **PDF bản quét** (ảnh, không có chữ): lệnh thoát với mã 2 và in thông báo "cần OCR". Đừng tự đoán nội dung; hãy nói rõ với người dùng rằng tệp là bản quét nên cần OCR.
- Lỗi khác (tệp lạ, hỏng): lệnh in `ERROR: ...` và thoát mã 1; đọc thông báo rồi báo lại, đừng thử lại vô hạn.
- Giới hạn: không có internet, 60 s mỗi lệnh (tệp lớn đổi bằng LibreOffice có thể lâu hơn: chạy nền với `&` rồi hỏi lại).
