"""Convert QCEW quarters coded in NAICS 2017 (2021) to NAICS 2022 (DATA_CONDITIONS C1), per imputation version.

For each quarter and each version m:
  1. complete county x ownership x 6-digit table in NAICS 2017: published raw values (incl. true zeros)
     + imputed version m for suppressed cells (impute_qcew.py output);
  2. map 6-digit codes with the Census 2017->2022 concordance: one-to-one and merged codes are summed;
     a 2017 code that splits is divided by the shares of its 2022 targets in the first NAICS-2022 quarter
     (2022 Q1, version m, same county and ownership); fallback: statewide shares; fallback: equal shares;
  3. rebuild 5-, 4-, 3-digit and sector cells from the converted 6-digit cells; the statewide file (24000) is the
     sum over counties (counties + 24999 = state exactly, DATA_CONDITIONS D2);
  4. status per cell: 'published' if every contributing source cell was published and none was split;
     'converted' if a split contributed; otherwise 'imputed';
  5. published aggregates are kept: where a code's composition is unchanged by the concordance (its 6-digit codes
     map only among themselves) and BLS published that cell in the NAICS-2017 file, the published value and status
     are used (counties and the statewide file). Without this, a published state or 4-digit cell would be labelled
     'imputed' whenever any contributing county 6-digit cell was suppressed.
Values: qtrly_estabs (fractional after splits), month1-3 employment, total quarterly wages.
Ownership totals and area totals (levels 0-1) do not depend on industry codes and are copied from the raw files.

Output: <out_dir>/qcew_complete_<yyyy>q<q>_M<M>.csv.gz
  columns: draw, area_fips, own_code, industry_code, level, status, qtrly_estabs, month1_emplvl, month2_emplvl,
           month3_emplvl, total_qtrly_wages

Usage: python3 convert_qcew_naics.py <qcew_raw_root> <qcew_imputed_dir> <concordance.xlsx> <out_dir>
                                     [--quarters 2021q1,2021q2,2021q3,2021q4] [--ref 2022q1] [--draws 20]
"""
import argparse, collections, csv, gzip, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond

MEAS = ('qtrly_estabs', 'month1_emplvl', 'month2_emplvl', 'month3_emplvl', 'total_qtrly_wages')


def load_concordance(path):
    import openpyxl
    ws = openpyxl.load_workbook(cond.require(path), read_only=True).worksheets[0]
    m = collections.defaultdict(list)
    for r in ws.iter_rows(values_only=True):
        if r and isinstance(r[0], (int, float)) and isinstance(r[2], (int, float)):
            a, b = str(int(r[0])), str(int(r[2]))
            if b not in m[a]: m[a].append(b)
    return m


def complete_6d(qdir, imp_path, draws):
    """{m: {(area, own, code): (values, status)}} at 6-digit for counties; status 'published' or 'imputed'."""
    pub, lvl01 = {}, []
    for f in cond.area_files(qdir):
        with cond.open_text(f) as fh:
            for r in csv.DictReader(fh):
                lv = cond.COUNTY_AGGLVL.get(r['agglvl_code'])
                if lv is None: continue
                if lv <= 1: lvl01.append(r); continue
                if lv == 6 and cond.qcew_status(r) in ('published', 'true_zero'):
                    pub[(r['area_fips'], r['own_code'], r['industry_code'])] = tuple(cond.qcew_value(r, v) if v != 'qtrly_estabs' else float(r[v]) for v in MEAS)
    out = {m: {k: (v, 'published') for k, v in pub.items()} for m in range(1, draws + 1)}
    with cond.open_text(imp_path) as fh:
        for r in csv.DictReader(fh):
            m = int(r['draw'])
            if m <= draws and r['level'] == '6':
                out[m][(r['area_fips'], r['own_code'], r['industry_code'])] = (tuple(float(r[v]) for v in MEAS), 'imputed')
    return out, lvl01


def closed_codes(conc, codes2017):
    """Codes (levels 2-6, NAICS 2022) whose composition is unchanged by the concordance."""
    def up(code, lv): return cond.SECTOR.get(code[:2], code[:2]) if lv == 2 else code[:lv]
    src = collections.defaultdict(set)
    for a in codes2017:
        for b in conc.get(a, [a]): src[b].add(a)
    closed = set()
    for lv in (6, 5, 4, 3, 2):
        groups17, groups22 = collections.defaultdict(set), collections.defaultdict(set)
        for a in codes2017: groups17[up(a, lv)].add(a)
        for b in src: groups22[up(b, lv)].add(b)
        for x in set(groups17) | set(groups22):
            if all(up(b, lv) == x for a in groups17[x] for b in conc.get(a, [a])) and \
               all(up(a, lv) == x for b in groups22[x] for a in src[b]) and \
               (lv < 6 or (len(src.get(x, ())) == 1 and next(iter(src[x])) == x)):
                closed.add((x, lv))
    return closed


def published_raw(qdir):
    """Published (incl. true-zero) cells of the NAICS-2017 file at levels 2-6: {(area, own, code, level): values}."""
    out = {}
    for f in cond.area_files(qdir, include_state=True):
        lv_map = cond.STATE_AGGLVL if f.endswith('24000.csv') else cond.COUNTY_AGGLVL
        with cond.open_text(f) as fh:
            for r in csv.DictReader(fh):
                lv = lv_map.get(r['agglvl_code'])
                if lv and lv >= 2 and cond.qcew_status(r) in ('published', 'true_zero'):
                    out[(r['area_fips'], r['own_code'], r['industry_code'], lv)] = [cond.qcew_value(r, v) if v != 'qtrly_estabs' else float(r[v]) for v in MEAS]
    return out


def main():
    ap = argparse.ArgumentParser()
    for x in ('raw', 'imp', 'concordance', 'out'): ap.add_argument(x)
    ap.add_argument('--quarters', default='2021q1,2021q2,2021q3,2021q4'); ap.add_argument('--ref', default='2022q1')
    ap.add_argument('--draws', type=int, default=20)
    a = ap.parse_args()
    cond.start_run(*[v for v in vars(a).values() if isinstance(v, str) and os.path.exists(v)])  # F11
    conc = load_concordance(a.concordance)
    splits = {k: v for k, v in conc.items() if len(v) > 1}
    print(f'concordance: {len(conc)} 2017 codes, {len(splits)} split: ' + ', '.join(f'{k}->{"/".join(v)}' for k, v in sorted(splits.items())))
    os.makedirs(a.out, exist_ok=True)
    ry, rq = int(a.ref[:4]), int(a.ref[5])
    ref, _ = complete_6d(os.path.join(a.raw, a.ref), os.path.join(a.imp, f'qcew_imputed_{ry}q{rq}_M20.csv.gz'), a.draws)
    log = collections.Counter()
    for qs in a.quarters.split(','):
        y, q = int(qs[:4]), int(qs[5])
        tab, lvl01 = complete_6d(cond.require(os.path.join(a.raw, qs)), os.path.join(a.imp, f'qcew_imputed_{y}q{q}_M20.csv.gz'), a.draws)
        raw_pub = published_raw(os.path.join(a.raw, qs))
        closed = closed_codes(conc, {k[2] for k in tab[1]} | {k[2] for k in raw_pub if k[3] == 6})
        path = os.path.join(a.out, f'qcew_complete_{y}q{q}_M{a.draws}.csv.gz')
        with gzip.open(path, 'wt', newline='') as fh:
            w = csv.writer(fh)
            w.writerow(['draw', 'area_fips', 'own_code', 'industry_code', 'level', 'status'] + list(MEAS))
            for m in range(1, a.draws + 1):
                # reference shares for split targets (2022 Q1, version m): county, then state
                rshare = collections.defaultdict(float); rstate = collections.defaultdict(float)
                for (ar, own, code), (vals, st_) in ref[m].items():
                    rshare[(ar, own, code)] += vals[1]; rstate[(own, code)] += vals[1]
                conv = collections.defaultdict(lambda: [0.0] * len(MEAS)); status = {}
                def mark(key, s):
                    order = {'published': 0, 'imputed': 1, 'converted': 2}
                    status[key] = s if key not in status or order[s] > order[status[key]] else status[key]
                for (ar, own, code), (vals, st_) in tab[m].items():
                    targets = conc.get(code)
                    if not targets:
                        targets = [code]
                        if m == 1: log[f'{qs}_code_not_in_concordance'] += 1
                    if len(targets) == 1:
                        t = targets[0]
                        conv[(ar, own, t)] = [x + v for x, v in zip(conv[(ar, own, t)], vals)]; mark((ar, own, t), st_)
                        continue
                    wts = [rshare.get((ar, own, t), 0.0) for t in targets]
                    if sum(wts) <= 0: wts = [rstate.get((own, t), 0.0) for t in targets]; log['split_state_shares'] += m == 1
                    if sum(wts) <= 0: wts = [1.0] * len(targets); log['split_equal_shares'] += m == 1
                    tw = sum(wts)
                    for t, wt in zip(targets, wts):
                        conv[(ar, own, t)] = [x + v * wt / tw for x, v in zip(conv[(ar, own, t)], vals)]; mark((ar, own, t), 'converted')
                    if m == 1: log['cells_split'] += 1
                # aggregate up: 6 -> 5 -> 4 -> 3 -> sector, counties; then state = sum of areas
                cells = {}
                for (ar, own, code), vals in conv.items():
                    for lv in (6, 5, 4, 3, 2):
                        c = code if lv == 6 else (cond.SECTOR.get(code[:2], code[:2]) if lv == 2 else code[:lv])
                        k = (ar, own, c, lv)
                        old = cells.get(k)
                        cells[k] = ([x + v for x, v in zip(old[0], vals)] if old else list(vals), max(old[1], status[(ar, own, code)], key=['published', 'imputed', 'converted'].index) if old else status[(ar, own, code)])
                state = {}
                for (ar, own, c, lv), (vals, s) in cells.items():
                    k = ('24000', own, c, lv); old = state.get(k)
                    state[k] = ([x + v for x, v in zip(old[0], vals)] if old else list(vals), max(old[1], s, key=['published', 'imputed', 'converted'].index) if old else s)
                for r in lvl01:
                    w.writerow([m, r['area_fips'], r['own_code'], r['industry_code'], cond.COUNTY_AGGLVL[r['agglvl_code']], cond.qcew_status(r)]
                               + [cond.out_value(cond.qcew_value(r, v) if v != 'qtrly_estabs' else float(r[v])) for v in MEAS])
                for (ar, own, c, lv), (vals, s) in sorted(cells.items()) + sorted(state.items()):
                    rp = raw_pub.get((ar, own, c, lv))
                    if rp is not None and (c, lv) in closed:      # step 5: keep BLS-published aggregates
                        vals, s = rp, 'published'
                        if m == 1: log[f'{qs}_published_kept'] += 1
                    w.writerow([m, ar, own, c, lv, s] + [cond.out_value(x) for x in vals])
        print(f'{qs}: wrote {path}')
    print('log:', dict(log))


if __name__ == '__main__':
    main()
