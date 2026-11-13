"""Download external sources for the simulation stage and record a manifest (DATA_CONDITIONS E4).

Sources (Maryland pilot):
  LODES8 (LEHD Origin-Destination Employment Statistics), MD, private jobs (JT02), 2021-2023:
      wac (workplace area characteristics, census block: jobs by sector, age, earnings, race, ethnicity,
      education, sex, firm age, firm size), rac (residence area), od main + aux (commuting flows), block crosswalk
  CBP (County Business Patterns) county files 2021-2023: establishments by employment-size class, county x NAICS
  BDS (Business Dynamics Statistics) 2023 release: national sector x firm age x firm size (joint);
      state x sector x firm age; state x sector x firm size; state x county x firm age; state x county x firm size
  Occupation -> tasks -> AI exposure (R9): O*NET 31.0 database (text); Eloundou et al. 2023 occupation-level exposure
      and their O*NET-SOC crosswalk; Felten-Raj-Seamans AIOE data appendix and language-modeling AIOE
Files that are small are kept whole. Nationwide BDS tables are filtered to Maryland (st = 24) or kept whole for the
national joint; years 2019-2023. The full download's SHA-256 is recorded either way.

Manifest: <out_root>/MANIFEST.csv (one row per file: source, url, local file, downloaded_utc, bytes, sha256,
rows_total, rows_kept, filter).

Usage: python3 fetch_external.py <out_root>   (e.g. data/raw/external)
"""
import csv, datetime, gzip, hashlib, io, os, sys, time, urllib.request

UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'}
LODES = 'https://lehd.ces.census.gov/data/lodes/LODES8/md/'
CBP = 'https://www2.census.gov/programs-surveys/cbp/datasets/{y}/cbp{yy}co.zip'
BDS = 'https://www2.census.gov/programs-surveys/bds/tables/time-series/2023/'

JOBS = []
for y in (2021, 2022, 2023):
    JOBS += [('LODES8', LODES + f'wac/md_wac_S000_JT02_{y}.csv.gz', f'lodes/md_wac_S000_JT02_{y}.csv.gz', None),
             ('LODES8', LODES + f'rac/md_rac_S000_JT02_{y}.csv.gz', f'lodes/md_rac_S000_JT02_{y}.csv.gz', None),
             ('LODES8', LODES + f'od/md_od_main_JT02_{y}.csv.gz', f'lodes/md_od_main_JT02_{y}.csv.gz', None),
             ('LODES8', LODES + f'od/md_od_aux_JT02_{y}.csv.gz', f'lodes/md_od_aux_JT02_{y}.csv.gz', None),
             ('CBP', CBP.format(y=y, yy=str(y)[2:]), f'cbp/cbp{str(y)[2:]}co.zip', None)]
JOBS.append(('LODES8', LODES + 'md_xwalk.csv.gz', 'lodes/md_xwalk.csv.gz', None))
# Occupation -> tasks -> AI exposure (design rule R9)
JOBS += [('O*NET 31.0', 'https://www.onetcenter.org/dl_files/database/db_31_0_text.zip', 'occupation/onet/db_31_0_text.zip', None),
         ('Eloundou et al. 2023 (GPTs are GPTs)', 'https://raw.githubusercontent.com/openai/GPTs-are-GPTs/main/data/occ_level.csv', 'occupation/ai_exposure/eloundou2023_occ_level.csv', None),
         ('Eloundou et al. 2023 (GPTs are GPTs)', 'https://raw.githubusercontent.com/openai/GPTs-are-GPTs/main/data/nem-onet-to-soc-crosswalk.xlsx', 'occupation/ai_exposure/eloundou2023_nem-onet-to-soc-crosswalk.xlsx', None),
         ('BLS OEWS May 2021 national (via Eloundou et al. repository)', 'https://raw.githubusercontent.com/openai/GPTs-are-GPTs/main/data/national_May2021_dl.csv', 'occupation/oews/oews_national_May2021_dl.csv', None),
         ('O*NET Resource Center: O*NET-SOC 2010 to 2019 taxonomy crosswalk', 'https://www.onetcenter.org/taxonomy/2019/walk/2010_to_2019_Crosswalk.csv?fmt=csv', 'occupation/onet/2010_to_2019_Crosswalk.csv', None),
         ('Felten, Raj, Seamans 2021 (AIOE)', 'https://raw.githubusercontent.com/AIOE-Data/AIOE/main/AIOE_DataAppendix.xlsx', 'occupation/ai_exposure/felten_AIOE_DataAppendix.xlsx', None),
         ('Felten, Raj, Seamans 2023 (language modeling AIOE)', 'https://raw.githubusercontent.com/AIOE-Data/AIOE/main/Language%20Modeling%20AIOE%20and%20AIIE.xlsx', 'occupation/ai_exposure/felten_language_modeling_AIOE_AIIE.xlsx', None)]
for name, filt in (('sec_fa_fz', 'years'), ('st_sec_fa', 'md'), ('st_sec_fz', 'md'), ('st_cty_fac', 'md'), ('st_cty_fzc', 'md')):
    JOBS.append(('BDS', BDS + f'bds2023_{name}.csv', f'bds/bds2023_{name}' + ('_md' if filt == 'md' else '') + '_2019-2023.csv.gz', filt))


def get(url, tries=3):
    for i in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300).read()
        except Exception as e:
            if i == tries - 1: raise
            time.sleep(5 * (i + 1))


def main():
    root = sys.argv[1]
    os.makedirs(root, exist_ok=True)
    man_path = os.path.join(root, 'MANIFEST.csv')
    done = set()
    if os.path.exists(man_path):
        done = {r['local_file'] for r in csv.DictReader(open(man_path))}
    new = not os.path.exists(man_path)
    with open(man_path, 'a', newline='') as mf:
        w = csv.writer(mf)
        if new: w.writerow(['source', 'url', 'local_file', 'downloaded_utc', 'bytes', 'sha256', 'rows_total', 'rows_kept', 'filter'])
        for src, url, local, filt in JOBS:
            out = os.path.join(root, local)
            if local in done and os.path.exists(out):
                print('exists, skipped:', local); continue
            os.makedirs(os.path.dirname(out), exist_ok=True)
            data = get(url)
            sha = hashlib.sha256(data).hexdigest()
            total = kept = ''
            tmp = out + '.part'
            if filt is None:
                open(tmp, 'wb').write(data)
            else:
                rows = list(csv.reader(io.StringIO(data.decode('utf-8-sig'))))
                hdr, body = rows[0], rows[1:]
                yi = hdr.index('year')
                keep = [r for r in body if r and 2019 <= int(r[yi]) <= 2023 and (filt == 'years' or r[hdr.index('st')] == '24')]
                total, kept = len(body), len(keep)
                with gzip.open(tmp, 'wt', newline='') as fh:
                    cw = csv.writer(fh); cw.writerow(hdr); cw.writerows(keep)
            os.replace(tmp, out)
            w.writerow([src, url, local, datetime.datetime.utcnow().isoformat(timespec='seconds'), len(data), sha, total, kept,
                        {'md': 'st=24, years 2019-2023', 'years': 'years 2019-2023', None: 'none'}[filt]])
            mf.flush()
            print(f'{local}: {len(data):,} bytes' + (f', kept {kept:,} of {total:,} rows' if filt else ''))


if __name__ == '__main__':
    main()
