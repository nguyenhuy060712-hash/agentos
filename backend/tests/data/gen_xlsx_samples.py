"""Builds the 11.65 sample workbooks (needs openpyxl, so it runs INSIDE the box: python3 gen_xlsx_samples.py <outdir>).
sample_formulas.xlsx  Vietnamese sheet with SUM / ratio formulas
sample_ref.xlsx       a planted #REF! formula
sample_macro.xlsm     formulas + a (fake, tiny) xl/vbaProject.bin so the test can check the VBA part survives"""
import os, sys, zipfile, shutil
from openpyxl import Workbook

out = sys.argv[1] if len(sys.argv) > 1 else "."
os.makedirs(out, exist_ok=True)

def formulas_book():
    wb = Workbook(); ws = wb.active; ws.title = "Doanh thu"
    ws.append(["Chi nhánh", "Q2", "Q3", "Tăng trưởng"])
    for i, (n, a, b) in enumerate([("Hà Nội", 1100, 1200), ("Đà Nẵng", 700, 860), ("TP. Hồ Chí Minh", 1500, 1710)], 2):
        ws.append([n, a, b, f"=C{i}/B{i}-1"])
    ws.append(["Tổng", "=SUM(B2:B4)", "=SUM(C2:C4)", "=C5/B5-1"])
    return wb

wb = formulas_book(); wb.save(os.path.join(out, "sample_formulas.xlsx"))
wb = formulas_book(); wb["Doanh thu"]["E2"] = "=#REF!+1"; wb.save(os.path.join(out, "sample_ref.xlsx"))

tmp = os.path.join(out, "_m.xlsx"); formulas_book().save(tmp); dst = os.path.join(out, "sample_macro.xlsm")
with zipfile.ZipFile(tmp) as zi, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
    for it in zi.infolist():
        data = zi.read(it.filename)
        if it.filename == "[Content_Types].xml":
            s = data.decode("utf-8").replace("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml", "application/vnd.ms-excel.sheet.macroEnabled.main+xml")
            data = s.replace("</Types>", '<Override PartName="/xl/vbaProject.bin" ContentType="application/vnd.ms-office.vbaProject"/></Types>').encode("utf-8")
        elif it.filename == "xl/_rels/workbook.xml.rels":
            data = data.decode("utf-8").replace("</Relationships>", '<Relationship Id="rIdVba" Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject" Target="vbaProject.bin"/></Relationships>').encode("utf-8")
        zo.writestr(it, data)
    zo.writestr("xl/vbaProject.bin", b"\xd0\xcf\x11\xe0AOS-FAKE-VBA-PROJECT" + b"\x00" * 64)
os.remove(tmp)
print("ok", sorted(os.listdir(out)))
