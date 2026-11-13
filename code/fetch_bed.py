"""Download BLS Business Employment Dynamics (BED) for Maryland and record it in the external manifest (DATA_CONDITIONS C9).

BED = longitudinally linked QCEW establishments, with administrative changes (reporting-unit breakouts, consolidations,
UI account changes) removed: openings, closings, expansions, contractions, gross job gains and losses, births and deaths.
Maryland: private sector, quarterly, 12 industry groups (BED state data are at NAICS supersector level), seasonally
adjusted (S) and not (U); levels (L) and rates (R); employment (1) and number of establishments (2).

BLS refuses automated requests without a contact address. The address is read from the environment variable
BLS_CONTACT_EMAIL (never written into code or files), and sent only to download.bls.gov.

Saves <out_root>/bed/: bd.* metadata files (small, whole) and bed_md_<y0>-<y1>.csv.gz (Maryland rows of
bd.data.0.Current, streamed and filtered; the download's SHA-256 is recorded in MANIFEST.csv).
Usage: BLS_CONTACT_EMAIL=... python3 fetch_bed.py <out_root> [--from-year 2019]
"""
import argparse, csv, datetime, gzip, hashlib, os, sys, time, urllib.request

BASE = 'https://download.bls.gov/pub/time.series/bd/'
META = ['bd.txt', 'bd.series', 'bd.dataclass', 'bd.dataelement', 'bd.ratelevel', 'bd.industry', 'bd.state', 'bd.sizeclass',
        'bd.unitanalysis', 'bd.seasonal', 'bd.ownership', 'bd.periodicity', 'bd.footnote']
DATA = 'bd.data.0.Current'


def request(name, email):
    req = urllib.request.Request(BASE + name, headers={'User-Agent': f'research data request (contact: {email})', 'From': email})
    return urllib.request.urlopen(req, timeout=600)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('out_root'); ap.add_argument('--from-year', type=int, default=2019)
    a = ap.parse_args()
    email = os.environ.get('BLS_CONTACT_EMAIL')
    if not email: sys.exit('set BLS_CONTACT_EMAIL (BLS requires a contact address for automated downloads)')
    out = os.path.join(a.out_root, 'bed'); os.makedirs(out, exist_ok=True)
    man_path = os.path.join(a.out_root, 'MANIFEST.csv')
    rows = []
    now = lambda: datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    for name in META:
        data = request(name, email).read()
        if b'Access Denied' in data[:2000]: sys.exit(f'BLS refused {name}')
        open(os.path.join(out, name + '.part'), 'wb').write(data); os.replace(os.path.join(out, name + '.part'), os.path.join(out, name))
        rows.append(['BLS BED', BASE + name, f'bed/{name}', now(), len(data), hashlib.sha256(data).hexdigest(), '', '', ''])
        time.sleep(1.5)
    md = set()
    with open(os.path.join(out, 'bd.series')) as fh:
        for r in csv.DictReader(fh, delimiter='\t'):
            r = {k.strip(): v.strip() for k, v in r.items()}
            if r['state_code'] == '24': md.add(r['series_id'])
    y1 = a.from_year; total = kept = nbytes = 0; sha = hashlib.sha256()
    dest = os.path.join(out, f'bed_md_{a.from_year}-latest.csv.gz')
    with request(DATA, email) as resp, gzip.open(dest + '.part', 'wt', newline='') as fo:
        w = csv.writer(fo); w.writerow(['series_id', 'year', 'period', 'value', 'footnote_codes'])
        buf = b''; header = True
        while True:
            chunk = resp.read(1 << 20)
            if not chunk: break
            sha.update(chunk); nbytes += len(chunk)
            if header and b'Access Denied' in chunk[:2000]: sys.exit('BLS refused the data file')
            buf += chunk; lines = buf.split(b'\n'); buf = lines.pop()
            for ln in lines:
                if header: header = False; continue
                f = [x.strip() for x in ln.decode('utf-8', 'replace').split('\t')]
                if len(f) < 4: continue
                total += 1
                if f[0] in md and int(f[1]) >= a.from_year:
                    w.writerow(f[:5] if len(f) >= 5 else f + ['']); kept += 1; y1 = max(y1, int(f[1]))
    final = os.path.join(out, f'bed_md_{a.from_year}-{y1}.csv.gz')
    os.replace(dest + '.part', final)
    rows.append(['BLS BED', BASE + DATA, f'bed/{os.path.basename(final)}', now(), nbytes, sha.hexdigest(), total, kept,
                 f'state_code 24 (Maryland), year >= {a.from_year}'])
    new = not os.path.exists(man_path)
    with open(man_path, 'a', newline='') as mf:
        mw = csv.writer(mf)
        if new: mw.writerow(['source', 'url', 'local_file', 'downloaded_utc', 'bytes', 'sha256', 'rows_total', 'rows_kept', 'filter'])
        mw.writerows(rows)
    print(f'{len(md)} Maryland series; data rows {total:,}, kept {kept:,}; saved {final}')


if __name__ == '__main__':
    main()
