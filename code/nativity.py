"""Nativity as TABLES (design section 9; D14): US-born (N1), naturalised (N2), non-citizen (N3).

Changed 2026-10-24 (Hao): nativity is no longer written per establishment x worker type (which tripled the worker
rows and ran out of memory at county scale). The shares were already deterministic given the cell, the worker group
and the establishment's first firm-size class, so tables lose nothing; any establishment's nativity mix is its
worker mix x the share table. Worker group for nativity = race-ethnicity x education (sex and age left out to keep
ACS cells from getting thin; nativity varies most by race-ethnicity and education). The worker file is streamed twice.

QWI publishes no nativity, so it comes from surveys, as a SOFT constraint:
  1. base shares per nativity group (race-ethnicity x education) in the cell's industry and place-of-work area,
     ACS workplace 1-year 2021-2024, person weights, empirical-Bayes shrinkage (k records) towards coarser levels:
       systematic  industry x area x (race-ethnicity, education) -> industry x area -> industry (state)
       random      industry x area -> industry (nativity independent of worker type within the industry)
  2. firm-size tilt: the odds of being foreign-born x (CPS ASEC sector x firm-size foreign-born odds / sector odds)^lambda,
     national, lambda = 0.5 (no county evidence to estimate it; sensitivity option), applied by the establishment's firm
     size class when it first appears
  3. interval constraint: per county x 6-digit cell and version, if the implied foreign-born share of employment (all
     quarters) lies outside the ACS 95% interval for the industry x area (variance from the 80 replicate weights,
     4/80 x sum of squared deviations), all foreign-born odds in the cell are shifted (one log-odds shift) to the nearest
     bound; inside the interval nothing moves
  4. persistence: shares are fixed per cell x group x first firm-size class, so each nativity group keeps the stock-flow
     identity of worker_stockflow.py (naturalisation within 2021-2024 is ignored)
Caution: ACS largely misses Ocean City's seasonal J-1 workers (DATA_CONDITIONS D8), so summer nativity in Worcester
tourism is understated; nativity is the most model-dependent dimension (proposal) and the plasmode's never-tabulated one.

Outputs (<out_dir>):
  nativity_shares_<tag>.csv.gz  version, county, naics6, first firm-size class, race-ethnicity, education -> shares N1/N2/N3
  nativity_counts_<tag>.csv.gz  version, quarter, county, naics6, firm-size class, race-ethnicity, education, nativity ->
                                employment, hires, separations, stock start / end (summed over establishments, sex, age)
  nativity_report_<tag>.txt
Usage: python3 nativity.py <workers_dir> <acs_workers.parquet> <acs_raw.parquet> <cps_firmsize_sector.csv> <out_dir>
                           [--prior random|systematic] [--k 10] [--size-lambda 0.5]
"""
import argparse, collections, csv, gzip, math, os, statistics as st, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond
import priors

NAT = {'us_born': 'N1', 'naturalized': 'N2', 'noncitizen': 'N3'}
YOUNG = {'A01', 'A02', 'A03'}
CPS_MAP = {'1': ('Under 10', '10 to 24'), '2': ('10 to 24', '25 to 99'), '3': ('25 to 99', '100 to 499'), '4': ('100 to 499',), '5': ('500 to 999', '1000+')}


def re_group(race, eth):
    if eth == 'A2': return 'H'
    return {'A1': 'W', 'A2': 'B', 'A4': 'A'}.get(race, 'O')


def logit(p): p = min(max(p, 1e-6), 1 - 1e-6); return math.log(p / (1 - p))


def expit(x): return 1 / (1 + math.exp(-x))


def main():
    ap = argparse.ArgumentParser()
    for x in ('workers', 'acs', 'acs_raw', 'cps', 'out'): ap.add_argument(x)
    ap.add_argument('--prior', default='random', choices=('random', 'systematic'))
    ap.add_argument('--k', type=float, default=10.0); ap.add_argument('--size-lambda', type=float, default=0.5); ap.add_argument('--tag', default='slices')
    a = ap.parse_args()
    cond.start_run(a.workers, a.acs, a.acs_raw, a.cps)
    import pyarrow.parquet as pq
    acs = [p for p in pq.read_table(cond.require(a.acs), columns=['sample_type', 'YEAR', 'MULTYEAR', 'SERIAL', 'PERNUM', 'sector', 'naics3', 'naics4', 'powpuma_counties',
                                                              'PERWT', 'SEX', 'agegrp', 'education', 'race', 'ethnicity', 'nativity']).to_pylist() if p['sample_type'] == '1-year' and p['nativity'] in NAT]
    rw_cols = [f'REPWTP{i}' for i in range(1, 81)]
    raw = pq.read_table(cond.require(a.acs_raw), columns=['YEAR', 'MULTYEAR', 'SERIAL', 'PERNUM'] + rw_cols).to_pylist()
    rep = {(r['YEAR'], r['MULTYEAR'], r['SERIAL'], r['PERNUM']): [r[c] for c in rw_cols] for r in raw}
    del raw
    have4 = {p['naics4'] for p in acs if p['naics4']}; have3 = {p['naics3'] for p in acs if p['naics3']}
    def ind_key(n6): return ('4', n6[:4]) if n6[:4] in have4 else (('3', n6[:3]) if n6[:3] in have3 else ('s', priors.bds_sector(n6)))
    # conditioning cells: counts and weighted nativity totals; direct estimates with replicate weights at industry x area
    C = collections.defaultdict(lambda: [0, collections.Counter()])
    D = collections.defaultdict(lambda: [0.0, 0.0, [0.0] * 80, [0.0] * 80])   # w, w*fb, rep w, rep w*fb
    for p in acs:
        e = 'E5' if p['agegrp'] in YOUNG else p['education']; n = NAT[p['nativity']]; fb = 1.0 if n != 'N1' else 0.0
        areas = (p['powpuma_counties'] or '').replace(',', ' ').split()
        rw = rep.get((p['YEAR'], p['MULTYEAR'], p['SERIAL'], p['PERNUM']))
        for k in (('4', p['naics4']), ('3', p['naics3']), ('s', p['sector'])):
            if not k[1]: continue
            for key in [(k, 'MD')] + [(k, ar) for ar in areas] + [(k, ar, re_group(p['race'], p['ethnicity']), e) for ar in areas]:
                C[key][0] += 1; C[key][1][n] += p['PERWT']
            for ar in areas:
                d = D[(k, ar)]; d[0] += p['PERWT']; d[1] += p['PERWT'] * fb
                if rw:
                    for i in range(80): d[2][i] += rw[i]; d[3][i] += rw[i] * fb
    del acs, rep   # summarised in C and D; free the records and replicate weights (memory, 2026-10-25)
    def est(key):
        c = C.get(key)
        if not c or c[0] == 0: return None, 0
        t = sum(c[1].values()); return ({n: c[1][n] / t for n in ('N1', 'N2', 'N3')} if t > 0 else None), c[0]
    def shrink(cell, n_, parent):
        if cell is None: return parent
        w = n_ / (n_ + a.k); return {x: w * cell[x] + (1 - w) * parent[x] for x in parent}
    cache = {}
    def base(n6, county, t):
        ik = ind_key(n6); key = (ik, county, t if a.prior == 'systematic' else None)
        if key in cache: return cache[key]
        b, _ = est((ik, 'MD')); b = b or {'N1': 0.8, 'N2': 0.12, 'N3': 0.08}
        c, nn = est((ik, county)); b = shrink(c, nn, b)
        if a.prior == 'systematic':
            r, e = t
            c, nn = est((ik, county, r, e)); b = shrink(c, nn, b)
        cache[key] = b; return b
    # CPS firm-size odds ratios by sector
    cps = collections.defaultdict(dict)
    with open(cond.require(a.cps)) as fh:
        for r in csv.DictReader(fh): cps[r['sector']][r['firmsize']] = (float(r['workers']), float(r['foreign_born']))
    def size_tilt(n6, fs):
        d = cps.get(priors.bds_sector(n6))
        if not d: return 1.0
        tot = sum(w for w, _ in d.values()); avg = sum(w * f for w, f in d.values()) / tot
        sel = [d[c] for c in CPS_MAP.get(fs, ()) if c in d]
        if not sel or not 0 < avg < 1: return 1.0
        fbk = sum(w * f for w, f in sel) / sum(w for w, _ in sel)
        return math.exp(a.size_lambda * (logit(fbk) - logit(avg)))
    def shares(n6, county, grp, fs0, delta):
        b = base(n6, county, grp); fb = b['N2'] + b['N3']
        if fb <= 0: return {'N1': 1.0, 'N2': 0.0, 'N3': 0.0}
        odds = fb / max(b['N1'], 1e-9) * size_tilt(n6, fs0)
        p = expit(math.log(odds) + delta)
        return {'N1': 1 - p, 'N2': p * b['N2'] / fb, 'N3': p * b['N3'] / fb}
    src = cond.require(os.path.join(a.workers, f'workers_sf_{a.tag}.csv.gz'))
    MEAS = ('emp', 'hires', 'separations', 'stock_start', 'stock_end')
    # pass 1 (streamed): each establishment's first firm-size class; per cell, employment by base foreign-born log-odds
    first_fs = {}; pooled = collections.defaultdict(collections.Counter); groups = set()
    KEYF = ('version', 'year', 'quarter', 'county', 'naics6', 'firm_size_class', 're', 'education'); I = sys.intern
    with cond.open_text(src) as fh:
        for r in csv.DictReader(fh):
            for f in KEYF: r[f] = I(r[f])
            fs0 = first_fs.setdefault((r['version'], r['est_id']), r['firm_size_class'])
            e = float(r['emp'] or 0)
            if e <= 0: continue
            g = (r['re'], r['education']); groups.add((r['version'], r['county'], r['naics6'], fs0, g))
            sh0 = shares(r['naics6'], r['county'], g, fs0, 0.0); fb0 = sh0['N2'] + sh0['N3']
            pooled[(r['version'], r['county'], r['naics6'])][round(logit(fb0), 6) if 0 < fb0 < 1 else (-50.0 if fb0 <= 0 else 50.0)] += e
    # ACS interval constraint per cell x version: one log-odds shift to the nearest bound when outside the interval
    deltas = {}; bound_log = collections.Counter(); comp = []
    for key, pl in pooled.items():
        m, c, n6 = key
        d = D.get((ind_key(n6), c)); den_ = sum(pl.values())
        def implied(delta):
            return sum(e * expit(lo_ + delta) for lo_, e in pl.items()) / den_ if den_ else 0.0
        f0 = implied(0.0)
        if not d or d[0] <= 0: deltas[key] = 0.0; bound_log['no_acs_interval'] += 1; continue
        theta = d[1] / d[0]
        reps = [d[3][i] / d[2][i] for i in range(80) if d[2][i] > 0]
        se = math.sqrt(4 / 80 * sum((x - theta) ** 2 for x in reps)) if len(reps) == 80 else 0.05
        lo, hi = max(theta - 1.96 * se, 0.0), min(theta + 1.96 * se, 1.0)
        target = lo if f0 < lo else (hi if f0 > hi else None)
        if target is None: deltas[key] = 0.0; bound_log['inside_interval'] += 1
        else:
            a_, b_ = -6.0, 6.0
            for _ in range(40):
                mid = (a_ + b_) / 2
                if implied(mid) < target: a_ = mid
                else: b_ = mid
            deltas[key] = (a_ + b_) / 2; bound_log['moved_to_bound'] += 1
        comp.append((c, n6, m, f0, implied(deltas[key]), theta, lo, hi, d[0]))
    os.makedirs(a.out, exist_ok=True)
    # share table
    p_sh = os.path.join(a.out, f'nativity_shares_{a.tag}.csv.gz')
    with gzip.open(p_sh + '.part', 'wt', newline='') as fo:
        w = csv.writer(fo); w.writerow(['version', 'county', 'naics6', 'first_firm_size_class', 're', 'education', 'logodds_shift', 'N1', 'N2', 'N3'])
        for m, c, n6, fs0, g in sorted(groups):
            dl = deltas.get((m, c, n6), 0.0); sh = shares(n6, c, g, fs0, dl)
            w.writerow([m, c, n6, fs0, g[0], g[1], round(dl, 6)] + [round(sh[n], 6) for n in ('N1', 'N2', 'N3')])
    os.replace(p_sh + '.part', p_sh)
    # pass 2 (streamed): counts by cell x quarter x firm-size class x group x nativity, summed over establishments, sex, age
    cnt = collections.defaultdict(lambda: [0.0] * len(MEAS))
    fbshare = collections.defaultdict(lambda: [0.0, 0.0])
    with cond.open_text(src) as fh:
        for r in csv.DictReader(fh):
            for f in KEYF: r[f] = I(r[f])
            g = (r['re'], r['education']); fs0 = first_fs[(r['version'], r['est_id'])]
            sh = shares(r['naics6'], r['county'], g, fs0, deltas.get((r['version'], r['county'], r['naics6']), 0.0))
            vals = [float(r[x]) if r.get(x, '') != '' else 0.0 for x in MEAS]
            base_k = (r['version'], r['year'], r['quarter'], r['county'], r['naics6'], r['firm_size_class'], g[0], g[1])
            for n, p in sh.items():
                acc = cnt[base_k + (n,)]
                for i, v in enumerate(vals): acc[i] += v * p
            e = vals[0]; k = (r['county'], r['naics6'][:4], r['firm_size_class']); fbshare[k][0] += e * (sh['N2'] + sh['N3']); fbshare[k][1] += e
    p_ct = os.path.join(a.out, f'nativity_counts_{a.tag}.csv.gz')
    with gzip.open(p_ct + '.part', 'wt', newline='') as fo:
        w = csv.writer(fo); w.writerow(['version', 'year', 'quarter', 'county', 'naics6', 'firm_size_class', 're', 'education', 'nativity'] + list(MEAS))
        for k in sorted(cnt):
            v = cnt[k]
            if any(x >= 0.0005 for x in v): w.writerow(list(k) + [round(x, 4) for x in v])
    os.replace(p_ct + '.part', p_ct)
    L = [f'Nativity ({a.prior}) as tables, {a.tag}; group = race-ethnicity x education; k = {a.k}, firm-size lambda = {a.size_lambda}; '
         f'ACS interval constraint: {dict(bound_log)}; share rows {len(groups):,}, count rows {len(cnt):,}',
         '', 'Foreign-born share of employment by cell (version 1): model before constraint -> after; ACS direct estimate [95% interval] (records)']
    for c, n6, m, f0, f1, th, lo, hi, wsum in sorted(comp):
        if m == '1': L.append(f'  {c} {n6}: {f0:.3f} -> {f1:.3f}   ACS {th:.3f} [{lo:.3f}, {hi:.3f}]')
    L.append('\nForeign-born share by industry x firm size class (0-19 ... 500+), all quarters and versions:')
    for c, i4 in sorted({(k[0], k[1]) for k in fbshare}):
        L.append(f'  {c} {i4}: ' + '  '.join(f'{fs}: {fbshare[(c, i4, fs)][0] / fbshare[(c, i4, fs)][1]:.3f}' for fs in priors.SIZES if fbshare.get((c, i4, fs), [0, 0])[1] > 0))
    L.append(cond.run_line())
    print('\n'.join(L)); open(os.path.join(a.out, f'nativity_report_{a.tag}.txt'), 'w').write('\n'.join(L) + '\n')

if __name__ == '__main__':
    main()
