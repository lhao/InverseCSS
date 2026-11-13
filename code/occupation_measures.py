"""Occupation -> task content -> AI exposure, by SOC 6-digit and by ACS occupation code (design rule R9).

Task content (Acemoglu & Autor 2011 composites from O*NET 31.0; each element standardized across occupations with
OEWS May 2021 national employment weights, composites summed and re-standardized: weighted mean 0, sd 1):
  nr_analytical    = Analyzing data (4.A.2.a.4) + Thinking creatively (4.A.2.b.2) + Interpreting information for others (4.A.4.a.1)   [IM]
  nr_interpersonal = Interpersonal relationships (4.A.4.a.4) + Guiding/motivating subordinates (4.A.4.b.4) + Coaching (4.A.4.b.5)   [IM]
  routine_cognitive= Repeating same tasks (4.C.3.b.7) + Being exact/accurate (4.C.3.b.4) - Determine tasks/priorities (4.C.3.b.8)   [CX]
  routine_manual   = Pace set by equipment (4.C.3.d.3, CX) + Controlling machines (4.A.3.a.3, IM) + Repetitive motions (4.C.2.d.1.i, CX)
  nr_manual_phys   = Operating vehicles (4.A.3.a.4, IM) + Using hands (4.C.2.d.1.g, CX) + Manual dexterity (1.A.2.a.2, IM) + Spatial orientation (1.A.1.f.1, IM)
  (4.C.3.b.8 is named "Structured versus Unstructured Work" in older O*NET releases.)
AI exposure:
  aioe, lm_aioe           Felten, Raj & Seamans (2021) AI occupational exposure; language-modeling AIOE (2023)  [SOC 2010;
                          carried to SOC 2018 codes through the O*NET-SOC 2010 -> 2019 crosswalk where no direct score]
  llm_beta_gpt4, llm_beta_human (and alpha, gamma)  Eloundou et al. (2023) exposure to LLMs             [O*NET-SOC -> SOC 6]
O*NET-SOC codes (e.g. 11-1011.03) are averaged to SOC 6-digit (11-1011) with equal weights.

ACS occupation codes (IPUMS OCCSOC, e.g. '111021', '1110XX', '13207X') are matched to every SOC 6-digit code that fits
the pattern (X = any digit). Codes with no match that end in 0 are SOC broad / minor groups (e.g. 252020 = 25-2021 +
25-2022): trailing zeros are widened to X one at a time until codes match. Measures are averaged with OEWS employment
weights. Unmatched codes are reported.

Outputs (<out_dir>): occ_measures_soc6.csv, occ_measures_acs_occsoc.csv, occ_measures_report.txt
Usage: python3 occupation_measures.py <external_occupation_dir> <acs_parquet> <out_dir>
"""
import io, os, re, sys, zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond
import numpy as np, pandas as pd

COMPOSITES = {
    'nr_analytical': [('Work Activities', '4.A.2.a.4', 'IM', 1), ('Work Activities', '4.A.2.b.2', 'IM', 1), ('Work Activities', '4.A.4.a.1', 'IM', 1)],
    'nr_interpersonal': [('Work Activities', '4.A.4.a.4', 'IM', 1), ('Work Activities', '4.A.4.b.4', 'IM', 1), ('Work Activities', '4.A.4.b.5', 'IM', 1)],
    'routine_cognitive': [('Work Context', '4.C.3.b.7', 'CX', 1), ('Work Context', '4.C.3.b.4', 'CX', 1), ('Work Context', '4.C.3.b.8', 'CX', -1)],
    'routine_manual': [('Work Context', '4.C.3.d.3', 'CX', 1), ('Work Activities', '4.A.3.a.3', 'IM', 1), ('Work Context', '4.C.2.d.1.i', 'CX', 1)],
    'nr_manual_phys': [('Work Activities', '4.A.3.a.4', 'IM', 1), ('Work Context', '4.C.2.d.1.g', 'CX', 1), ('Abilities', '1.A.2.a.2', 'IM', 1), ('Abilities', '1.A.1.f.1', 'IM', 1)],
}


def wstd(x, w):
    m = np.average(x, weights=w); s = np.sqrt(np.average((x - m) ** 2, weights=w))
    return (x - m) / s


def main():
    src, acs_path, out = sys.argv[1:4]
    os.makedirs(out, exist_ok=True)
    rep = []
    z = zipfile.ZipFile(cond.require(os.path.join(src, 'onet', 'db_31_0_text.zip')))
    def onet(name):
        f = [x for x in z.namelist() if x.endswith('/' + name + '.txt')][0]
        return pd.read_csv(io.BytesIO(z.read(f)), sep='\t')
    tabs = {n: onet(n) for n in ('Work Activities', 'Work Context', 'Abilities')}
    oews = pd.read_csv(cond.require(os.path.join(src, 'oews', 'oews_national_May2021_dl.csv')))
    oews = oews[oews.O_GROUP == 'detailed'].copy()
    oews['emp'] = pd.to_numeric(oews.TOT_EMP.astype(str).str.replace(',', ''), errors='coerce')
    w = oews.set_index('OCC_CODE')['emp']

    # O*NET elements -> SOC 6-digit
    elem = {}
    for comp, items in COMPOSITES.items():
        for tab, eid, scale, sign in items:
            d = tabs[tab]; d = d[(d['Element ID'] == eid) & (d['Scale ID'] == scale)]
            v = d.groupby(d['O*NET-SOC Code'].str[:7])['Data Value'].mean()
            elem[(comp, eid)] = (v, sign)
    soc = sorted(set.intersection(*[set(v.index) for v, _ in elem.values()]))
    df = pd.DataFrame(index=pd.Index(soc, name='soc6'))
    df['oews_emp'] = w.reindex(df.index)
    wt = df['oews_emp'].fillna(df['oews_emp'].median())
    for comp, items in COMPOSITES.items():
        tot = sum(sign * wstd(elem[(comp, eid)][0].reindex(df.index).values, wt.values) for _, eid, _, sign in items)
        df[comp] = wstd(tot, wt.values)
    # AI exposure
    fa = pd.read_excel(os.path.join(src, 'ai_exposure', 'felten_AIOE_DataAppendix.xlsx'), 'Appendix A').set_index('SOC Code')['AIOE']
    fl = pd.read_excel(os.path.join(src, 'ai_exposure', 'felten_language_modeling_AIOE_AIIE.xlsx'), 'LM AIOE').set_index('SOC Code')['Language Modeling AIOE']
    # AIOE and LM AIOE are keyed by SOC 2010; carry them to SOC 2018 codes through the O*NET-SOC 2010 -> 2019 crosswalk
    # (DATA_CONDITIONS C7): a 2018 code without a direct score gets the mean over the 2010 codes that map to it
    cw = pd.read_csv(cond.require(os.path.join(src, 'onet', '2010_to_2019_Crosswalk.csv')))
    c10 = [c for c in cw.columns if '2010' in c and 'Code' in c][0]; c19 = [c for c in cw.columns if '2019' in c and 'Code' in c][0]
    pairs = pd.DataFrame({'s10': cw[c10].str[:7], 's18': cw[c19].str[:7]}).drop_duplicates()
    def to2018(series):
        m = pairs.assign(v=pairs.s10.map(series)).dropna(subset=['v']).groupby('s18').v.mean()
        out = series.copy()
        add = m[~m.index.isin(series.index)]
        rep.append(f'{series.name}: {len(series)} SOC 2010 scores; {len(add)} SOC 2018 codes added through the 2010 -> 2019 crosswalk')
        return pd.concat([out, add])
    fa.name, fl.name = 'AIOE', 'LM AIOE'
    fa, fl = to2018(fa), to2018(fl)
    el = pd.read_csv(os.path.join(src, 'ai_exposure', 'eloundou2023_occ_level.csv'))
    el = el.groupby(el['O*NET-SOC Code'].str[:7])[['dv_rating_alpha', 'dv_rating_beta', 'dv_rating_gamma', 'human_rating_alpha', 'human_rating_beta', 'human_rating_gamma']].mean()
    allsoc = sorted(set(df.index) | set(fa.index) | set(el.index))
    df = df.reindex(allsoc); df.index.name = 'soc6'
    df['oews_emp'] = w.reindex(df.index)
    df['aioe'] = fa.reindex(df.index); df['lm_aioe'] = fl.reindex(df.index)
    for a, b in (('dv_rating_alpha', 'llm_alpha_gpt4'), ('dv_rating_beta', 'llm_beta_gpt4'), ('dv_rating_gamma', 'llm_gamma_gpt4'),
                 ('human_rating_alpha', 'llm_alpha_human'), ('human_rating_beta', 'llm_beta_human'), ('human_rating_gamma', 'llm_gamma_human')):
        df[b] = el[a].reindex(df.index)
    titles = oews.set_index('OCC_CODE')['OCC_TITLE']
    df.insert(0, 'title', titles.reindex(df.index))
    df.to_csv(os.path.join(out, 'occ_measures_soc6.csv'))
    measures = list(COMPOSITES) + ['aioe', 'lm_aioe', 'llm_alpha_gpt4', 'llm_beta_gpt4', 'llm_gamma_gpt4', 'llm_alpha_human', 'llm_beta_human', 'llm_gamma_human']
    rep.append(f'SOC 6-digit occupations: {len(df)}; with all task composites: {df[list(COMPOSITES)].notna().all(axis=1).sum()}; '
               f'with AIOE: {df.aioe.notna().sum()}; with LLM exposure: {df.llm_beta_gpt4.notna().sum()}')
    rep.append('correlations (employment-weighted rows with all measures):')
    cc = df.dropna(subset=measures)
    rep.append(cc[['nr_analytical', 'nr_interpersonal', 'routine_cognitive', 'routine_manual', 'nr_manual_phys', 'aioe', 'lm_aioe', 'llm_beta_gpt4', 'llm_beta_human']].corr().round(2).to_string())

    # ACS OCCSOC -> measures
    acs = pd.read_parquet(cond.require(acs_path), columns=['YEAR', 'OCCSOC', 'PERWT', 'EMPSTAT'])
    acs = acs[(acs.YEAR >= 2019) & (acs.EMPSTAT == 1) & (acs.OCCSOC.str.strip() != '0')]
    codes = acs.groupby('OCCSOC').PERWT.sum()
    soc_digits = {s.replace('-', ''): s for s in df.index}
    rows = []
    for code, pw in codes.items():
        c = code.strip(); hits, rule = [], 'exact or X-wildcard'
        while True:
            pat = re.compile('^' + c.replace('X', r'\d').replace('Y', r'\d') + '$')
            hits = [s for d, s in soc_digits.items() if pat.match(d)]
            if hits or not c.rstrip('X').endswith('0'): break
            # SOC broad / minor group codes end in 0 (e.g. 25-2020 = 25-2021 + 25-2022): widen trailing zeros
            k = len(c.rstrip('X')) - 1; c = c[:k] + 'X' * (len(c) - k); rule = 'broad/minor group (trailing 0 -> X)'
        r = {'occsoc': code, 'acs_workers_weighted': pw, 'n_soc6_matched': len(hits), 'match_rule': rule if hits else 'none',
             'soc6_matched': ' '.join(hits)}
        if hits:
            sub = df.loc[hits]; ww = sub['oews_emp'].fillna(1.0)
            for mcol in measures:
                ok = sub[mcol].notna()
                r[mcol] = np.average(sub.loc[ok, mcol], weights=ww[ok]) if ok.any() else np.nan
        rows.append(r)
    m = pd.DataFrame(rows).sort_values('occsoc')
    m.to_csv(os.path.join(out, 'occ_measures_acs_occsoc.csv'), index=False)
    tot = m.acs_workers_weighted.sum()
    rep.append(f'\nACS occupation codes among employed persons in the extract, 2019-2024: {len(m)}; weighted share matched to >=1 SOC: '
               f'{m.loc[m.n_soc6_matched > 0, "acs_workers_weighted"].sum() / tot:.1%}; share with task composites: '
               f'{m.loc[m.nr_analytical.notna(), "acs_workers_weighted"].sum() / tot:.1%}; with AIOE: {m.loc[m.aioe.notna(), "acs_workers_weighted"].sum() / tot:.1%}; '
               f'with LLM beta: {m.loc[m.llm_beta_gpt4.notna(), "acs_workers_weighted"].sum() / tot:.1%}')
    un = m[m.n_soc6_matched == 0].sort_values('acs_workers_weighted', ascending=False)
    rep.append('largest unmatched ACS codes: ' + ', '.join(f'{r.occsoc} ({r.acs_workers_weighted:,.0f})' for r in un.head(12).itertuples()))
    open(os.path.join(out, 'occ_measures_report.txt'), 'w').write('\n'.join(rep) + '\n')
    print('\n'.join(rep))


if __name__ == '__main__':
    main()
