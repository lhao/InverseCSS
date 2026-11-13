"""Step 3c: calibrate industry-reclassification rates per establishment by injection and recovery (DATA_CONDITIONS C2, C9).

Observed (classify_breaks.py, Table L): reclassification events DETECTED as offsetting breaks, in excess of a placebo,
per 1,000 comparable county x 6-digit cells per quarter. Detection is rare: the moved establishment must make a break
(>= 1.5x, >= 20 jobs) and nothing else may move the establishment counts of the two cells that quarter.
So the observed rate understates true reclassification, and the per-establishment rate rho is calibrated so that
  rho x E = target,  E = expected detected events per cell per unit rho = sum over comparable origin cells of
                          (establishments x detection probability) / number of comparable cells,
with detection probability measured by injecting one synthetic move at a time into the REAL QCEW changes of that quarter
pair (all other real events present) and running the same detection rules as classify_breaks.py:
  origin A: a comparable cell published in both quarters with >= 1 establishment; destination B: a comparable cell in
  the same county and sector (weights 4 / 2 / 1 / 1 for the same 5-, 4-, 3-digit parent, sector; as in build_panel);
  moved size: an establishment drawn from A's CBP size classes (uniform within the class), capped at A's employment;
  detected if A or B breaks after the move and the pair fits (opposite signs, |sizes| within 0.8-1.25, establishment
  counts exactly opposite).
Target = (within-sector match share - placebo share) x breaks per 1,000 cells, by group (Q1 2022, Q1 2023-24, Q2-Q4),
with a 90% interval from the binomial standard errors of both shares.

Outputs (<out_dir>): reclass_rates.csv (group, rho, interval, target, E), calibration_report.txt
Usage: python3 calibrate_reclass.py <qcew_raw_root> <qcew_complete_dir> <cbp_dir> <breaks_dir> <out_dir> [--k 20] [--seed 17]
"""
import argparse, collections, csv, math, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond
import classify_breaks as cb
import create_establishments as ce

GROUPS = {'q1_2022': lambda y, q: (y, q) == (2022, 1), 'q1': lambda y, q: q == 1 and y in (2023, 2024),
          'other': lambda y, q: q != 1}
LABEL = {'q1_2022': 'Q1 2022', 'q1': 'Q1 2023, 2024', 'other': 'Q2-Q4 all years'}


def sector(n): return cond.SECTOR.get(n[:2], n[:2])


def is_break(m1, m3, min_jobs):
    return max(m1, m3) >= min_jobs and (min(m1, m3) <= 0 or not (1 / cb.RATIO <= m1 / m3 <= cb.RATIO))


def fits(da, ea, db, eb):
    if da == 0 or db == 0 or (da > 0) == (db > 0): return False
    if not (cb.MATCH_LO <= abs(db) / abs(da) <= cb.MATCH_HI): return False
    return ea != 0 and eb == -ea


def targets(breaks_dir):
    """Placebo-corrected detected reclassification events per 1,000 comparable cells, with standard errors."""
    rows = list(csv.DictReader(open(cond.require(os.path.join(breaks_dir, 'breaks_classified.csv')))))
    summ = open(cond.require(os.path.join(breaks_dir, 'breaks_summary.txt'))).read()
    rates = open(cond.require(os.path.join(breaks_dir, 'event_rates.txt'))).read()
    out = {}
    for g, f in GROUPS.items():
        r = [x for x in rows if f(int(x['year']), int(x['quarter']))]
        n = len(r)
        act = sum(1 for x in r if x['class'] == 'reclassification' and x['match_level'] in ('5-digit', '4-digit', '3-digit', 'sector')) / n
        m = re.search(re.escape(LABEL[g]) + r'\s+[\d.]+% vs\s+[\d.]+%\s+([\d.]+)% vs\s+([\d.]+)%', summ)
        plc = float(m.group(2)) / 100
        lab = {'q1_2022': 'Q1 2022', 'q1': 'Q1 2023-24', 'other': 'Q2-Q4'}[g]
        per1000 = float(re.search(re.escape(lab) + r'\s+\d+(?:\s+[\d.]+){6}\s+([\d.]+)', rates).group(1))
        se = math.sqrt(act * (1 - act) / n + plc * (1 - plc) / n)
        out[g] = {'breaks': n, 'actual_share': act, 'placebo_share': plc, 'breaks_per_1000': per1000,
                  'target': (act - plc) * per1000, 'target_se': se * per1000}
    return out


def main():
    ap = argparse.ArgumentParser()
    for x in ('raw', 'complete', 'cbp', 'breaks', 'out'): ap.add_argument(x)
    ap.add_argument('--k', type=int, default=20, help='injections per origin cell and quarter pair')
    ap.add_argument('--seed', type=int, default=17); ap.add_argument('--min-jobs', type=float, default=20)
    a = ap.parse_args()
    cond.start_run(*[v for v in vars(a).values() if isinstance(v, str) and os.path.exists(v)])
    tg = targets(a.breaks)
    data = cb.load(a.raw, a.complete)
    cbp = ce.load_cbp(a.cbp, None)
    qs = sorted(data)
    acc = {g: collections.Counter() for g in GROUPS}
    by_size = collections.defaultdict(lambda: [0, 0])     # origin size band -> [injections, detected]
    for i in range(1, len(qs)):
        (y, q), (py, pq) = qs[i], qs[i - 1]
        if (y * 4 + q) - (py * 4 + pq) != 1: continue
        g = next(k for k, f in GROUPS.items() if f(y, q))
        chg = cb.changes(data, (y, q), (py, pq))
        prv, cur = data[(py, pq)][0], data[(y, q)][0]
        acc[g]['cells'] += len(chg); acc[g]['quarters'] += 1
        by_cs = collections.defaultdict(list)
        for k in chg: by_cs[(k[0], sector(k[1]))].append(k)
        shares_cache = {}
        for A, (dA, eA, m1A, m3A) in chg.items():
            if A not in prv or A not in cur: continue
            N = int(round(prv[A][2]))
            if N < 1 or m3A <= 0: continue
            dests = [B for B in by_cs[(A[0], sector(A[1]))] if B != A]
            acc[g]['origin_estabs'] += N
            if not dests: continue
            w = [4 if B[1][:5] == A[1][:5] else 2 if B[1][:4] == A[1][:4] else 1 for B in dests]
            sh = shares_cache.get(A)
            if sh is None: sh = shares_cache[A] = ce.class_shares(cbp[min(py, 2023)], A[0], A[1])[0] or {'n<5': 1.0}
            rng = cond.rng_for(a.seed, y, q, *A)
            det = 0
            for _ in range(a.k):
                u = rng.random() * sum(w); B = dests[-1]
                for bb, ww in zip(dests, w):
                    u -= ww
                    if u <= 0: B = bb; break
                ui = rng.random(); cls = ce.CBP_CLASSES[-1]
                for k_, lo, hi in ce.CBP_CLASSES:
                    ui -= sh.get(k_, 0.0)
                    if ui <= 0: cls = (k_, lo, hi); break
                s = min(cls[1] + rng.random() * (min(cls[2], 2000) - cls[1]), m3A)
                dB, eB, m1B, m3B = chg[B]
                a_d, a_e, a_m1 = dA - s, eA - 1, m1A - s
                b_d, b_e, b_m1 = dB + s, eB + 1, m1B + s
                hit = (is_break(a_m1, m3A, a.min_jobs) or is_break(b_m1, m3B, a.min_jobs)) and fits(a_d, a_e, b_d, b_e)
                det += hit
                band = '<20' if m3A < 20 else '20-99' if m3A < 100 else '100-499' if m3A < 500 else '500+'
                by_size[(g, band)][0] += 1; by_size[(g, band)][1] += hit
            acc[g]['E_num'] += N * det / a.k
    os.makedirs(a.out, exist_ok=True)
    res = []
    L = ['Step 3c: reclassification rates per establishment-quarter, calibrated by injection and recovery',
         f'(k = {a.k} injections per origin cell and quarter pair; detection rules as classify_breaks.py)', '',
         f'{"group":16s} {"target /1000":>12s} {"(90% interval)":>16s} {"E /1000 per rho":>15s} {"detect. prob.":>13s} {"rho":>8s} {"(90% interval)":>18s} {"provisional":>11s}']
    prov = {'q1_2022': 0.006, 'q1': 0.0025, 'other': 0.0004}
    for g in GROUPS:
        A = acc[g]; t = tg[g]
        E = 1000 * A['E_num'] / A['cells']                       # detected events per 1,000 cells if rho = 1
        pdet = A['E_num'] / A['origin_estabs']
        lo_t, hi_t = t['target'] - 1.645 * t['target_se'], t['target'] + 1.645 * t['target_se']
        rho, rlo, rhi = max(t['target'], 0) / E, max(lo_t, 0) / E, max(hi_t, 0) / E
        res.append({'group': g, 'rho': rho, 'rho_lo90': rlo, 'rho_hi90': rhi, 'target_per_1000': t['target'], 'target_se': t['target_se'],
                    'E_per_1000_per_rho': E, 'detection_prob': pdet, 'cells_per_quarter': A['cells'] / A['quarters'],
                    'estabs_per_cell': A['origin_estabs'] / A['cells']})
        L.append(f'{LABEL[g]:16s} {t["target"]:12.2f} {f"({lo_t:.2f}, {hi_t:.2f})":>16s} {E:15.2f} {pdet:13.4f} {rho:8.4f} {f"({rlo:.4f}, {rhi:.4f})":>18s} {prov[g]:11.4f}')
    L += ['', 'target: (within-sector match share - placebo share) x breaks per 1,000 comparable cells (Table L inputs):']
    for g in GROUPS:
        t = tg[g]; L.append(f'  {LABEL[g]:16s} breaks {t["breaks"]:4d}  actual {t["actual_share"]:.1%}  placebo {t["placebo_share"]:.1%}  breaks per 1,000 cells {t["breaks_per_1000"]:.1f}')
    L += ['', 'detection probability of one injected move, by origin cell size (jobs):']
    for g in GROUPS:
        L.append(f'  {LABEL[g]:16s} ' + '  '.join(f'{b}: {by_size[(g, b)][1] / by_size[(g, b)][0]:.3f}' for b in ('<20', '20-99', '100-499', '500+') if by_size[(g, b)][0]))
    with open(os.path.join(a.out, 'reclass_rates.csv'), 'w', newline='') as fh:
        w = csv.DictWriter(fh, list(res[0])); w.writeheader(); w.writerows(res)
    open(os.path.join(a.out, 'calibration_report.txt'), 'w').write('\n'.join(L) + '\n')
    print('\n'.join(L))


if __name__ == '__main__':
    main()
