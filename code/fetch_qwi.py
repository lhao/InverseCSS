"""Download QWI files from LEHD and keep only the years in a window.

Each LEHD file covers the full history (1990 onward), so the file is streamed,
its md5 is checked against LEHD's published checksum (of the uncompressed file),
and only rows with year in [first, last] are written, gzip-compressed and
otherwise unchanged.

Usage:
  python3 fetch_qwi.py <state> <first_year> <last_year> <out_dir> <tab> [<tab> ...]
  tab = "<demo>_<firm>_<geo>_<ind>_<own>", e.g. sa_f_gs_n6_op
Example:
  python3 fetch_qwi.py md 2021 2024 data/raw/qwi/R2026Q3 sa_f_gs_n6_op se_fa_gs_n6_op
"""
import gzip, hashlib, os, sys, urllib.request, zlib

BASE = 'https://lehd.ces.census.gov/data/qwi/latest_release/{st}/'
UA = {'User-Agent': 'Mozilla/5.0 (research data request)'}


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120)


def checksums(st):
    out = {}
    for line in get(BASE.format(st=st) + f'qwi_{st}.md5sum').read().decode().splitlines():
        md5, name = line.split()
        out[name] = md5
    return out


def fetch(st, first, last, out_dir, tab, sums):
    name = f'qwi_{st}_{tab}_u.csv'
    out = os.path.join(out_dir, f'qwi_{st}_{tab}_u_{first}-{last}.csv.gz')
    if os.path.exists(out):
        print('exists, skipped:', out); return
    md5, dec = hashlib.md5(), zlib.decompressobj(16 + zlib.MAX_WBITS)
    buf, header, kept, total = b'', None, 0, 0
    tmp = out + '.part'
    with get(BASE.format(st=st) + name + '.gz') as r, gzip.open(tmp, 'wb') as w:
        while True:
            chunk = r.read(1 << 20)
            data = dec.decompress(chunk) if chunk else dec.flush()
            md5.update(data); buf += data
            lines = buf.split(b'\n'); buf = lines.pop()
            for ln in lines:
                if header is None:
                    header = ln.split(b','); yi = header.index(b'year'); w.write(ln + b'\n'); continue
                total += 1
                if first <= int(ln.split(b',', yi + 1)[yi]) <= last:
                    w.write(ln + b'\n'); kept += 1
            if not chunk: break
    if sums.get(name) != md5.hexdigest():
        os.remove(tmp); raise SystemExit(f'md5 mismatch for {name}: {md5.hexdigest()} vs {sums.get(name)}')
    os.rename(tmp, out)
    print(f'{name}: md5 ok, kept {kept:,} of {total:,} rows -> {os.path.basename(out)}')


if __name__ == '__main__':
    st, first, last, out_dir, *tabs = sys.argv[1:]
    os.makedirs(out_dir, exist_ok=True)
    sums = checksums(st)
    for f in ('version_qwi.txt', f'qwi_{st}.md5sum'):
        open(os.path.join(out_dir, f), 'wb').write(get(BASE.format(st=st) + f).read())
    for tab in tabs:
        fetch(st, int(first), int(last), out_dir, tab, sums)
