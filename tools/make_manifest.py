"""Write docs/data_manifest.csv: one row per raw input file (path, bytes, SHA-256, file date).
No data is copied: the hashes let anyone check that a file they download matches the one we used.
File date = modification time in the project Drive (the download date, or the copy date for files downloaded elsewhere first).
Usage: python3 tools/make_manifest.py [path/to/data/raw]   (default ~/InverseCSS/data/raw)"""
import csv, hashlib, os, sys, datetime as dt
raw = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else '~/InverseCSS/data/raw')
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'docs', 'data_manifest.csv')
os.makedirs(os.path.dirname(out), exist_ok=True)
rows = []
for d, _, fs in os.walk(raw):
    for f in sorted(fs):
        if f.startswith('.'): continue
        p = os.path.join(d, f); rel = os.path.relpath(p, raw)
        h = hashlib.sha256()
        with open(p, 'rb') as fh:
            for b in iter(lambda: fh.read(1 << 20), b''): h.update(b)
        st = os.stat(p)
        parts = rel.split(os.sep)
        src = parts[0] + '/' + parts[1] if parts[0] == 'external' and len(parts) > 2 else parts[0]
        rows.append([src,
                     rel, st.st_size, h.hexdigest(), dt.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M')])
rows.sort(key=lambda r: r[1])
with open(out, 'w', newline='') as fh:
    w = csv.writer(fh); w.writerow(['source', 'path', 'bytes', 'sha256', 'file_date_local']); w.writerows(rows)
print(len(rows), 'files ->', os.path.normpath(out))
