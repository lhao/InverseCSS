"""CPS ASEC: how worker composition and AI exposure vary with employer (firm) size, by sector (national, 2021-2024).

FIRMSIZE refers to the employer of the longest job last year (all locations of the employer), so it matches the
national-firm size concept of QWI firm-size classes (design rule R7). Industry and occupation of that job (INDLY, OCCLY)
are Census codes; they are linked to NAICS sector and to SOC-based occupation measures through ACS, which records both
coding systems for the same workers (IND <-> INDNAICS, OCC <-> OCCSOC; weighted modal match, 2021-2024).

Universe: ASEC respondents (ASECFLAG = 1), private wage and salary job last year (CLASSWLY 22; CPS has no separate
nonprofit code), positive FIRMSIZE. Weights: ASECWT, averaged over survey years.

Outputs (<out_dir>): cps_firmsize_sector.csv (sector x firm-size class: workers, foreign-born, noncitizen, BA+, female,
mean LLM exposure beta, AIOE, task composites), cps_firmsize_report.txt
Usage: python3 cps_firmsize_tables.py <cps_parquet> <cps_labels.json> <acs_parquet> <occ_measures_acs_occsoc.csv> <out_dir>
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond
import numpy as np, pandas as pd
from acs_worker_tables import sector_of, MEASURES


def modal_map(df, src, dst, w):
    g = df.groupby([src, dst])[w].sum().reset_index().sort_values(w, ascending=False)
    return g.drop_duplicates(src).set_index(src)[dst]


def main():
    cps_path, lab_path, acs_path, occ_path, out = sys.argv[1:6]
    os.makedirs(out, exist_ok=True)
    lab = json.load(open(cond.require(lab_path)))
    acs = pd.read_parquet(cond.require(acs_path), columns=['YEAR', 'MULTYEAR', 'EMPSTAT', 'IND', 'INDNAICS', 'OCC', 'OCCSOC', 'PERWT'])
    acs = acs[(acs.YEAR >= 2021) & acs.MULTYEAR.isna() & (acs.EMPSTAT == 1)]
    ind2sec = modal_map(acs.assign(sector=acs.INDNAICS.map(sector_of)).dropna(subset=['sector']), 'IND', 'sector', 'PERWT')
    occ2soc = modal_map(acs[acs.OCCSOC != '0'], 'OCC', 'OCCSOC', 'PERWT')
    occm = pd.read_csv(cond.require(occ_path), dtype={'occsoc': str}).set_index('occsoc')[MEASURES]

    c = pd.read_parquet(cond.require(cps_path), columns=['YEAR', 'ASECFLAG', 'ASECWT', 'CLASSWLY', 'FIRMSIZE', 'INDLY', 'OCCLY',
                                                          'NATIVITY', 'CITIZEN', 'EDUC', 'SEX', 'AGE'])
    c = c[(c.ASECFLAG == 1) & (c.CLASSWLY == 22) & (c.FIRMSIZE > 0) & (c.ASECWT > 0)].copy()
    fs_lab = lab['FIRMSIZE']['values']
    c['firmsize'] = c.FIRMSIZE.astype(int).astype(str).map(lambda k: fs_lab.get(k, fs_lab.get(k.zfill(2), k)))
    c['sector'] = c.INDLY.map(ind2sec)
    c['occsoc'] = c.OCCLY.map(occ2soc)
    c = c.join(occm, on='occsoc')
    nat = lab['NATIVITY']['values']                   # foreign-born codes taken from the codebook labels
    fb_codes = [int(k) for k, v in nat.items() if 'foreign born' in v.lower() and 'parents' not in v.lower()]
    c['foreign_born'] = c.NATIVITY.isin(fb_codes)
    c['noncitizen'] = c.CITIZEN.isin([int(k) for k, v in lab['CITIZEN']['values'].items() if 'not a citizen' in v.lower()])
    c['ba_plus'] = c.EDUC >= 111
    c['female'] = c.SEX == 2
    c['wt'] = c.ASECWT / c.YEAR.nunique()

    def tab(g):
        w = g.wt; r = {'workers': w.sum(), 'n_records': len(g)}
        for k in ('foreign_born', 'noncitizen', 'ba_plus', 'female'): r[k] = np.average(g[k], weights=w)
        for m in MEASURES:
            ok = g[m].notna(); r[m] = np.average(g.loc[ok, m], weights=w[ok]) if ok.any() else np.nan
        return pd.Series(r)
    order = {v: i for i, v in enumerate(fs_lab.values())}
    t = c.groupby(['sector', 'firmsize']).apply(tab).reset_index()
    t['fs_order'] = t.firmsize.map(order); t = t.sort_values(['sector', 'fs_order']).drop(columns='fs_order')
    t.to_csv(os.path.join(out, 'cps_firmsize_sector.csv'), index=False)
    allsec = c.groupby('firmsize').apply(tab).reset_index(); allsec['o'] = allsec.firmsize.map(order); allsec = allsec.sort_values('o').drop(columns='o')
    rep = [f'CPS ASEC 2021-2024, private wage and salary jobs last year with firm size: {len(c):,} records',
           f'linked to sector: {np.average(c.sector.notna(), weights=c.wt):.1%}; to occupation measures: {np.average(c.llm_beta_gpt4.notna(), weights=c.wt):.1%}',
           f'FIRMSIZE categories (codebook): {fs_lab}', '',
           'All private sectors, by firm size:',
           allsec[['firmsize', 'workers', 'foreign_born', 'noncitizen', 'ba_plus', 'female', 'llm_beta_gpt4', 'nr_analytical', 'routine_manual']].round(3).to_string(index=False)]
    for sec in ('72', '54', '62', '44-45'):
        rep += ['', f'Sector {sec}, by firm size:',
                t[t.sector == sec][['firmsize', 'n_records', 'foreign_born', 'ba_plus', 'llm_beta_gpt4', 'nr_analytical']].round(3).to_string(index=False)]
    open(os.path.join(out, 'cps_firmsize_report.txt'), 'w').write('\n'.join(rep) + '\n')
    print('\n'.join(rep))


if __name__ == '__main__':
    main()
