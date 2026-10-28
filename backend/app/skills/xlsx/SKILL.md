---
name: xlsx
description: Use when the user wants an Excel file (.xlsx) - tables, lists, sales or cost figures, data exported from an analysis. Dùng khi người dùng cần tệp Excel (.xlsx) - bảng số liệu, danh sách, doanh thu, chi phí, kết quả phân tích.
compatibility: Python 3 with openpyxl (installed in the Agent OS sandbox image). Run inside the sandbox or the virtual computer; no internet needed.
---

# Excel (.xlsx)

One `chay_code_python` call. Rows (first row = header) or a pandas DataFrame:

```python
import sys; sys.path.insert(0, "/memories/skills/xlsx/scripts"); from make_xlsx import build
rows = [["Chi nhánh", "Q2", "Q3"], ["Hà Nội", 1100, 1200], ["Đà Nẵng", 700, 860]]
print(build({"sheets": [{"name": "Doanh thu", "rows": rows, "total": True}]}, "doanh-thu-q3"))   # -> /workspace/out/doanh-thu-q3.xlsx
# from pandas: build(df, "ket-qua")   (several sheets: {"sheets": [{...}, {...}]})
```

- The helper is `/memories/skills/xlsx/scripts/make_xlsx.py` (read-only). In the sandbox just import it as above; in a virtual computer you may also read it or run it with `execute`: `python /memories/skills/xlsx/scripts/make_xlsx.py <spec.json> <name>`.
- Keep numbers as numbers (1200, not "1.200") so Excel can sum them; the helper formats `#,##0`. `"total": True` adds a SUM row (real formulas).
- Header is bold, first row frozen, column width fits the text, font Noto Sans (Vietnamese OK). Sheet name ≤ 31 chars.
- An attached Excel/CSV file is at `/tmp/<file name>`: `pd.read_excel("/tmp/x.xlsx")`, then write the result with `build(df, ...)`.
- The file shows up for the user as a download card; answer with a short summary, not the whole table.
- Limits: no internet, 512 MB RAM, 60 s per run.

## Edit, recalculate, check (existing workbooks)

`python /memories/skills/xlsx/scripts/xlsx_tool.py <command>` prints one JSON line (exit 0 = ok, 1 = problem found, 2 = failed).

| Command | Use |
|---|---|
| `inspect <file>` | sheets, size, formula count, `has_vba`, `needs_recalc`, error cells |
| `read <file> --sheet S --range A1:D20 --max-rows 50 [--formulas]` | look at cells (calculated values; `--formulas` shows the formulas) |
| `edit <file> --ops ops.json [--out P]` | apply operations, save a NEW file |
| `recalc <file>` | LibreOffice headless round trip (120 s limit) so formulas get values, then scans for errors |
| `validate <file>` | zip + reopen + error cells `#REF! #DIV/0! #VALUE! #NAME? #N/A` |
| `create <spec.json> <name>` | same as `make_xlsx.py` |

ops.json is a list: `{"op":"set","sheet":"S","cell":"B2","value":"=SUM(A1:A9)"}` (text starting with `=` is a formula), `{"op":"set_rows","sheet":"S","start":"A10","rows":[[..]]}`, `{"op":"insert_rows","sheet":"S","at":5,"count":1}` (formulas below the insert are NOT shifted - fix them with `set`), `{"op":"add_sheet","name":"N","rows":[[..]]}`, `{"op":"format","sheet":"S","range":"B2:B9","number_format":"#,##0","bold":true,"fill":"FFF2CC","width":14}`.

Rules:
1. Never overwrite the original. `edit` saves `/workspace/out/<name>_v2.xlsx` (or `.xlsm`) by default and refuses the input path. The file keeps its extension; `.xlsm` keeps its VBA macros.
2. After every `edit`: run `recalc <the _v2 file>`, then `validate`. Fix any error cell (`error_cells` lists sheet and cell) and repeat. Do not tell the user it is done while `validate` reports errors.
3. If `recalc` says "recalc failed, file structurally valid", the file still opens and Excel recalculates on open: tell the user that values were not verified.
4. To answer questions about the data use `read` or pandas (`pd.read_excel`), do not edit.
5. A chat upload cannot be `.xlsm`; macro workbooks arrive through the computer's files.
