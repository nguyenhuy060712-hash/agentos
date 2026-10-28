"""Scan files for the Gemini key WITHOUT knowing it: only its SHA-256 (env KEY_SHA256) is given; every 53-char window
starting with 'AQ.' is hashed and compared. Usage: KEY_SHA256=… python3 scripts/leakscan.py <paths…>"""
import hashlib, os, sys, pathlib
H = os.environ["KEY_SHA256"]; hits = 0; files = 0; n = 0
for root in sys.argv[1:]:
    for p in (pathlib.Path(root).rglob("*") if pathlib.Path(root).is_dir() else [pathlib.Path(root)]):
        if not p.is_file() or ".venv" in p.parts or "node_modules" in p.parts or p.stat().st_size > 50_000_000: continue
        b = p.read_bytes(); files += 1; i = b.find(b"AQ.")
        while i >= 0:
            n += 1
            if hashlib.sha256(b[i:i + 53]).hexdigest() == H: hits += 1; print("LEAK in", p)
            i = b.find(b"AQ.", i + 1)
print(f"scanned {files} files, {n} 'AQ.' windows, key hits: {hits}")
