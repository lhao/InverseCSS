"""Step 4: stock-flow consistency by worker type (post-processing of worker_groups.py output).

The cross-sectional worker groups match QWI's compositions quarter by quarter, but a type's stock need not equal last
quarter's stock + hires - separations. Here flows are primary and stocks are derived, as in QWI's own accounting:
  end stock(e, t) = aged start stock(e, t) + hires(e, t) - separations(e, t)        (exact, every establishment and type)
Per establishment-quarter (establishment totals from the panel: month-3 start and end employment, hires, separations):
  0. start stock = last quarter's end stock (continuers, reclassified); merges add the merged-away unit's stock;
     split successors take their share of the parent's stock; entrants start at zero; rescaled to the panel's start.
  aging: each quarter a share 0.25 / (years in group) of each age group moves up (A01 14-18 ... A07 55-64; A08 stays);
     moving from A03 (22-24) to A04 (25-34) the 'not available' education is replaced by the establishment's (else the
     cell's) education mix of 25-34s of the same sex and race-ethnicity.
  1. short spells: K = max(separations - start, 0) workers are hired and separated within the quarter; by type in
     proportion to the cross-sectional hires composition.
  2. separations of the start stock (separations - K): by type in proportion to start stock x separation propensity, the
     propensity = cell x size separations of the type / start stock of the type (QWI-constrained cross-section); capped at
     the type's stock, the excess spread over the remaining capacity.
  3. hires (hires - K): per cell x size, raked so the cell's hires composition equals the QWI-constrained cross-section
     (flows have priority); within the cell each establishment's seed is a share --need-weight towards its target end
     composition (cross-sectional allocation scaled to end employment, minus stayers), the rest its cross-sectional hires
     composition. Stocks are derived; their drift from the cross-section is reported.
  Exits separate their whole stock; entrants hire their end stock.
Outputs (<out_dir>): workers_sf_<tag>.csv.gz (as workers_<tag> plus stock_start, stock_end), stockflow_report_<tag>.txt
(identity check, nonnegativity, cell-level gaps to the QWI-constrained compositions).
Usage: python3 worker_stockflow.py <workers_dir> <panel_dir> <out_dir> [--tag slices]
"""
import argparse, collections, csv, gzip, os, statistics as st, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond
import priors

AGES = ['A01', 'A02', 'A03', 'A04', 'A05', 'A06', 'A07', 'A08']
# worker rows held as compact tuples (memory, 2026-10-25): codes interned, numbers converted once
Row = collections.namedtuple('Row', 'version year quarter county naics6 est_id firm_size_class sex agegrp education re emp hires separations')
PFIELDS = ('event', 'pred_id', 'emp_prev_m3', 'emp_m3', 'hires', 'separations', 'emp_avg')
WIDTH = {'A01': 5, 'A02': 3, 'A03': 3, 'A04': 10, 'A05': 10, 'A06': 10, 'A07': 10}
EDU4 = ['E1', 'E2', 'E3', 'E4']


def age_types(S, edu_mix):
    """One quarter of aging: S {type: count} -> aged {type: count}; edu_mix(sex, re) -> {E1..E4: share} for new 25-year-olds."""
    out = collections.Counter()
    for (s, a, e, r), v in S.items():
        if v <= 0: continue
        if a == 'A08': out[(s, a, e, r)] += v; continue
        up = v * 0.25 / WIDTH[a]; out[(s, a, e, r)] += v - up
        na = AGES[AGES.index(a) + 1]
        if a == 'A03':
            mix = edu_mix(s, r)
            for e2, sh in mix.items(): out[(s, na, e2, r)] += up * sh
        else: out[(s, na, e, r)] += up
    return out


def capped_split(total, weights, caps):
    """Split total proportionally to weights, each capped; excess spread over remaining capacity. Returns (alloc, left)."""
    x = {k: 0.0 for k in weights}; rem = total; free = [k for k in weights if weights[k] > 0 and caps.get(k, 0) > 0]
    for _ in range(50):
        if rem <= 1e-12 or not free: break
        tw = sum(weights[k] for k in free); add = {k: rem * weights[k] / tw for k in free}; rem = 0.0; nxt = []
        for k in free:
            room = caps[k] - x[k]
            if add[k] >= room: x[k] = caps[k]; rem += add[k] - room
            else: x[k] += add[k]; nxt.append(k)
        free = nxt
    if rem > 1e-9:   # weights exhausted: use remaining capacity of any type
        cap_left = {k: caps[k] - x.get(k, 0.0) for k in caps if caps[k] - x.get(k, 0.0) > 1e-12}
        tc = sum(cap_left.values())
        if tc > 0:
            take = min(rem, tc)
            for k, c in cap_left.items(): x[k] = x.get(k, 0.0) + take * c / tc
            rem -= take
    return x, rem


def main():
    ap = argparse.ArgumentParser()
    for x in ('workers', 'panel', 'out'): ap.add_argument(x)
    ap.add_argument('--tag', default='slices')
    ap.add_argument('--stock-lambda', type=float, default=1.0, help='weight of end-stock deviations relative to hires deviations')
    ap.add_argument('--need-weight', type=float, default=0.5, help='share of hires steered towards the target end composition')
    a = ap.parse_args()
    cond.start_run(a.workers, a.panel)
    P = {}; merged = collections.defaultdict(list)
    with cond.open_text(cond.require(os.path.join(a.panel, f'panel_{a.tag}.csv.gz'))) as fh:
        for r in csv.DictReader(fh):
            P[(r['version'], r['year'], r['quarter'], r['est_id'])] = {f: r[f] for f in PFIELDS}
    with cond.open_text(cond.require(os.path.join(a.panel, f'exits_{a.tag}.csv.gz'))) as fh:
        for r in csv.DictReader(fh):
            if r['event'] == 'merged_away': merged[(r['version'], r['year'], r['quarter'], r['successor_id'])].append(r['est_id'])
    os.makedirs(a.out, exist_ok=True)
    tmp = os.path.join(a.out, f'workers_sf_{a.tag}.csv.gz.part')
    log = collections.Counter(); gaps = collections.defaultdict(list); ident = []; neg = 0
    stock = {}                       # (version, est_id) -> {type: end stock}
    exits_est = collections.defaultdict(set)
    with cond.open_text(cond.require(os.path.join(a.panel, f'exits_{a.tag}.csv.gz'))) as fh:
        for r in csv.DictReader(fh):
            if r['event'] == 'exit': exits_est[(r['version'], r['year'], r['quarter'])].add(r['est_id'])
    def flush(q_rows, fo_w):
        nonlocal neg
        if not q_rows: return
        m, y, q = q_rows[0].version, q_rows[0].year, q_rows[0].quarter
        by_est = collections.defaultdict(list)
        for r in q_rows: by_est[r.est_id].append(r)
        # cell x size cross-sectional compositions (QWI-constrained), and start stocks by type for the propensities
        cell = collections.defaultdict(lambda: {'emp': collections.Counter(), 'h': collections.Counter(), 'x': collections.Counter(), 's0': collections.Counter()})
        info = {}
        for est, rows in by_est.items():
            r0 = rows[0]; key = (r0.county, r0.naics6, r0.firm_size_class); info[est] = key
            for r in rows:
                t = (r.sex, r.agegrp, r.education, r.re)
                cell[key]['emp'][t] += r.emp; cell[key]['h'][t] += r.hires; cell[key]['x'][t] += r.separations
        flowc = {'h': collections.defaultdict(collections.Counter), 'x': collections.defaultdict(collections.Counter)}
        pending = {}
        # start stocks
        start = {}
        for est, rows in by_est.items():
            pr = P.get((m, y, q, est))
            if pr is None: continue    # exit rows (in workers file with separations only); handled below
            ev = pr['event']; S = collections.Counter(stock.get((m, est), {}))
            if ev == 'merge_survivor':
                for b in merged.get((m, y, q, est), []): S.update(stock.get((m, b), {}))
            if ev == 'split_successor' and pr['pred_id']:
                par = stock.get((m, pr['pred_id']), {}); pe = P.get((m, y, q, pr['pred_id']))
                tot_par = sum(par.values())
                share = float(pr['emp_prev_m3'] or 0) / (float(pr['emp_prev_m3'] or 0) + float(pe['emp_prev_m3'] or 0)) if pe and (float(pr['emp_prev_m3'] or 0) + float(pe['emp_prev_m3'] or 0)) > 0 else 0.5
                S = collections.Counter({t: v * share for t, v in par.items()})
            start[est] = S
        # parents of split successors give up the successors' share
        for est, S in list(start.items()):
            pr = P[(m, y, q, est)]
            if pr['event'] == 'split_successor' and pr['pred_id'] in start:
                pe = P[(m, y, q, pr['pred_id'])]
                tot = float(pr['emp_prev_m3'] or 0) + float(pe['emp_prev_m3'] or 0)
                if tot > 0: start[pr['pred_id']] = collections.Counter({t: v * float(pe['emp_prev_m3'] or 0) / tot for t, v in start[pr['pred_id']].items()})
        for est, S in start.items():
            key = info[est]
            for t, v in S.items(): cell[key]['s0'][t] += v
        for est, rows in by_est.items():
            pr = P.get((m, y, q, est)); key = info[est]
            cx = {(r.sex, r.agegrp, r.education, r.re): r for r in rows}
            if pr is None or est in exits_est[(m, y, q)] and pr is None:
                # exit: separates its whole (aged) start stock
                S = collections.Counter(stock.pop((m, est), {}))
                for t, v in S.items():
                    fo_w.writerow([m, y, q, key[0], key[1], est, key[2], *t, 0, 0, round(v, 3), round(v, 3), 0]); ident.append(0.0)
                log['exits'] += 1
                continue
            e_prev = float(pr['emp_prev_m3'] or 0); e_end = float(pr['emp_m3']); H = float(pr['hires'] or 0); X = float(pr['separations'] or 0)
            if pr['event'] != 'initial':   # the panel file rounds to 2 decimals: take hires from the identity (gap logged)
                H_id = e_end - e_prev + X
                gaps['hires_rounding'].append(abs(H_id - H)); H = max(H_id, 0.0)
            if pr['event'] == 'initial':
                S_end = collections.Counter({t: r.emp for t, r in cx.items()})
                se = sum(S_end.values())
                S_end = collections.Counter({t: v * e_end / se for t, v in S_end.items()}) if se > 0 else S_end
                stock[(m, est)] = S_end
                for t, v in S_end.items(): fo_w.writerow([m, y, q, key[0], key[1], est, key[2], *t, round(cx[t].emp, 3), 0, 0, '', round(v, 3)])
                log['initial'] += 1
                continue
            S0 = start.get(est, collections.Counter()); s0 = sum(S0.values())
            if s0 > 0 and e_prev > 0: S0 = collections.Counter({t: v * e_prev / s0 for t, v in S0.items()})
            elif e_prev > 0:
                comp = {t: r.emp for t, r in cx.items()}; tc = sum(comp.values())
                if tc > 0: log['start_stock_from_cross_section'] += 1
                else:   # no type rows for this establishment this quarter either: take the cell x size composition (2026-10-25)
                    comp = dict(cell[key]['emp']); tc = sum(comp.values()); log['start_stock_from_cell'] += 1
                S0 = collections.Counter({t: e_prev * v / tc for t, v in comp.items()}) if tc > 0 else S0
            else: S0 = collections.Counter()
            # aging, with new 25-year-olds taking the establishment's (else cell's) education mix of 25-34s
            def edu_mix(s, r_, S0=S0, key=key):
                for src in (S0, cell[key]['emp']):
                    w = {e: src.get((s, 'A04', e, r_), 0.0) for e in EDU4}; tw = sum(w.values())
                    if tw > 0: return {e: v / tw for e, v in w.items()}
                return {e: 0.25 for e in EDU4}
            Sa = age_types(S0, edu_mix)
            hcomp = {t: r.hires for t, r in cx.items() if r.hires > 0}
            th = sum(hcomp.values())
            if th <= 0:
                hcomp = {t: r.emp for t, r in cx.items() if r.emp > 0} or {t: 1.0 for t in Sa}; th = sum(hcomp.values())
            hsh = {t: v / th for t, v in hcomp.items()} if th > 0 else {}
            K = max(X - e_prev, 0.0)
            Kt = {t: K * v for t, v in hsh.items()}
            # separations of the start stock, by propensity
            Xs = X - K
            prop = {}
            for t, v in Sa.items():
                s_cell = cell[key]['s0'].get(t, 0.0)
                prop[t] = (cell[key]['x'].get(t, 0.0) / s_cell) if s_cell > 0 else 1.0
            mean_p = sum(prop[t] * v for t, v in Sa.items()) / sum(Sa.values()) if Sa else 1.0
            wts = {t: v * (prop[t] if prop[t] > 0 else mean_p * 0.1) for t, v in Sa.items()}
            Xt, left = capped_split(Xs, wts, dict(Sa))
            if left > 1e-6:
                log['separations_exceed_stock'] += 1
                log['separations_exceed_stock_no_start_stock' if sum(Sa.values()) <= 1e-9 else 'separations_exceed_stock_with_start_stock'] += 1
                gaps['separations_unplaced'].append(left)
            R = {t: max(Sa.get(t, 0.0) - Xt.get(t, 0.0), 0.0) for t in sorted(set(Sa) | set(Xt))}
            # hires: half towards target end composition, half by cross-sectional hires composition
            Hs = H - K
            tgt = {t: r.emp for t, r in cx.items()}; tt = sum(tgt.values())
            need = {t: max(e_end * v / tt - R.get(t, 0.0), 0.0) for t, v in tgt.items()} if tt > 0 else {}
            tn = sum(need.values())
            Hseed = collections.Counter()
            for t, v in hsh.items(): Hseed[t] += (1 - a.need_weight) * v if tn > 0 else v
            if tn > 0:
                for t, v in need.items(): Hseed[t] += a.need_weight * v / tn
            pending[est] = (key, pr, Sa, K, Kt, Xt, R, Hs, Hseed, e_end)
            log['continuers_entrants'] += 1
        # pass 2: hires per cell x size raked so the cell's hires composition equals the QWI-constrained cross-section
        # (flows have priority); within the cell the seed steers each establishment towards its target end composition
        by_key = collections.defaultdict(list)
        for est, v in pending.items(): by_key[v[0]].append(est)
        for key, ests in by_key.items():
            ch = cell[key]['h']; tch = sum(ch.values())
            totH = sum(pending[e][7] + pending[e][3] for e in ests); totHs = sum(pending[e][7] for e in ests)
            seedH = {(e, t): pending[e][7] * v for e in ests for t, v in pending[e][8].items() if v > 0}
            if tch > 0 and totHs > 0:
                Kc = collections.Counter()
                for e in ests: Kc.update(pending[e][4])
                Hstar = {t: max(totH * v / tch - Kc.get(t, 0.0), 0.0) for t, v in ch.items()}
                # balance hires and stocks: each type's inconsistency D = S* - R - H* (target end stock minus stayers
                # minus QWI-composition hires) is split between hires and end stock in proportion to H* and S* / lambda,
                # which minimises the summed chi-square deviation of both from their targets
                ce = cell[key]['emp']; tce = sum(ce.values()); totE = sum(pending[e][9] for e in ests)
                Rc = collections.Counter()
                for e in ests: Rc.update(pending[e][6])
                T = {}
                for t in sorted(set(Hstar) | set(ce)):
                    Ss = totE * ce.get(t, 0.0) / tce if tce > 0 else 0.0; Hs_ = Hstar.get(t, 0.0)
                    D = Ss - Rc.get(t, 0.0) - Hs_
                    den = Hs_ + Ss / a.stock_lambda
                    T[t] = max(Hs_ + (D * Hs_ / den if den > 0 else 0.0), 0.0)
                for t in sorted({t for (_, t) in seedH}): T.setdefault(t, 0.0)
                sT = sum(T.values()); T = {t: v * totHs / sT for t, v in T.items()} if sT > 0 else T
                # types with a target but no seed get a small seed in every establishment that hires
                for t, v in T.items():
                    if v > 0 and not any((e, t) in seedH for e in ests):
                        for e in ests:
                            if pending[e][7] > 0: seedH[(e, t)] = 1e-6
                Hx = priors.ipf_np(seedH, {e: pending[e][7] for e in ests if pending[e][7] > 0}, {t: v for t, v in T.items() if v > 0})
                rs = collections.Counter()
                for (e, t), v in Hx.items(): rs[e] += v
                Hx = {(e, t): v * pending[e][7] / rs[e] for (e, t), v in Hx.items() if rs[e] > 0}   # establishment hires exact
                # an establishment whose own hire composition lies only in types with a zero cell target gets nothing from
                # the raking: keep its own composition so its hires (and end stock) stay exact (found 2026-10-25, Montgomery)
                for e in ests:
                    if pending[e][7] > 0 and rs[e] <= 0:
                        hs = {t: v for t, v in pending[e][8].items() if v > 0}; th_ = sum(hs.values())
                        if th_ > 0:
                            for t, v in hs.items(): Hx[(e, t)] = pending[e][7] * v / th_
                            log['hires_kept_own_composition'] += 1
            else:
                Hx = seedH
            for est in ests:
                key_, pr, Sa, K, Kt, Xt, R, Hs, Hseed, e_end = pending[est]
                Ht = collections.Counter({t: v for (e, t), v in Hx.items() if e == est})
                S_end = collections.Counter()
                for t in sorted(set(R) | set(Ht)):
                    v_ = R.get(t, 0.0) + Ht.get(t, 0.0)
                    if v_ < -1e-6: neg += 1
                    S_end[t] = max(v_, 0.0)                 # float noise below 1e-6 clamped to zero
                tot_h = sum(Ht.values()) + K; tot_x = sum(Xt.values()) + K
                ident.append(abs(sum(S_end.values()) - sum(Sa.values()) - (tot_h - tot_x)))
                gaps['end_vs_panel'].append(abs(sum(S_end.values()) - e_end))
                stock[(m, est)] = S_end
                for t in sorted(set(Ht) | set(Kt)): flowc['h'][key][t] += Ht.get(t, 0.0) + Kt.get(t, 0.0)
                for t in sorted(set(Xt) | set(Kt)): flowc['x'][key][t] += Xt.get(t, 0.0) + Kt.get(t, 0.0)
                emp_avg = float(pr['emp_avg']); mid = {t: (Sa.get(t, 0.0) + S_end.get(t, 0.0)) / 2 for t in sorted(set(Sa) | set(S_end))}; tm = sum(mid.values())
                for t in sorted(set(Sa) | set(S_end) | set(Ht) | set(Xt) | set(Kt)):
                    ev_ = emp_avg * mid.get(t, 0.0) / tm if tm > 0 else 0.0
                    h_ = Ht.get(t, 0.0) + Kt.get(t, 0.0); x_ = Xt.get(t, 0.0) + Kt.get(t, 0.0)
                    if max(ev_, h_, x_, S_end.get(t, 0.0), Sa.get(t, 0.0)) >= 0.005:
                        fo_w.writerow([m, y, q, key[0], key[1], est, key[2], *t, round(ev_, 3), round(h_, 3), round(x_, 3), round(Sa.get(t, 0.0), 3), round(S_end.get(t, 0.0), 3)])
        # composition gaps per cell x size (end stock vs cross-sectional employment composition; flows vs cross-section)
        endc = collections.defaultdict(collections.Counter); hc = collections.defaultdict(collections.Counter); xc = collections.defaultdict(collections.Counter)
        for est in by_est:
            if (m, est) in stock and P.get((m, y, q, est)):
                for t, v in stock[(m, est)].items(): endc[info[est]][t] += v
        for key, cl in cell.items():
            for lab, A, B in (('stock', endc[key], cl['emp']), ('hires', flowc['h'][key], cl['h']), ('separations', flowc['x'][key], cl['x'])):
                ta, tb = sum(A.values()), sum(B.values())
                if ta > 0 and tb > 0 and tb >= 20:
                    gaps[f'{lab}_tvd'].append(sum(abs(A.get(t, 0) / ta - B.get(t, 0) / tb) for t in sorted(set(A) | set(B))) / 2)
    with gzip.open(tmp, 'wt', newline='') as fo:
        w = csv.writer(fo)
        w.writerow(['version', 'year', 'quarter', 'county', 'naics6', 'est_id', 'firm_size_class', 'sex', 'agegrp', 'education', 're',
                    'emp', 'hires', 'separations', 'stock_start', 'stock_end'])
        cur, buf = None, []
        with cond.open_text(cond.require(os.path.join(a.workers, f'workers_{a.tag}.csv.gz'))) as fh:
            rd = csv.reader(fh); hdr = next(rd); ix = [hdr.index(f) for f in Row._fields]; I = sys.intern
            for x in rd:
                r = Row(*[I(x[i]) for i in ix[:11]], float(x[ix[11]]), float(x[ix[12]]), float(x[ix[13]]))
                k = (r.version, r.year, r.quarter)
                if k != cur:
                    flush(buf, w); cur, buf = k, []
                buf.append(r)
        flush(buf, w)
    os.replace(tmp, os.path.join(a.out, f'workers_sf_{a.tag}.csv.gz'))
    L = [f'Stock-flow consistency by worker type ({a.workers}); log {dict(log)}',
         f'identity |end - aged start - (hires - separations)| per establishment: max {max(ident):.2e}',
         f'end stock vs panel month-3 employment per establishment: max gap {max(gaps["end_vs_panel"]):.2e}; '
         f'establishment-quarters with gap > 0.02: {sum(1 for g in gaps["end_vs_panel"] if g > 0.02):,} of {len(gaps["end_vs_panel"]):,}; '
         f'separations that found no start stock: {sum(gaps["separations_unplaced"]):.2f} jobs in {len(gaps["separations_unplaced"])} establishment-quarters',
         f'negative type stocks (beyond float noise 1e-6): {neg}',
         f'panel rounding: |hires from identity - panel hires| max {max(gaps["hires_rounding"]):.3f}, mean {st.mean(gaps["hires_rounding"]):.4f}',
         'composition vs the QWI-constrained cross-section (cell x size groups >= 20, TVD): ' + '; '.join(
             f'{lab} median {st.median(gaps[lab + "_tvd"]):.3f}, 90th pct {sorted(gaps[lab + "_tvd"])[int(0.9 * len(gaps[lab + "_tvd"]))]:.3f}'
             for lab in ('stock', 'hires', 'separations') if gaps.get(lab + '_tvd'))]
    print('\n'.join(L)); open(os.path.join(a.out, f'stockflow_report_{a.tag}.txt'), 'w').write('\n'.join(L) + '\n')


if __name__ == '__main__':
    main()
