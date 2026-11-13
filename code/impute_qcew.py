"""Multiple imputation of suppressed QCEW county cells (pilot: one state, one quarter).

Method ("allocation"): top-down through county -> ownership -> NAICS 2..6 digit.
At every parent whose value is known (published, or imputed earlier in the same draw),
the leftover = parent - sum(published children) is split among the suppressed children
with a Dirichlet-multinomial draw. Prior weight of a suppressed child = establishments x
typical size, where typical size comes from (1) the same cell in adjacent quarters,
(2) the same quarter in adjacent years, (3) the same industry/ownership statewide,
(4) statewide parents. Employment months share one draw of shares; wages use weights
x typical wage per worker. Finally the suppressed 6-digit county cells are raked so that
counties also add up to the published state 6-digit totals.

Baseline ("hotdeck"): the hierarchical hot-deck - draw a donor at random from published
6-digit cells of the same industry and ownership in the state (other counties); if none,
from the 5-, 4-, 3-, 2-digit parent industry.

Validation (--validate F): additionally hide a share F of published 6-digit cells that
have <= 3 establishments and at least one suppressed sibling, impute, and score both methods
on the hidden cells.

Random streams (F4, ANALYSIS_STRATEGY S4a): one per draw x county x parent group (x quarter), so a draw can be built
alone (--versions) and states or counties never share numbers. --persistent leaves the quarter out of the key: a version
then uses the same numbers for the same parent group every quarter (imputed shares persistent over time).

Ablation options (drafts/ablation_note.md; defaults leave the production method unchanged):
  --no-rake  arm A, impute only: skip the raking of county cells to the published state totals
  --point    arm B, rake only (use with --draws 1): split each leftover in proportion to the prior weights,
             with no Dirichlet, multinomial or random-zero draw
  --hotdeck-sized  with --validate: also score a size-matched hot-deck ("hotdecksz": donors with the recipient's
             establishment count; added 2026-11-06). Off by default, so earlier outputs are reproduced unchanged.

Usage:
  python3 impute_qcew.py <qcew_root> <year> <qtr> <out_dir> [--draws M] [--alpha A] [--seed S] [--validate F] [--persistent]
                                                            [--no-rake] [--point]
"""
import argparse, collections, csv, gzip, math, os, random, statistics as st, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond   # shared handling of known data conditions (docs/DATA_CONDITIONS.md)

MONTHS = ('month1_emplvl', 'month2_emplvl', 'month3_emplvl')
VARS = MONTHS + ('total_qtrly_wages',)
SECTOR, COUNTY_LVL, STATE_LVL = cond.SECTOR, cond.COUNTY_AGGLVL, cond.STATE_AGGLVL
ZERO_PROB = {1: 0.07, 2: 0.01}   # P(all months zero | establishments), from published MD 2022 Q2 cells


def parent_ind(ind, depth):
    """Industry code one level up (depth = level of ind: 2..6)."""
    return cond.parent_industry(ind, depth)


def load_area(path, lvls):
    out = {}
    if not os.path.exists(path): return out          # neighbouring quarters outside the window are optional (E3)
    with cond.open_text(path) as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        lv = lvls.get(r['agglvl_code'])
        if lv is None: continue
        rec = {'lv': lv, 'disc': r['disclosure_code'], 'estabs': int(r['qtrly_estabs'])}
        for v in VARS: rec[v] = float(r[v])
        out[(r['own_code'], r['industry_code'])] = rec
    return out


def key_parent(own, ind, lv):
    if lv == 1: return ('0', '10')
    return (own, parent_ind(ind, lv) if lv >= 2 else None)


def quarter_shift(y, q, k):
    i = y * 4 + (q - 1) + k
    return i // 4, i % 4 + 1


class Priors:
    """Typical employment per establishment and wage per worker for a cell."""
    def __init__(self, root, y, q, counties):
        self.nb = []   # list of {county: {key: rec}} for neighbours in preference order
        for k in ((-1, 1), (-4, 4)):
            group = []
            for s in k:
                yy, qq = quarter_shift(y, q, s)
                group.append({c: load_area(f'{root}/{yy}q{qq}/{c}.csv', COUNTY_LVL) for c in counties})
            self.nb.append(group)
        self.state = load_area(f'{root}/{y}q{q}/24000.csv', STATE_LVL)

    @staticmethod
    def _rates(rec):
        emp = sum(rec[m] for m in MONTHS) / 3
        if rec['disc'] != '' or rec['estabs'] == 0 or emp <= 0: return None
        return emp / rec['estabs'], rec['total_qtrly_wages'] / emp

    def get(self, county, own, ind, lv):
        for group in self.nb:   # adjacent quarters, then adjacent years
            vals = [self._rates(g[county][(own, ind)]) for g in group if (own, ind) in g[county]]
            vals = [v for v in vals if v]
            if vals: return tuple(st.mean(x) for x in zip(*vals)), 'neighbour'
        k, d = (own, ind), lv
        while d >= 1:   # statewide same industry, then statewide parents
            if k in self.state and self._rates(self.state[k]): return self._rates(self.state[k]), f'state_l{d}'
            k = key_parent(k[0], k[1], d); d -= 1
        return None, 'none'


def dirichlet(rng, alphas):
    g = [rng.gammavariate(max(a, 1e-6), 1.0) for a in alphas]
    s = sum(g)
    return [x / s for x in g] if s > 0 else [1 / len(g)] * len(g)


def multinomial(rng, n, p):
    """Integer split of n by probabilities p (sequential binomials via normal approx when large)."""
    n, out, rest = int(round(n)), [], 1.0
    for i, pi in enumerate(p):
        if i == len(p) - 1: out.append(n); break
        pr = min(1.0, pi / rest) if rest > 0 else 0
        if n > 50:
            k = int(round(rng.gauss(n * pr, math.sqrt(max(n * pr * (1 - pr), 1e-9)))))
        else:
            k = sum(rng.random() < pr for _ in range(n))
        k = max(0, min(n, k)); out.append(k); n -= k; rest -= pi
    return out


class County:
    """Known values, hierarchy and floors for one county within one draw."""
    def __init__(self, cells):
        self.cells = cells
        self.kids = collections.defaultdict(list)
        for k, r in cells.items():
            if r['lv'] >= 1: self.kids[key_parent(k[0], k[1], r['lv'])].append(k)
        self.val = {k: {v: (0.0 if r['disc'] == '-' else r[v]) for v in VARS}
                    for k, r in cells.items() if r['disc'] in ('', '-')}
        self.published = set(self.val)
        self.floor = {}

    def fl(self, k):
        """What the published descendants of a suppressed cell already account for."""
        if k not in self.floor:
            self.floor[k] = {v: sum(self.val[c][v] if c in self.published else self.fl(c)[v]
                                    for c in self.kids.get(k, [])) for v in VARS}
        return self.floor[k]


def allocate_level(key, cty, county, lv, priors, alpha, log, point=False):
    """Fill suppressed cells at level lv whose parent is known. Returns {parent: [suppressed children]}."""
    groups = {}
    for p in [k for k in cty.val if cty.cells.get(k, {'lv': 0})['lv'] == lv - 1 or (lv == 1 and k == ('0', '10'))]:
        ch = cty.kids.get(p, [])
        unk = [k for k in ch if k not in cty.val and cty.cells[k]['lv'] == lv]
        if not unk: continue
        rng = cond.rng_for(*key, county, *p)   # one stream per draw x county x parent group (F4)
        known = [k for k in ch if k in cty.val]
        w, wg = [], []
        for k in unk:
            r = cty.cells[k]
            rates, src = priors.get(county, k[0], k[1], r['lv'])
            size, wage = rates or (1.0, 1.0)
            log['prior_' + src] += 1
            z = (not point) and rng.random() < ZERO_PROB.get(r['estabs'], 0.0)
            ww = 0.0 if z or r['estabs'] == 0 else r['estabs'] * size
            w.append(ww); wg.append(ww * wage)
        for k in unk: cty.val[k] = {}
        for weights, names in ((w, MONTHS), (wg, ('total_qtrly_wages',))):
            tot = sum(weights)
            base = [x / tot for x in weights] if tot > 0 else [1 / len(weights)] * len(weights)
            share = base if point else dirichlet(rng, [alpha * b for b in base])
            for v in names:
                floors = [cty.fl(k)[v] for k in unk]
                left = cty.val[p][v] - sum(cty.val[k][v] for k in known) - sum(floors)
                if left < -0.5: log['negative_leftover'] += 1
                left = max(left, 0.0)
                parts = multinomial(rng, left, share) if v in MONTHS and not point else [left * x for x in share]
                for k, x, f0 in zip(unk, parts, floors):
                    cty.val[k][v] = float(x) + f0
        groups[p] = unk
    return groups


def rake_level(ctys, groups_by_county, state, lv, log, iters=50, apply=True):
    """Adjust the above-floor part of suppressed level-lv county cells so counties add up to
    published state cells, keeping every county parent's total fixed. apply=False (ablation arm A) changes
    nothing and only logs the gaps to the state totals."""
    for v in VARS:
        rows, cols = {}, collections.defaultdict(list)
        for c, groups in groups_by_county.items():
            for p, unk in groups.items():
                rows[(c, p)] = [(c, k) for k in unk]
                for k in unk: cols[k].append((c, k))
        x = {ck: ctys[ck[0]].val[ck[1]][v] - ctys[ck[0]].fl(ck[1])[v] for r in rows.values() for ck in r}
        row_t = {g: sum(x[ck] for ck in m) for g, m in rows.items()}
        # keep support: a cell drawn at exactly 0 could never be scaled up to meet a state total
        for ck in x:
            if apply and ctys[ck[0]].cells[ck[1]]['estabs'] > 0: x[ck] = max(x[ck], 1e-3 * ctys[ck[0]].cells[ck[1]]['estabs'])
        col_t = {}
        for k, m in cols.items():
            s = state.get(k)
            if not s or s['disc'] != '': continue
            pub = sum(ctys[c].val[k][v] for c in ctys if k in ctys[c].published)
            col_t[k] = max(s[v] - pub - sum(ctys[c].fl(k)[v] for c, _ in m), 0.0)
        for _ in range(iters if apply else 0):
            for k, t in col_t.items():
                cur = sum(x[ck] for ck in cols[k])
                if cur > 0:
                    for ck in cols[k]: x[ck] *= t / cur
            for g, m in rows.items():
                cur = sum(x[ck] for ck in m)
                if cur > 0:
                    for ck in m: x[ck] *= row_t[g] / cur
        if apply:
            for ck, xv in x.items():
                ctys[ck[0]].val[ck[1]][v] = xv + ctys[ck[0]].fl(ck[1])[v]
        gaps = [(abs(sum(x[ck] for ck in cols[k]) - t), t) for k, t in col_t.items()]
        big = sum(1 for g, t in gaps if g > max(5.0, 0.02 * t))
        log[f'L{lv}_{v}_state_targets'] += len(gaps); log[f'L{lv}_{v}_state_misses_gt2pct'] += big
        log[f'L{lv}_{v}_abs_gap'] += sum(g for g, t in gaps); log[f'L{lv}_{v}_target_total'] += sum(t for g, t in gaps)


def draw_once(key, cells_by_county, priors, alpha, log, rake=True, point=False):
    ctys = {c: County(cells) for c, cells in cells_by_county.items()}
    for lv in range(1, 7):
        groups = {c: allocate_level(key, ctys[c], c, lv, priors, alpha, log, point) for c in ctys}
        rake_level(ctys, groups, priors.state, lv, log, apply=rake)
    return {c: {k: cty.val[k] for k in cty.val if k not in cty.published} for c, cty in ctys.items()}


def hotdeck(key, county, k, rec, cells_by_county):
    """Hierarchical hot-deck donor draw (levels)."""
    own, ind = k; rng = cond.rng_for(*key, 'hotdeck', county, own, ind)
    for d in (6, 5, 4, 3, 2):
        code = ind if d == 6 else (SECTOR.get(ind[:2], ind[:2]) if d == 2 else ind[:d])
        pool = [cells[(o, i)] for c, cells in cells_by_county.items() if c != county
                for (o, i), r in cells.items()
                if o == own and r['lv'] == 6 and r['disc'] == '' and
                (i == code if d == 6 else (SECTOR.get(i[:2], i[:2]) == code if d == 2 else i.startswith(code)))]
        if pool:
            r = rng.choice(pool); return {v: r[v] for v in VARS}
    return {v: 0.0 for v in VARS}


def hotdeck_sized(key, county, k, rec, cells_by_county):
    """Size-matched hot-deck (added 2026-11-06; Harman's reading note H16). Same hierarchical donor pools as hotdeck(),
    but the donor must have the recipient's establishment count. If no level has such a donor, a donor from the first
    non-empty pool is scaled by (recipient establishments / donor establishments). Own random stream."""
    own, ind = k; rng = cond.rng_for(*key, 'hotdeck_sized', county, own, ind)
    n_rec, first_pool = rec['estabs'], None
    for d in (6, 5, 4, 3, 2):
        code = ind if d == 6 else (SECTOR.get(ind[:2], ind[:2]) if d == 2 else ind[:d])
        pool = [cells[(o, i)] for c, cells in cells_by_county.items() if c != county
                for (o, i), r in cells.items()
                if o == own and r['lv'] == 6 and r['disc'] == '' and
                (i == code if d == 6 else (SECTOR.get(i[:2], i[:2]) == code if d == 2 else i.startswith(code)))]
        same_size = [r for r in pool if r['estabs'] == n_rec]
        if same_size:
            r = rng.choice(same_size); return {v: r[v] for v in VARS}
        if pool and first_pool is None: first_pool = [r for r in pool if r['estabs'] > 0] or pool
    if first_pool:
        r = rng.choice(first_pool)
        scale = n_rec / r['estabs'] if r['estabs'] > 0 else 1.0
        return {v: r[v] * scale for v in VARS}
    return {v: 0.0 for v in VARS}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('root'); ap.add_argument('year', type=int); ap.add_argument('qtr', type=int); ap.add_argument('out')
    ap.add_argument('--draws', type=int, default=20); ap.add_argument('--alpha', type=float, default=20)
    ap.add_argument('--seed', type=int, default=1); ap.add_argument('--validate', type=float, default=0)
    ap.add_argument('--persistent', action='store_true', help='leave the quarter out of the random-stream key')
    ap.add_argument('--no-rake', action='store_true', help='ablation arm A: no raking to state totals')
    ap.add_argument('--point', action='store_true', help='ablation arm B: proportional split, no random draws')
    ap.add_argument('--hotdeck-sized', action='store_true', help='validation: also score the size-matched hot-deck (H16)')
    a = ap.parse_args()
    cond.start_run(*[v for v in vars(a).values() if isinstance(v, str) and os.path.exists(v)])  # F11
    rng = cond.rng_for(a.seed, 'hide', a.year, a.qtr)   # only selects the cells hidden for validation
    qdir = cond.require(f'{a.root}/{a.year}q{a.qtr}')
    counties = sorted(os.path.basename(f)[:-4] for f in cond.area_files(qdir))   # F1: area files only
    cells_by_county = {c: load_area(f'{qdir}/{c}.csv', COUNTY_LVL) for c in counties}
    priors = Priors(a.root, a.year, a.qtr, counties)

    hidden = {}
    if a.validate:
        for c, cells in cells_by_county.items():
            sib = collections.defaultdict(list)
            for k, r in cells.items():
                if r['lv'] == 6: sib[(k[0], k[1][:5])].append(k)
            for g, ks in sib.items():
                if not any(cells[k]['disc'] == 'N' for k in ks): continue
                for k in ks:
                    r = cells[k]
                    if r['disc'] == '' and 1 <= r['estabs'] <= 3 and rng.random() < a.validate:
                        hidden[(c, k)] = {v: r[v] for v in VARS}; r['disc'] = 'N'

    log = collections.Counter()
    draws, hd, hds = [], [], []
    for m in range(a.draws):
        key = (a.seed, m + 1) if a.persistent else (a.seed, m + 1, a.year, a.qtr)
        d = draw_once(key, cells_by_county, priors, a.alpha, log, rake=not a.no_rake, point=a.point)
        draws.append(d)
        if hidden:
            hd.append({ck: hotdeck(key, ck[0], ck[1], cells_by_county[ck[0]][ck[1]], cells_by_county) for ck in hidden})
            if a.hotdeck_sized:
                hds.append({ck: hotdeck_sized(key, ck[0], ck[1], cells_by_county[ck[0]][ck[1]], cells_by_county) for ck in hidden})

    os.makedirs(a.out, exist_ok=True)
    tag = f'{a.year}q{a.qtr}' + ('_validation' if hidden else '')
    with gzip.open(f'{a.out}/qcew_imputed_{tag}_M{a.draws}.csv.gz', 'wt', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['draw', 'area_fips', 'own_code', 'industry_code', 'level', 'qtrly_estabs'] + list(VARS))
        for m, d in enumerate(draws, 1):
            for c in counties:
                for k, x in sorted(d[c].items()):
                    r = cells_by_county[c][k]
                    w.writerow([m, c, k[0], k[1], r['lv'], r['estabs']] + [round(x[v]) for v in VARS])
    print('log:', dict(log))
    print('suppressed cells imputed per draw:', sum(len(d[c]) for c in counties for d in draws[:1]))

    if hidden:
        rows = []
        for ck, truth in hidden.items():
            c, k = ck
            row = {'area_fips': c, 'own_code': k[0], 'industry_code': k[1], 'estabs': cells_by_county[c][k]['estabs']}
            methods = [('alloc', [d[c].get(k) for d in draws]), ('hotdeck', [h[ck] for h in hd])]
            if hds: methods.append(('hotdecksz', [h[ck] for h in hds]))
            for meth, src in methods:
                for v in ('month3_emplvl', 'total_qtrly_wages'):
                    xs = sorted(s[v] for s in src if s)
                    if not xs: continue
                    row[f'{meth}_{v}_mean'] = st.mean(xs)
                    row[f'{meth}_{v}_p10'] = xs[int(0.1 * len(xs))]; row[f'{meth}_{v}_p90'] = xs[min(len(xs) - 1, int(0.9 * len(xs)))]
            for v in ('month3_emplvl', 'total_qtrly_wages'): row[f'true_{v}'] = truth[v]
            rows.append(row)
        with open(f'{a.out}/validation_{a.year}q{a.qtr}_alpha{a.alpha:g}.csv', 'w', newline='') as fh:
            cols = sorted({c for r in rows for c in r}, key=lambda s: (not s.startswith(('area', 'own', 'ind', 'est', 'true')), s))
            wr = csv.DictWriter(fh, cols); wr.writeheader(); wr.writerows(rows)
        print(f'\nvalidation: {len(rows)} hidden cells (published, 1-3 establishments, with suppressed siblings)')
        for v in ('month3_emplvl', 'total_qtrly_wages'):
            for meth in ('alloc', 'hotdeck') + (('hotdecksz',) if hds else ()):
                ok = [r for r in rows if f'{meth}_{v}_mean' in r]
                err = [r[f'{meth}_{v}_mean'] - r[f'true_{v}'] for r in ok]
                ae = sorted(abs(e) for e in err)
                cover = st.mean(r[f'{meth}_{v}_p10'] <= r[f'true_{v}'] <= r[f'{meth}_{v}_p90'] for r in ok)
                print(f'  {v:18s} {meth:8s} n={len(ok)} median|err|={st.median(ae):,.1f} mean|err|={st.mean(ae):,.1f} '
                      f'mean bias={st.mean(err):+,.1f} 80%-interval coverage={cover:.2f}  (median truth {st.median(r[f"true_{v}"] for r in ok):,.0f})')


if __name__ == '__main__':
    main()
