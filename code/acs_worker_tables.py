"""ACS workers employed in Maryland, harmonized to QWI categories, with occupation task content and AI exposure (R9).

Universe: employed (EMPSTAT = 1) persons whose place of work is Maryland (PWSTATE2 = 24). With a residence-based
extract (STATEFIP = MD) this covers Maryland residents working in Maryland (~88% of Maryland jobs; in-commuters
need the workplace extract, PWSTATE2 = MD). Works unchanged on either kind of extract, or on both combined.

Harmonized variables:
  powpuma, powpuma_area, powpuma_counties   place-of-work area (2010-based codes for data years <= 2021, 2020-based
                                            from 2022) and the counties it covers (Census tract-to-PUMA files)
  sector, naics3, naics4                    from INDNAICS (ACS industry codes are coarser than 6-digit)
  ownership                                 private for-profit / private nonprofit / federal / state / local / self-employed
  sex, agegrp (QWI A01-A08), education (QWI E1-E5; E5 = under 25), race (QWI A1-A7), ethnicity (QWI A1/A2)
  nativity                                  us_born / naturalized / noncitizen
  occupation measures                       task composites and AI exposure from occupation_measures.py (by OCCSOC)
  incwage, uhrswork, wkswork2, trantime, tranwork, perwt, sample type (1-year / 5-year), data year

Outputs (<out_dir>):
  acs_md_workplace_workers.parquet          one row per worker (all samples; use sample_type to avoid pooling 1-yr and 5-yr)
  acs_tables_area_sector.csv                1-year samples pooled 2021-2024: weighted workers, shares and means by area x sector
  acs_tables_report.txt                     coverage and key patterns
Usage: python3 acs_worker_tables.py <acs_parquet> <occ_measures_acs_occsoc.csv> <tract_to_puma_dir> <out_dir>
"""
import csv, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond
import numpy as np, pandas as pd

COUNTY = {'001': 'Allegany', '003': 'Anne Arundel', '005': 'Baltimore County', '009': 'Calvert', '011': 'Caroline', '013': 'Carroll',
          '015': 'Cecil', '017': 'Charles', '019': 'Dorchester', '021': 'Frederick', '023': 'Garrett', '025': 'Harford', '027': 'Howard',
          '029': 'Kent', '031': 'Montgomery', '033': "Prince George's", '035': "Queen Anne's", '037': "St. Mary's", '039': 'Somerset',
          '041': 'Talbot', '043': 'Washington', '045': 'Wicomico', '047': 'Worcester', '510': 'Baltimore City'}
MEASURES = ['nr_analytical', 'nr_interpersonal', 'routine_cognitive', 'routine_manual', 'nr_manual_phys', 'aioe', 'lm_aioe',
            'llm_beta_gpt4', 'llm_beta_human']


def powpuma_counties(geo_dir):
    """{vintage: {powpuma_code: [county fips]}}; POWPUMAs are county-based groups of PUMAs (first three digits);
    2020-based combined codes: 190 = groups 100 + 200, 1590 = groups 1500 + 1600."""
    out = {}
    for vin in ('2010', '2020'):
        g = {}
        with open(cond.require(os.path.join(geo_dir, f'{vin}_Census_Tract_to_{vin}_PUMA.txt')), encoding='utf-8-sig') as fh:
            for r in csv.DictReader(fh):
                if r['STATEFP'] == '24':
                    g.setdefault(int(r['PUMA5CE']) // 100 * 100, set()).add('24' + r['COUNTYFP'])
        if vin == '2020':
            g[190] = g.get(100, set()) | g.get(200, set()); g[1590] = g.get(1500, set()) | g.get(1600, set())
        out[vin] = {k: sorted(v) for k, v in g.items()}
    return out


def sector_of(ind):
    s = str(ind).strip()
    if not s or s == '0': return None
    two = s[:2]
    if two in ('31', '32', '33') or s.startswith('3M'): return '31-33'
    if two in ('44', '45') or s.startswith('4M'): return '44-45'
    if two in ('48', '49'): return '48-49'
    return two if two.isdigit() else None


def main():
    acs_path, occ_path, geo_dir, out = sys.argv[1:5]
    os.makedirs(out, exist_ok=True)
    d = pd.read_parquet(cond.require(acs_path))
    d = d[(d.EMPSTAT == 1) & (d.PWSTATE2 == 24) & (d.YEAR >= 2019)].copy()
    d['data_year'] = d.MULTYEAR.fillna(d.YEAR).astype(int)
    d['sample_type'] = np.where(d.MULTYEAR.isna(), '1-year', '5-year')
    pw = powpuma_counties(geo_dir)
    d['powpuma'] = d.PWPUMA00.astype(int)
    d['powpuma_vintage'] = np.where(d.data_year <= 2021, '2010', '2020')
    d['powpuma_counties'] = [' '.join(pw[v].get(p, [])) for v, p in zip(d.powpuma_vintage, d.powpuma)]
    d['powpuma_area'] = [', '.join(COUNTY[c[2:]] for c in pw[v].get(p, [])) for v, p in zip(d.powpuma_vintage, d.powpuma)]
    d['sector'] = d.INDNAICS.map(sector_of)
    s = d.INDNAICS.astype(str).str.strip()
    d['naics3'] = np.where(s.str[:3].str.isdigit(), s.str[:3], None)
    d['naics4'] = np.where(s.str[:4].str.isdigit() & (s.str.len() >= 4), s.str[:4], None)
    cw = d.CLASSWKRD.astype(int)
    d['ownership'] = np.select([cw == 22, cw == 23, cw == 25, cw == 27, cw == 28, cw.isin([13, 14])],
                               ['private_forprofit', 'private_nonprofit', 'federal', 'state', 'local', 'self_employed'], 'other')
    age = d.AGE.astype(int)
    d['agegrp'] = pd.cut(age, [13, 18, 21, 24, 34, 44, 54, 64, 200], labels=['A01', 'A02', 'A03', 'A04', 'A05', 'A06', 'A07', 'A08']).astype(str)
    e = d.EDUCD.astype(int)
    d['education'] = np.select([age < 25, e < 62, e < 65, e < 101], ['E5', 'E1', 'E2', 'E3'], 'E4')   # QWI: E5 = not available (under 25)
    rc, rd = d.RACE.astype(int), d.RACED.astype(int)
    nhpi = (rc == 6) & (rd >= 680) & (rd < 700)
    d['race'] = np.select([rc == 1, rc == 2, rc == 3, rc.isin([4, 5]) | ((rc == 6) & ~nhpi), nhpi, rc.isin([8, 9])],
                          ['A1', 'A2', 'A3', 'A4', 'A5', 'A7'], 'other')   # QWI race codes; 'other' = some other race alone
    d['ethnicity'] = np.where(d.HISPAN.astype(int) == 0, 'A1', 'A2')
    cz = d.CITIZEN.astype(int)
    d['nativity'] = np.select([cz.isin([0, 1]), cz == 2], ['us_born', 'naturalized'], 'noncitizen')
    occ = pd.read_csv(cond.require(occ_path), dtype={'occsoc': str})[['occsoc'] + MEASURES]
    d = d.merge(occ, left_on='OCCSOC', right_on='occsoc', how='left')
    keep = ['YEAR', 'MULTYEAR', 'data_year', 'sample_type', 'SERIAL', 'PERNUM', 'PERWT', 'STATEFIP', 'COUNTYFIP', 'PUMA',
            'powpuma', 'powpuma_vintage', 'powpuma_area', 'powpuma_counties', 'INDNAICS', 'sector', 'naics3', 'naics4', 'OCCSOC',
            'ownership', 'SEX', 'AGE', 'agegrp', 'education', 'race', 'ethnicity', 'nativity', 'BPL', 'YRIMMIG', 'SPEAKENG',
            'INCWAGE', 'UHRSWORK', 'WKSWORK2', 'TRANWORK', 'TRANTIME'] + MEASURES
    tmp = os.path.join(out, 'acs_md_workplace_workers.parquet.part')
    d[keep].to_parquet(tmp, index=False); os.replace(tmp, os.path.join(out, 'acs_md_workplace_workers.parquet'))

    # area x sector tables, 1-year samples pooled 2021-2024, private + government wage and salary workers
    one = d[(d.sample_type == '1-year') & d.data_year.between(2021, 2024) & d.ownership.isin(['private_forprofit', 'private_nonprofit', 'federal', 'state', 'local'])]
    one = one.assign(wt=one.PERWT / one.data_year.nunique())                        # average annual weighted workers
    def tab(g):
        w = g.wt
        r = {'workers': w.sum(), 'n_records': len(g), 'foreign_born': np.average(g.nativity != 'us_born', weights=w),
             'noncitizen': np.average(g.nativity == 'noncitizen', weights=w), 'female': np.average(g.SEX == 2, weights=w),
             'ba_plus': np.average(g.education == 'E4', weights=w), 'black': np.average(g.race == 'A2', weights=w),
             'hispanic': np.average(g.ethnicity == 'A2', weights=w), 'mean_wage': np.average(g.INCWAGE.clip(upper=9_999_998), weights=w),
             'mean_commute_min': np.average(g.TRANTIME, weights=w)}
        for mcol in MEASURES:
            ok = g[mcol].notna()
            r[mcol] = np.average(g.loc[ok, mcol], weights=w[ok]) if ok.any() else np.nan
        return pd.Series(r)
    t = one.groupby(['powpuma_vintage', 'powpuma', 'powpuma_area', 'sector']).apply(tab).reset_index()
    t.to_csv(os.path.join(out, 'acs_tables_area_sector.csv'), index=False)

    rep = [f'Workers employed in Maryland (PWSTATE2 = 24, EMPSTAT = 1), data years 2019-2024: {len(d):,} records',
           'records by sample type and data year: ' + d.groupby(['sample_type', 'data_year']).size().to_string().replace('\n', '; '),
           f'occupation measures attached (weighted, 1-year 2021-2024): '
           f'{np.average(one.llm_beta_gpt4.notna(), weights=one.wt):.1%} (LLM beta), {np.average(one.nr_analytical.notna(), weights=one.wt):.1%} (task composites)',
           f'ownership shares: ' + one.groupby('ownership').wt.sum().div(one.wt.sum()).round(3).to_string().replace('\n', '; ')]
    st = one.groupby('sector').apply(tab)
    rep += ['', 'Statewide by sector (1-year 2021-2024, average annual workers): workers, foreign-born share, BA+, LLM exposure beta, AIOE, analytical, routine manual',
            st[['workers', 'foreign_born', 'ba_plus', 'llm_beta_gpt4', 'aioe', 'nr_analytical', 'routine_manual']].round(3).sort_values('llm_beta_gpt4', ascending=False).to_string()]
    for v in ('2020',):
        sl = t[(t.powpuma_vintage == v) & (((t.powpuma == 1400) & (t.sector == '72')) | ((t.powpuma == 1000) & (t.sector.isin(['54', '72']))))]
        rep += ['', 'Pilot areas (2020-based codes): Lower Eastern Shore (1400: Somerset, Wicomico, Worcester) x 72; Montgomery (1000) x 54, 72',
                sl[['powpuma_area', 'sector', 'workers', 'n_records', 'foreign_born', 'ba_plus', 'llm_beta_gpt4', 'nr_analytical', 'routine_manual', 'mean_wage']].round(3).to_string(index=False)]
    open(os.path.join(out, 'acs_tables_report.txt'), 'w').write('\n'.join(rep) + '\n')
    print('\n'.join(rep))


if __name__ == '__main__':
    main()
