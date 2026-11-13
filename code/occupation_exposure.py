"""Step 4 (R9): occupation mix and AI exposure per synthetic establishment, pilot slices.

For each worker type (sex x age x education x race-ethnicity, as in worker_groups.py) in an industry and place-of-work
area, ACS workplace records (1-year 2021-2024, person weights) give the expected occupation-based measures:
  AI exposure   llm_beta_gpt4 (Eloundou et al. 2023, main), llm_beta_human, aioe (Felten-Raj-Seamans 2021), lm_aioe (2023)
  task content  nr_analytical, nr_interpersonal, routine_cognitive, routine_manual, nr_manual_phys (Acemoglu-Autor)
  occupation    shares of SOC major groups (first two digits of OCCSOC)
An establishment's value is the average over its workers (employment), and separately over its hires and
separations, using the worker counts by type from worker_groups.py.

ACS industry for a NAICS 6-digit cell: the ACS 4-digit code if ACS has it (7211, 5417), else 3-digit (722), else sector.
Small-sample smoothing (empirical Bayes, k = 10 records): each level is shrunk towards the next coarser level:
  systematic  industry x area x (sex, age3, education, race-ethnicity) -> industry x area x (education, age3)
              -> industry x area -> industry statewide
  random      industry x area -> industry statewide (occupation independent of worker type within the industry:
              the maximum-entropy assumption)
Exposure is potential and fixed per occupation (C7); establishment exposure changes over time only through workforce
composition.

Outputs (<out_dir>): est_exposure_<tag>.csv.gz (version x quarter x establishment), exposure_report_<tag>.txt
Usage: python3 occupation_exposure.py <workers_dir> <panel_dir> <acs_workplace.parquet> <out_dir> [--prior random|systematic] [--k 10]
"""
import argparse, collections, csv, gzip, os, statistics as st, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond
import priors

MEAS = ['llm_beta_gpt4', 'llm_beta_human', 'aioe', 'lm_aioe', 'nr_analytical', 'nr_interpersonal', 'routine_cognitive', 'routine_manual', 'nr_manual_phys']
SOC = ['11', '13', '15', '17', '19', '21', '23', '25', '27', '29', '31', '33', '35', '37', '39', '41', '43', '45', '47', '49', '51', '53', '55']
YOUNG = {'A01', 'A02', 'A03'}


def age3(a): return 'y' if a in YOUNG else ('o' if a in ('A07', 'A08') else 'm')


def re_group(race, eth):
    if eth == 'A2': return 'H'
    return {'A1': 'W', 'A2': 'B', 'A4': 'A'}.get(race, 'O')


class Acc:
    """Weighted sums for the measures and SOC shares in one conditioning cell."""
    __slots__ = ('n', 'w', 'mw', 'ms', 'soc')

    def __init__(self):
        self.n = 0; self.w = 0.0; self.mw = collections.Counter(); self.ms = collections.Counter(); self.soc = collections.Counter()

    def add(self, p):
        self.n += 1; self.w += p['PERWT']
        for m in MEAS:
            if p[m] is not None: self.mw[m] += p['PERWT']; self.ms[m] += p['PERWT'] * p[m]
        g = (p['OCCSOC'] or '')[:2]
        if g in SOC: self.soc[g] += p['PERWT']

    def est(self):
        return ({m: self.ms[m] / self.mw[m] for m in MEAS if self.mw[m] > 0},
                {g: self.soc[g] / self.w for g in SOC} if self.w > 0 else {})


def shrink(cell, parent, k):
    """Empirical-Bayes shrinkage of a cell estimate towards its parent, weight n / (n + k) on the cell."""
    if cell is None or cell.n == 0: return parent
    mc, sc = cell.est(); mp, sp = parent; wgt = cell.n / (cell.n + k)
    m = {x: wgt * mc[x] + (1 - wgt) * mp[x] if x in mc and x in mp else mp.get(x, mc.get(x)) for x in MEAS if x in mc or x in mp}
    s = {g: wgt * sc.get(g, 0.0) + (1 - wgt) * sp.get(g, 0.0) for g in SOC}
    return m, s


def main():
    ap = argparse.ArgumentParser()
    for x in ('workers', 'panel', 'acs', 'out'): ap.add_argument(x)
    ap.add_argument('--prior', default='random', choices=('random', 'systematic'))
    ap.add_argument('--k', type=float, default=10.0); ap.add_argument('--tag', default='slices')
    ap.add_argument('--cps', default='data/processed/ipums/workplace_00006/cps_firmsize_sector.csv')
    ap.add_argument('--size-tilt', type=float, default=None, help='lambda for the CPS firm-size shift of exposure measures (default 0.5 systematic, 0 random)')
    a = ap.parse_args()
    if a.size_tilt is None: a.size_tilt = 0.5 if a.prior == 'systematic' else 0.0
    cond.start_run(a.workers, a.panel, a.acs)
    import pyarrow.parquet as pq
    cols = ['sample_type', 'sector', 'naics3', 'naics4', 'powpuma_counties', 'PERWT', 'SEX', 'agegrp', 'education', 'race', 'ethnicity', 'OCCSOC'] + MEAS
    acs = [p for p in pq.read_table(cond.require(a.acs), columns=cols).to_pylist() if p['sample_type'] == '1-year']
    have4 = {p['naics4'] for p in acs if p['naics4']}; have3 = {p['naics3'] for p in acs if p['naics3']}
    def ind_key(n6):
        return ('4', n6[:4]) if n6[:4] in have4 else (('3', n6[:3]) if n6[:3] in have3 else ('s', priors.bds_sector(n6)))
    # conditioning cells
    C = collections.defaultdict(Acc)
    for p in acs:
        e = 'E5' if p['agegrp'] in YOUNG else p['education']
        ik = [('4', p['naics4']), ('3', p['naics3']), ('s', p['sector'])]
        areas = (p['powpuma_counties'] or '').replace(',', ' ').split()  # space-separated county list
        for k in ik:
            if not k[1]: continue
            C[(k, 'MD')].add(p)
            for ar in areas:
                C[(k, ar)].add(p)
                C[(k, ar, e, age3(p['agegrp']))].add(p)
                C[(k, ar, str(p['SEX']), age3(p['agegrp']), e, re_group(p['race'], p['ethnicity']))].add(p)
    del acs   # summarised in C; free the records (memory, 2026-10-25)
    cache = {}; lvl = collections.Counter()
    def type_measures(n6, county, t):
        """(measures, soc shares) for worker type t = (sex, agegrp, education, re) in the cell's industry and county."""
        ik = ind_key(n6); key = (ik, county, t if a.prior == 'systematic' else None)
        if key in cache: return cache[key]
        top = C.get((ik, 'MD'))
        est = top.est() if top else ({}, {})
        est = shrink(C.get((ik, county)), est, a.k)
        if a.prior == 'systematic':
            s, ag, e, r = t
            est = shrink(C.get((ik, county, e, age3(ag))), est, a.k)
            est = shrink(C.get((ik, county, s, age3(ag), e, r)), est, a.k)
            lvl['type_cells_with_records' if C.get((ik, county, s, age3(ag), e, r)) else 'type_cells_without_records'] += 1
        cache[key] = est
        return est
    # CPS ASEC: measure means by sector x firm size (national) -> additive shift by QWI firm-size class, x lambda
    CPS_MAP = {'1': ('Under 10', '10 to 24'), '2': ('10 to 24', '25 to 99'), '3': ('25 to 99', '100 to 499'), '4': ('100 to 499',), '5': ('500 to 999', '1000+')}
    cps = collections.defaultdict(dict)
    if a.size_tilt and os.path.exists(a.cps):
        with open(a.cps) as fh:
            for r in csv.DictReader(fh):
                cps[r['sector']][r['firmsize']] = (float(r['workers']), {m: float(r[m]) for m in MEAS if r.get(m) not in (None, '')})
    def size_shift(n6, fs):
        d = cps.get(priors.bds_sector(n6))
        if not d or not a.size_tilt: return {}
        tot = sum(w for w, _ in d.values())
        avg = {m: sum(w * v.get(m, 0) for w, v in d.values()) / tot for m in MEAS}
        sel = [d[c] for c in CPS_MAP.get(fs, ()) if c in d]
        if not sel: return {}
        sw = sum(w for w, _ in sel)
        return {m: a.size_tilt * (sum(w * v.get(m, 0) for w, v in sel) / sw - avg[m]) for m in MEAS if all(m in v for _, v in sel)}
    # firm age class per establishment-quarter (for the report)
    fage = {}
    with cond.open_text(cond.require(os.path.join(a.panel, f'panel_{a.tag}.csv.gz'))) as fh:
        for r in csv.DictReader(fh): fage[(r['version'], r['year'], r['quarter'], r['est_id'])] = r['firm_age_class']
    # establishments
    agg = collections.defaultdict(lambda: {'emp': 0.0, 'hires': 0.0, 'seps': 0.0, 'm_emp': collections.Counter(), 'w_emp': collections.Counter(),
                                           'm_h': collections.Counter(), 'w_h': collections.Counter(), 'm_s': collections.Counter(), 'w_s': collections.Counter(),
                                           'soc': collections.Counter(), 'info': None})
    with cond.open_text(cond.require(os.path.join(a.workers, f'workers_{a.tag}.csv.gz'))) as fh:
        for r in csv.DictReader(fh):
            k = (r['version'], r['year'], r['quarter'], r['county'], r['naics6'], r['est_id'])
            A = agg[k]; A['info'] = r['firm_size_class']
            ms, soc = type_measures(r['naics6'], r['county'], (r['sex'], r['agegrp'], r['education'], r['re']))
            e, h, sp = float(r['emp']), float(r['hires']), float(r['separations'])
            A['emp'] += e; A['hires'] += h; A['seps'] += sp
            for m, v in ms.items():
                A['m_emp'][m] += e * v; A['w_emp'][m] += e
                A['m_h'][m] += h * v; A['w_h'][m] += h; A['m_s'][m] += sp * v; A['w_s'][m] += sp
            for g, v in soc.items(): A['soc'][g] += e * v
    os.makedirs(a.out, exist_ok=True)
    tmp = os.path.join(a.out, f'est_exposure_{a.tag}.csv.gz.part')
    rep = collections.defaultdict(list)
    with gzip.open(tmp, 'wt', newline='') as fo:
        w = csv.writer(fo)
        w.writerow(['version', 'year', 'quarter', 'county', 'naics6', 'est_id', 'firm_size_class', 'firm_age_class', 'emp', 'hires', 'separations']
                   + MEAS + ['llm_beta_gpt4_hires', 'llm_beta_gpt4_separations'] + [f'soc_{g}' for g in SOC])
        for k, A in sorted(agg.items()):
            if A['emp'] <= 0 and A['seps'] <= 0: continue
            sh = size_shift(k[4], A['info'])
            mv = {m: A['m_emp'][m] / A['w_emp'][m] + sh.get(m, 0.0) if A['w_emp'][m] > 0 else '' for m in MEAS}
            lh = A['m_h']['llm_beta_gpt4'] / A['w_h']['llm_beta_gpt4'] + sh.get('llm_beta_gpt4', 0.0) if A['w_h']['llm_beta_gpt4'] > 0 else ''
            ls = A['m_s']['llm_beta_gpt4'] / A['w_s']['llm_beta_gpt4'] + sh.get('llm_beta_gpt4', 0.0) if A['w_s']['llm_beta_gpt4'] > 0 else ''
            fa = fage.get((k[0], k[1], k[2], k[5]), '')
            w.writerow(list(k) + [A['info'], fa, round(A['emp'], 3), round(A['hires'], 3), round(A['seps'], 3)]
                       + [round(v, 4) if v != '' else '' for v in mv.values()] + [round(x, 4) if x != '' else '' for x in (lh, ls)]
                       + [round(A['soc'][g] / A['emp'], 4) if A['emp'] > 0 else '' for g in SOC])
            if A['emp'] > 0 and mv['llm_beta_gpt4'] != '':
                rep['rows'].append((k, A['info'], fa, A['emp'], mv['llm_beta_gpt4'], lh, ls, A['hires'], A['seps']))
    os.replace(tmp, os.path.join(a.out, f'est_exposure_{a.tag}.csv.gz'))
    report(a, rep['rows'], lvl, C, ind_key)


def report(a, rows, lvl, C, ind_key):
    L = [f'Step 4 (R9) occupation and AI exposure per establishment ({a.prior}), slices; smoothing k = {a.k}; CPS firm-size shift lambda = {a.size_tilt}; {dict(lvl)}',
         'AI exposure measure: llm_beta_gpt4 (Eloundou et al. 2023, share of tasks exposed to LLMs incl. complementary software)', '']
    def wmean(xs): s = sum(w for w, _ in xs); return sum(w * v for w, v in xs) / s if s else float('nan')
    by_ind = collections.defaultdict(list); by_sz = collections.defaultdict(list); by_age = collections.defaultdict(list)
    disp = collections.defaultdict(list); flows = collections.defaultdict(lambda: [[], [], []])
    for (m, y, q, c, n, est), fs, fa, e, v, lh, ls, h, sp in rows:
        ind4 = n[:4]
        by_ind[(c, ind4)].append((e, v)); by_sz[(c, ind4, fs)].append((e, v))
        by_age[(c, ind4, 'young 0-5' if fa in ('1', '2', '3') else 'mature 6+')].append((e, v))
        disp[(m, y, q, c, n)].append((e, v))
        F = flows[(c, ind4)]; F[0].append((e, v))
        if lh != '': F[1].append((h, lh))
        if ls != '': F[2].append((sp, ls))
    L.append('Employment-weighted mean AI exposure (llm_beta_gpt4), vs the ACS industry x area estimate:')
    for (c, ind4), xs in sorted(by_ind.items()):
        n6 = ind4 + '00'
        acs = C.get((ind_key(n6), c)); acs_v = acs.est()[0].get('llm_beta_gpt4') if acs else None
        L.append(f'  {c} {ind4}: panel {wmean(xs):.3f}   ACS {acs_v:.3f} ({acs.n} records)' if acs_v is not None else f'  {c} {ind4}: panel {wmean(xs):.3f}')
    L.append('\nBy firm size class (0-19 ... 500+):')
    for c, ind4 in sorted(by_ind):
        L.append(f'  {c} {ind4}: ' + '  '.join(f'{k}: {wmean(by_sz[(c, ind4, k)]):.3f}' for k in priors.SIZES if by_sz.get((c, ind4, k))))
    L.append('\nBy firm age group:')
    for c, ind4 in sorted(by_ind):
        L.append(f'  {c} {ind4}: ' + '  '.join(f'{g}: {wmean(by_age[(c, ind4, g)]):.3f}' for g in ('young 0-5', 'mature 6+') if by_age.get((c, ind4, g))))
    L.append('\nWorkers, hires and separations: mean exposure')
    for (c, ind4), F in sorted(flows.items()):
        L.append(f'  {c} {ind4}: employment {wmean(F[0]):.3f}   hires {wmean(F[1]):.3f}   separations {wmean(F[2]):.3f}')
    sds = [st.pstdev([v for _, v in xs]) for xs in disp.values() if len(xs) >= 5]
    L.append(f'\nWithin-cell dispersion across establishments (SD of exposure, cells with >= 5 establishments): median {st.median(sds):.4f}, 90th pct {sorted(sds)[int(0.9 * len(sds))]:.4f}')
    print('\n'.join(L))
    open(os.path.join(a.out, f'exposure_report_{a.tag}.txt'), 'w').write('\n'.join(L) + '\n')


if __name__ == '__main__':
    main()
