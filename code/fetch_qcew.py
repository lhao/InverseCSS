"""Download QCEW area files (all industries, all aggregation levels) from the BLS open-data API.

Usage:
  python3 fetch_qcew.py <first_year> <last_year> <out_root> <area_fips> [<area_fips> ...]
Writes <out_root>/<year>q<qtr>/<area_fips>.csv, unchanged from BLS.
Quarters not yet published (HTTP 404) are reported and skipped.
"""
import os, sys, time, urllib.error, urllib.request

URL = 'https://data.bls.gov/cew/data/api/{y}/{q}/area/{a}.csv'
UA = {'User-Agent': 'Mozilla/5.0 (research data request)'}

first, last, root, *areas = sys.argv[1:]
for y in range(int(first), int(last) + 1):
    for q in (1, 2, 3, 4):
        d = os.path.join(root, f'{y}q{q}'); os.makedirs(d, exist_ok=True)
        got = 0
        for a in areas:
            out = os.path.join(d, f'{a}.csv')
            if os.path.exists(out): got += 1; continue
            try:
                data = urllib.request.urlopen(urllib.request.Request(URL.format(y=y, q=q, a=a), headers=UA), timeout=60).read()
            except urllib.error.HTTPError as e:
                print(f'{y}q{q} {a}: HTTP {e.code}, skipped'); continue
            if not data.startswith(b'"area_fips"'):
                print(f'{y}q{q} {a}: unexpected content, skipped'); continue
            open(out, 'wb').write(data); got += 1
            time.sleep(0.3)
        print(f'{y}q{q}: {got}/{len(areas)} files')
