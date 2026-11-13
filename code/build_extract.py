"""Analysis extract for Stata / R: one row per synthetic establishment-quarter, selected counties and versions (2026-10-25).

Joins, per prior and version:
  - the step-3b panel (employment by month, wages, job and worker flows, firm size and age, events)
  - worker composition from the stock-flow worker file (employment, hires and separations by sex, age, education,
    race-ethnicity), summarised as shares
  - foreign-born shares: the worker mix x the nativity share table (nativity.py; cell x first firm-size class x
    race-ethnicity x education)
  - occupation-based AI exposure and task measures, and occupation-group shares (occupation_exposure.py)
Writes extract_<label>_<prior>_v<V>.dta (Stata 14+, readable in R with haven::read_dta), labels_<label>.do
(value labels for the class codes) and codebook_<label>.md.

Caution (analysis strategy S1): the data carry real information at the cell level (county x industry x firm size /
age x quarter); differences between establishments within a cell are model-generated.

Usage: python3 code/build_extract.py <panel_root> <workers_root> <out_dir> --prior random --versions 1 --counties 24031,24047 --label mont_worc
  panel_root   folder with p_<prior>/panel_md.csv.gz (merged step-3b panel, e.g. runs/md_m20)
  workers_root folder with <prior>/v<K>/g<G>/{workers,exposure} (e.g. runs/workers_m5)
"""
import argparse
import collections
import csv
import glob
import gzip
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond  # noqa: E402
import priors  # noqa: E402

AGE4 = {'A01': 'lt25', 'A02': 'lt25', 'A03': 'lt25', 'A04': '25_34', 'A05': '35_54', 'A06': '35_54', 'A07': '55p', 'A08': '55p'}
EDU = {'E1': 'lths', 'E2': 'hs', 'E3': 'somecol', 'E4': 'ba'}
RE = {'H': 'hisp', 'W': 'white', 'B': 'black', 'A': 'asian', 'O': 'other'}
MEAS = ['llm_beta_gpt4', 'llm_beta_human', 'aioe', 'lm_aioe', 'nr_analytical', 'nr_interpersonal', 'routine_cognitive', 'routine_manual', 'nr_manual_phys']
SOC = ['11', '13', '15', '17', '19', '21', '23', '25', '27', '29', '31', '33', '35', '37', '39', '41', '43', '45', '47', '49', '51', '53', '55']
FS_LAB = {1: '0-19', 2: '20-49', 3: '50-249', 4: '250-499', 5: '500+'}
FA_LAB = {1: '0-1 years', 2: '2-3 years', 3: '4-5 years', 4: '6-10 years', 5: '11+ years'}

PANEL_NUM = ['emp_prev_m3', 'emp_m1', 'emp_m2', 'emp_m3', 'emp_avg', 'wages', 'job_gain', 'job_loss', 'hires', 'separations', 'churn', 'firm_age_years']
LABELS = {
    'prior': 'reconstruction: 1 random baseline, 2 systematic', 'version': 'version (draw) of the reconstruction, 1-20',
    'year': 'year', 'quarter': 'quarter', 'tq': 'year-quarter (Stata %tq)', 'county': 'county FIPS',
    'naics6': 'industry, 6-digit NAICS 2022', 'naics4': 'industry, 4-digit NAICS', 'sector': 'sector (BDS grouping)',
    'est_id': 'synthetic establishment id (stable across quarters)', 'event': 'panel event this quarter',
    'pred_id': 'predecessor id (splits / reclassification)', 'firm_size': 'firm size class (national firm employment)',
    'firm_age': 'firm age class', 'firm_age_years': 'firm age in years (synthetic, within class)',
    'emp_prev_m3': 'employment, month 3 of previous quarter', 'emp_m1': 'employment, month 1', 'emp_m2': 'employment, month 2',
    'emp_m3': 'employment, month 3', 'emp_avg': 'average monthly employment', 'wages': 'total quarterly wages (USD)',
    'job_gain': 'job gains (economic)', 'job_loss': 'job losses (economic)', 'hires': 'hires', 'separations': 'separations',
    'churn': 'churn (hires beyond job gains)', 'post2023': '1 if 2023 Q1 or later (after ChatGPT release)',
    'w_emp': 'worker-file employment (check: = emp_avg)', 'fb': 'share foreign-born, employment',
    'h_fb': 'share foreign-born, hires', 's_fb': 'share foreign-born, separations',
}
for x in ('', 'h_', 's_'):
    base = {'': 'employment', 'h_': 'hires', 's_': 'separations'}[x]
    LABELS[f'{x}female'] = f'share female, {base}'
    for a in ('lt25', '25_34', '35_54', '55p'): LABELS[f'{x}age_{a}'] = f'share age {a.replace("_", "-").replace("lt25", "<25").replace("55p", "55+")}, {base}'
    for e, d in (('lths', 'less than high school'), ('hs', 'high school'), ('somecol', 'some college'), ('ba', "bachelor's+")):
        LABELS[f'{x}edu_{e}'] = f'share {d} among 25+, {base}'
    for r, d in (('hisp', 'Hispanic'), ('white', 'non-Hispanic white'), ('black', 'non-Hispanic Black'), ('asian', 'non-Hispanic Asian'), ('other', 'other / multiple')):
        LABELS[f'{x}re_{r}'] = f'share {d}, {base}'
for m in MEAS: LABELS[m] = f'occupation-weighted {m} (workers)'
LABELS['llm_beta_gpt4_hires'] = 'AI exposure llm_beta_gpt4 of hires'; LABELS['llm_beta_gpt4_separations'] = 'AI exposure llm_beta_gpt4 of separations'
for g in SOC: LABELS[f'soc_{g}'] = f'share of employment in SOC major group {g}'


def num(v):
    try: return float(v) if v != '' else float('nan')
    except ValueError: return float('nan')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('panel_root'); ap.add_argument('workers_root'); ap.add_argument('out')
    ap.add_argument('--prior', required=True, choices=('random', 'systematic')); ap.add_argument('--versions', default='1')
    ap.add_argument('--counties', default='24031,24047'); ap.add_argument('--label', default='mont_worc'); ap.add_argument('--tag', default='md')
    a = ap.parse_args()
    import pandas as pd
    V = [str(v) for v in cond.parse_versions(a.versions, 20)]; C = set(a.counties.split(','))
    os.makedirs(a.out, exist_ok=True)
    for v in V:
        pieces = sorted(glob.glob(os.path.join(a.workers_root, a.prior, f'v{v}', 'g*')))
        if not pieces: sys.exit(f'no worker pieces for version {v} in {a.workers_root}/{a.prior}')
        # 1. worker composition and foreign-born shares per establishment-quarter
        nat = {}
        for p in pieces:
            for r in csv.DictReader(cond.open_text(cond.require(os.path.join(p, 'workers', f'nativity_shares_{a.tag}.csv.gz')))):
                if r['county'] in C: nat[(r['county'], r['naics6'], r['first_firm_size_class'], r['re'], r['education'])] = float(r['N2']) + float(r['N3'])
        W = collections.defaultdict(lambda: collections.defaultdict(float)); first_fs = {}
        for p in pieces:
            with cond.open_text(os.path.join(p, 'workers', f'workers_sf_{a.tag}.csv.gz')) as fh:
                for r in csv.DictReader(fh):
                    if r['county'] not in C: continue
                    fs0 = first_fs.setdefault(r['est_id'], r['firm_size_class'])
                    k = (r['year'], r['quarter'], r['est_id']); A = W[k]
                    fb = nat.get((r['county'], r['naics6'], fs0, r['re'], r['education']), 0.0)
                    young = r['agegrp'] in ('A01', 'A02', 'A03')
                    for x, col in (('', 'emp'), ('h_', 'hires'), ('s_', 'separations')):
                        val = num(r[col])
                        if not val or math.isnan(val): continue
                        A[f'{x}tot'] += val; A[f'{x}fb'] += val * fb
                        if r['sex'] == '2': A[f'{x}female'] += val
                        A[f'{x}age_{AGE4[r["agegrp"]]}'] += val
                        if not young and r['education'] in EDU: A[f'{x}edu_{EDU[r["education"]]}'] += val; A[f'{x}tot25'] += val
                        A[f'{x}re_{RE[r["re"]]}'] += val
        # 2. exposure per establishment-quarter
        X = {}
        for p in pieces:
            fp = os.path.join(p, 'exposure', f'est_exposure_{a.tag}.csv.gz')
            with cond.open_text(fp) as fh:
                for r in csv.DictReader(fh):
                    if r['county'] in C: X[(r['year'], r['quarter'], r['est_id'])] = r
        # 3. panel rows, joined
        rows = []
        with cond.open_text(cond.require(os.path.join(a.panel_root, f'p_{a.prior}', f'panel_{a.tag}.csv.gz'))) as fh:
            for r in csv.DictReader(fh):
                if r['version'] != v or r['county'] not in C: continue
                y, q = int(r['year']), int(r['quarter']); k = (r['year'], r['quarter'], r['est_id'])
                o = {'prior': 1 if a.prior == 'random' else 2, 'version': int(v), 'year': y, 'quarter': q, 'tq': (y - 1960) * 4 + q - 1,
                     'county': r['county'], 'naics6': r['naics6'], 'naics4': r['naics6'][:4], 'sector': priors.bds_sector(r['naics6']),
                     'est_id': r['est_id'], 'event': r['event'], 'pred_id': r['pred_id'],
                     'firm_size': int(r['firm_size_class']) if r['firm_size_class'] else None,
                     'firm_age': int(r['firm_age_class']) if r['firm_age_class'] else None, 'post2023': int(y >= 2023)}
                for c in PANEL_NUM: o[c] = num(r[c])
                A = W.get(k, {}); o['w_emp'] = A.get('tot', 0.0)
                for x in ('', 'h_', 's_'):
                    t = A.get(f'{x}tot', 0.0); t25 = A.get(f'{x}tot25', 0.0)
                    o[f'{x}fb'] = A.get(f'{x}fb', 0.0) / t if t > 0 else float('nan')
                    o[f'{x}female'] = A.get(f'{x}female', 0.0) / t if t > 0 else float('nan')
                    for g in ('lt25', '25_34', '35_54', '55p'): o[f'{x}age_{g}'] = A.get(f'{x}age_{g}', 0.0) / t if t > 0 else float('nan')
                    for g in EDU.values(): o[f'{x}edu_{g}'] = A.get(f'{x}edu_{g}', 0.0) / t25 if t25 > 0 else float('nan')
                    for g in RE.values(): o[f'{x}re_{g}'] = A.get(f'{x}re_{g}', 0.0) / t if t > 0 else float('nan')
                e = X.get(k, {})
                for m in MEAS + ['llm_beta_gpt4_hires', 'llm_beta_gpt4_separations']: o[m] = num(e.get(m, ''))
                for g in SOC: o[f'soc_{g}'] = num(e.get(f'soc_{g}', ''))
                rows.append(o)
        df = pd.DataFrame(rows)
        for c in df.columns:
            if df[c].dtype == 'float64': df[c] = df[c].astype('float32')
        for c in ('prior', 'quarter', 'firm_size', 'firm_age', 'post2023'): df[c] = df[c].astype('Int8') if df[c].isna().any() else df[c].astype('int8')
        for c in ('version',): df[c] = df[c].astype('int8')
        for c in ('year', 'tq'): df[c] = df[c].astype('int16')
        out = os.path.join(a.out, f'extract_{a.label}_{a.prior}_v{v}.dta')
        df.to_stata(out + '.part', write_index=False, version=118, variable_labels={c: LABELS.get(c, c)[:80] for c in df.columns},
                    data_label=f'Inverse CSS synthetic establishments, {a.prior}, version {v}, counties {a.counties}')
        os.replace(out + '.part', out)
        chk = df['w_emp'] - df['emp_avg']
        print(f'{out}: {len(df):,} rows, {df.shape[1]} columns; worker-file employment vs emp_avg: max |gap| {chk.abs().max():.3f}; '
              f'rows without worker rows {int((df["w_emp"] == 0).sum()):,}; rows without exposure {int(df["llm_beta_gpt4"].isna().sum()):,}')
        cols = list(df.columns)
    with open(os.path.join(a.out, f'labels_{a.label}.do'), 'w') as fh:
        fh.write('* value labels for the class codes (run after: use extract_..., clear)\n')
        fh.write('label define fsize ' + ' '.join(f'{k} "{v}"' for k, v in FS_LAB.items()) + ', replace\nlabel values firm_size fsize\n')
        fh.write('label define fage ' + ' '.join(f'{k} "{v}"' for k, v in FA_LAB.items()) + ', replace\nlabel values firm_age fage\n')
        fh.write('label define prior 1 "random baseline" 2 "systematic", replace\nlabel values prior prior\nformat tq %tq\n')
    with open(os.path.join(a.out, f'codebook_{a.label}.md'), 'w') as fh:
        fh.write(f'# Codebook: analysis extract `{a.label}` (counties {a.counties})\n\n'
                 'One row per synthetic establishment-quarter, 2021 Q1 - 2024 Q4. One file per reconstruction (prior) and version.\n'
                 'Shares are of the establishment\'s employment (no prefix), hires (`h_`) or separations (`s_`); education shares are among workers 25+.\n'
                 'Use `emp_avg` for employment: `w_emp` (the worker file\'s total) can be up to about 0.3 jobs lower, because worker-type cells below 0.005 jobs are dropped. Establishments with average employment of about 0.3 or less (mostly temporarily closed) may have no worker rows, so their shares are missing.\n'
                 'Missing (.) = no workers / hires / separations of that kind. Firm size: ' + ', '.join(f'{k} = {v}' for k, v in FS_LAB.items()) +
                 '. Firm age: ' + ', '.join(f'{k} = {v}' for k, v in FA_LAB.items()) + '.\n\n'
                 '**Caution (analysis strategy S1):** the published tables pin down cells (county x industry x firm size / age x quarter); '
                 'differences between establishments within a cell are generated by the model. Test hypotheses about firm groups; treat '
                 'establishment-level heterogeneity within cells as model-based. Combine versions with Rubin\'s rules for uncertainty.\n\n'
                 '| Variable | Description |\n|---|---|\n')
        for c in cols: fh.write(f'| `{c}` | {LABELS.get(c, c)} |\n')
    print(cond.run_line())


if __name__ == '__main__':
    main()
