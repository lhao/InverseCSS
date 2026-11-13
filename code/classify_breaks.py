"""Classify QCEW employment breaks by likely cause (DATA_CONDITIONS C2; design rule R3), to calibrate panel events.

Cells: published private (own 5) county x NAICS 6-digit cells; consecutive quarters 2021 Q1 -> 2024 Q4 (2021 in NAICS 2022
codes via the converted files, published cells only). Change: d_emp = month1(t) - month3(t-1); d_est = estabs(t) - estabs(t-1).
Break: both sides >= `--min-jobs` and month1(t) / month3(t-1) outside [1/1.5, 1.5], or the cell appears / disappears
with >= `--min-jobs`.

Each break is assigned, in this order:
  reclassification  an offsetting change (opposite sign, |size| within [0.8, 1.25] of the break) in ANOTHER industry
                    of the SAME county with the establishment count moving exactly the opposite way (e.g. -1 / +1); searched in the same 5-, 4-,
                    3-digit parent, then sector, then any industry (the level is recorded)
  relocation        an offsetting change in the SAME industry in ANOTHER county, establishment counts exactly opposite
  birth_death       unmatched, establishment count moves in the same direction as employment
  seasonal          unmatched, no establishment change, and the same quarter a year earlier broke in the same direction
  shock             unmatched, no establishment change (existing establishments grew or shrank)
  other             anything else (e.g. establishment count moves against employment)
Each cell can be matched once (greedy, largest breaks first).

Comparable cells only: published on both sides, or published on one side and truly absent (no row) on the other;
suppressed, imputed or converted cells are not comparable. Chance offsets exist in large counties, so a placebo
matches each quarter's breaks against changes from a non-adjacent quarter pair (no real link possible); the actual
match rate minus the placebo rate, and Q1 vs other quarters, are the calibrated reclassification signals.

Outputs (<out_dir>): breaks_classified.csv (one row per break), breaks_summary.txt
Usage: python3 classify_breaks.py <qcew_raw_root> <qcew_complete_dir> <out_dir> [--min-jobs 20]
"""
import argparse, collections, csv, math, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond

RATIO = 1.5                 # break threshold
MATCH_LO, MATCH_HI = 0.8, 1.25   # offsetting partner: |change| within this ratio of the break
# cross-sector matches (level 0) occur as often in the placebo as in the data (2026-10-16 run), so they are not used
MATCH_LEVELS = (5, 4, 3, 2)


def load(raw, complete):
    """{(y, q): ({(county, naics6): (m1, m3, estabs)} published cells, set of cells present in any status)}
    for private 6-digit county cells. A cell absent from the 'present' set has no establishments (true zero, A3)."""
    out = {}
    for y, q, qdir in cond.quarter_dirs(raw):
        cp = os.path.join(complete, f'qcew_complete_{y}q{q}_M20.csv.gz')
        d, present = {}, set()
        if os.path.exists(cp):
            with cond.open_text(cp) as fh:
                for r in csv.DictReader(fh):
                    if r['draw'] == '1' and r['own_code'] == '5' and r['level'] == '6' and r['area_fips'] != '24000':
                        present.add((r['area_fips'], r['industry_code']))
                        if r['status'] == 'published':
                            d[(r['area_fips'], r['industry_code'])] = (float(r['month1_emplvl']), float(r['month3_emplvl']), float(r['qtrly_estabs']))
        else:
            for f in cond.area_files(qdir):
                with cond.open_text(f) as fh:
                    for r in csv.DictReader(fh):
                        if r['agglvl_code'] == '78' and r['own_code'] == '5':
                            present.add((r['area_fips'], r['industry_code']))
                            if cond.qcew_status(r) in ('published', 'true_zero'):
                                d[(r['area_fips'], r['industry_code'])] = (cond.qcew_value(r, 'month1_emplvl'), cond.qcew_value(r, 'month3_emplvl'), float(r['qtrly_estabs']))
        out[(y, q)] = (d, present)
    return out


def changes(data, t, tp):
    """Comparable changes between quarter tp and t: cells published on both sides, or published on one side and truly
    absent (no row in any status) on the other. Suppressed / imputed / converted cells are not comparable."""
    (cur, cpres), (prv, ppres) = data[t], data[tp]
    out = {}
    for k in set(cur) | set(prv):
        if k in cur and k in prv: m1, e1, m3, e0 = cur[k][0], cur[k][2], prv[k][1], prv[k][2]
        elif k in cur and k not in ppres: m1, e1, m3, e0 = cur[k][0], cur[k][2], 0.0, 0.0
        elif k in prv and k not in cpres: m1, e1, m3, e0 = 0.0, 0.0, prv[k][1], prv[k][2]
        else: continue
        out[k] = (m1 - m3, e1 - e0, m1, m3)
    return out


def main():
    ap = argparse.ArgumentParser()
    for x in ('raw', 'complete', 'out'): ap.add_argument(x)
    ap.add_argument('--min-jobs', type=float, default=20)
    a = ap.parse_args()
    data = load(a.raw, a.complete)
    qs = sorted(data)
    prev_break = {}                      # (county, naics, quarter-of-year) -> sign of last year's break
    placebo = collections.defaultdict(collections.Counter)
    rows = []
    for i in range(1, len(qs)):
        (y, q), (py, pq) = qs[i], qs[i - 1]
        if (y * 4 + q) - (py * 4 + pq) != 1: continue
        chg = changes(data, (y, q), (py, pq))
        if not chg: continue
        # placebo: partners drawn from a non-adjacent quarter pair (two quarters later, or earlier at the end)
        j = i + 2 if i + 2 < len(qs) else i - 2
        pchg = changes(data, qs[j], qs[j - 1]) if j >= 1 else {}
        breaks = []
        for k, (de, dest, m1, m3) in chg.items():
            big = max(m1, m3)
            if big < a.min_jobs: continue
            if min(m1, m3) <= 0 or not (1 / RATIO <= m1 / m3 <= RATIO):
                breaks.append(k)
        used = set()
        by_county = collections.defaultdict(list); by_ind = collections.defaultdict(list)
        for k in chg: by_county[k[0]].append(k); by_ind[k[1]].append(k)
        def fits(b, p, pool=None):
            pool = pool or chg
            db, dp = chg[b][0], pool[p][0]
            if db == 0 or dp == 0 or (db > 0) == (dp > 0): return False
            if not (MATCH_LO <= abs(dp) / abs(db) <= MATCH_HI): return False
            eb, ep = chg[b][1], pool[p][1]
            return eb != 0 and ep == -eb                       # establishments move exactly between the two cells
        # placebo match rate per level, same rules, partner pool from the non-adjacent quarter pair
        by_county_p = collections.defaultdict(list)
        for k in pchg: by_county_p[k[0]].append(k)
        for b in breaks:
            c, ind = b
            for lv in (5, 4, 3, 2, 0):
                pre = (cond.SECTOR.get(ind[:2], ind[:2]) if lv == 2 else ind[:lv]) if lv else None
                if any(p != b and fits(b, p, pchg) and (lv == 0 or (cond.SECTOR.get(p[1][:2], p[1][:2]) if lv == 2 else p[1][:lv]) == pre) for p in by_county_p[c]):
                    placebo[(y, q)][lv] += 1; break
            placebo[(y, q)]['n'] += 1
        for b in sorted(breaks, key=lambda k: -abs(chg[k][0])):
            if b in used: continue
            c, ind = b
            de, dest, m1, m3 = chg[b]
            cls, level, partner = None, '', ''
            for lv in MATCH_LEVELS:
                pre = (cond.SECTOR.get(ind[:2], ind[:2]) if lv == 2 else ind[:lv]) if lv else None
                cands = [p for p in by_county[c] if p != b and p not in used and fits(b, p) and
                         (lv == 0 or (cond.SECTOR.get(p[1][:2], p[1][:2]) if lv == 2 else p[1][:lv]) == pre)]
                if cands:
                    p = min(cands, key=lambda p: abs(abs(chg[p][0]) - abs(de)))
                    cls, level, partner = 'reclassification', {5: '5-digit', 4: '4-digit', 3: '3-digit', 2: 'sector', 0: 'cross-sector'}[lv], p[1]
                    used.add(p); break
            if cls is None:
                cands = [p for p in by_ind[ind] if p != b and p not in used and fits(b, p)]
                if cands:
                    p = min(cands, key=lambda p: abs(abs(chg[p][0]) - abs(de)))
                    cls, partner = 'relocation', p[0]; used.add(p)
            if cls is None:
                ly = prev_break.get((c, ind, q))
                if dest != 0 and (dest > 0) == (de > 0): cls = 'birth_death'
                elif dest == 0 and ly is not None and ly == (de > 0): cls = 'seasonal'
                elif dest == 0: cls = 'shock'
                else: cls = 'other'
            used.add(b)
            rows.append({'year': y, 'quarter': q, 'county': c, 'naics6': ind, 'sector': cond.SECTOR.get(ind[:2], ind[:2]),
                         'month3_prev': m3, 'month1_cur': m1, 'd_emp': de, 'd_estabs': dest, 'class': cls, 'match_level': level, 'partner': partner})
        for k in breaks: prev_break[(k[0], k[1], q)] = chg[k][0] > 0
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, 'breaks_classified.csv'), 'w', newline='') as fh:
        w = csv.DictWriter(fh, list(rows[0])); w.writeheader(); w.writerows(rows)

    def share_table(sel, title):
        lines = [title, f'  {"group":16s} {"breaks":>7s} | ' + ' '.join(f'{c:>16s}' for c in CLASSES) + ' | jobs in breaks']
        for gname, gfun in GROUPS:
            r = [x for x in sel if gfun(x)]
            if not r: continue
            n = len(r); jobs = sum(abs(x['d_emp']) for x in r)
            parts = []
            for c in CLASSES:
                rc = [x for x in r if x['class'] == c]
                parts.append(f'{len(rc) / n:6.1%} ({sum(abs(x["d_emp"]) for x in rc) / jobs:5.1%})')
            lines.append(f'  {gname:16s} {n:7d} | ' + ' '.join(f'{p:>16s}' for p in parts) + f' | {jobs:,.0f}')
        return lines
    CLASSES = ['reclassification', 'relocation', 'birth_death', 'seasonal', 'shock', 'other']
    GROUPS = [('Q1 2022', lambda x: (x['year'], x['quarter']) == (2022, 1)),
              ('Q1 2023, 2024', lambda x: x['quarter'] == 1 and x['year'] in (2023, 2024)),
              ('Q2-Q4 all years', lambda x: x['quarter'] != 1)]
    out = share_table(rows, f'Share of breaks by class: count (and share of |change| in jobs), private county 6-digit cells, >= {a.min_jobs:g} jobs')
    # reclassification match level
    out += ['', 'Reclassification match rate vs placebo (share of breaks matched; placebo = partners from a non-adjacent quarter):',
            f'  {"group":16s} {"within 4-digit":>16s} {"within sector":>16s} {"cross-sector":>16s}']
    for gname, gfun in GROUPS:
        r = [x for x in rows if gfun(x)]; n = len(r)
        act = {lv: sum(1 for x in r if x['class'] == 'reclassification' and x['match_level'] in lvs) / n for lv, lvs in
               (('w4', ('5-digit', '4-digit')), ('ws', ('5-digit', '4-digit', '3-digit', 'sector')), ('x', ('cross-sector',)))}
        qq = [k for k in placebo if gfun({'year': k[0], 'quarter': k[1]})]
        pn = sum(placebo[k]['n'] for k in qq) or 1
        plc = {'w4': sum(placebo[k][5] + placebo[k][4] for k in qq) / pn, 'ws': sum(placebo[k][5] + placebo[k][4] + placebo[k][3] + placebo[k][2] for k in qq) / pn,
               'x': sum(placebo[k][0] for k in qq) / pn}
        out.append(f'  {gname:16s} ' + ' '.join(f'{act[k]:6.1%} vs {plc[k]:5.1%}' for k in ('w4', 'ws', 'x')))
    out += ['', 'Reclassification matches by level (count):']
    for gname, gfun in GROUPS:
        r = [x for x in rows if gfun(x) and x['class'] == 'reclassification']
        c = collections.Counter(x['match_level'] for x in r)
        out.append(f'  {gname:16s} ' + ', '.join(f'{k} {c[k]}' for k in ('5-digit', '4-digit', '3-digit', 'sector', 'cross-sector')))
    # per-quarter rates relative to the number of cells
    out += ['', 'Breaks and reclassification matches per 1,000 comparable cells, by quarter:']
    for (y, q) in qs[1:]:
        n_cells = len(changes(data, (y, q), qs[qs.index((y, q)) - 1]))
        r = [x for x in rows if (x['year'], x['quarter']) == (y, q)]
        out.append(f'  {y}Q{q}: cells {n_cells:5d}  breaks {1000 * len(r) / n_cells:6.1f}  reclass {1000 * sum(x["class"] == "reclassification" for x in r) / n_cells:5.1f}'
                   f'  relocation {1000 * sum(x["class"] == "relocation" for x in r) / n_cells:5.1f}  birth/death {1000 * sum(x["class"] == "birth_death" for x in r) / n_cells:5.1f}'
                   f'  shock+seasonal {1000 * sum(x["class"] in ("shock", "seasonal") for x in r) / n_cells:5.1f}')
    # by sector, Q1 2022 vs other Q1
    out += ['', 'Q1 by sector: breaks per 1,000 cells, and reclassification share (2022 vs 2023-24):']
    sec_cells = collections.Counter()
    for (y, q) in qs[1:]:
        if q != 1: continue
        for k in changes(data, (y, q), qs[qs.index((y, q)) - 1]): sec_cells[(y, cond.SECTOR.get(k[1][:2], k[1][:2]))] += 1
    secs = sorted({x['sector'] for x in rows})
    for s in secs:
        vals = []
        for yy in ((2022,), (2023, 2024)):
            r = [x for x in rows if x['quarter'] == 1 and x['year'] in yy and x['sector'] == s]
            nc = sum(sec_cells[(y, s)] for y in yy)
            vals.append(f'{1000 * len(r) / nc if nc else 0:6.1f} / {sum(x["class"] == "reclassification" for x in r) / len(r) if r else 0:5.1%}')
        if sum(1 for x in rows if x['quarter'] == 1 and x['sector'] == s) >= 10:
            out.append(f'  {s:6s} 2022: {vals[0]}   2023-24: {vals[1]}')
    print('\n'.join(out))
    open(os.path.join(a.out, 'breaks_summary.txt'), 'w').write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
