"""Step 3e: held-out validation of the synthetic panel against tables that neither version uses (design section 4).

Held-out table: QWI county x NAICS sector by firm size and by firm age (qwi_md_sa_fs_gc_ns / fa_gc_ns). Both versions
are built from QWI state x 6-digit firm-class tables and county x 6-digit totals; the county x sector firm-class
breakdown is never used, so it tests whether the panels' firm structure inside a county is right.

Compared, per county x sector x quarter (2021 Q2 - 2024 Q4), panel vs QWI:
  shares     employment shares by firm class (TVD; employment = (start + end) / 2, QWI (Emp + EmpEnd) / 2)
  flows      job gain, job loss and churn rates by firm class (per job; mean absolute gap in percentage points)
Suppressed QWI classes are left out (at least 4 of 5 published; the panel is renormalised to the same classes).
Comparators: each panel (all versions; the spread across versions is reported) and a STATE BASELINE: the QWI state
x 6-digit firm-class shares and rates of the slice's industries, weighted by the slice's employment, i.e. "the county
looks like the state", which is all either version knows. Lower is better; a panel adds value where it beats the
baseline.
Caveat: a slice covers only part of its sector (coverage reported); the remainder of the sector is unknown to both
panels and the baseline alike, so it shifts all three comparisons equally.

Outputs (<out_dir>): heldout_<tag>.csv (one row per panel x dimension x county x sector x quarter x class),
heldout_report_<tag>.txt
Usage: python3 validate_heldout.py <qwi_dir> <out_dir> --panel random=<dir> --panel systematic=<dir> [--only ...] [--tag slices]
"""
import argparse, collections, csv, os, statistics as st, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond
import priors

CLASSES = ['1', '2', '3', '4', '5']
SIZE_LABEL = {'1': '0-19', '2': '20-49', '3': '50-249', '4': '250-499', '5': '500+'}
AGE_LABEL = {'1': '0-1', '2': '2-3', '3': '4-5', '4': '6-10', '5': '11+'}


def qwi_county_sector(path, dim, keys):
    """{(county, sector, y, q): {class: {emp, gain, loss, churn}}} from QWI county x sector by firm class (published only)."""
    out = collections.defaultdict(dict)
    with cond.open_text(cond.require(path)) as fh:
        for r in csv.DictReader(fh):
            if r['geo_level'] != 'C' or r['ownercode'] != 'A05' or r['sex'] != '0' or r['agegrp'] != 'A00' or r[dim] == '0': continue
            k = (r['geography'], r['industry'], int(r['year']), int(r['quarter']))
            if k[:2] not in keys: continue
            v = {x: cond.qwi_value(r, x) for x in ('Emp', 'EmpEnd', 'FrmJbGn', 'FrmJbLs', 'HirA')}
            if None in v.values(): continue
            out[k][r[dim]] = {'emp': (v['Emp'] + v['EmpEnd']) / 2, 'gain': v['FrmJbGn'], 'loss': v['FrmJbLs'], 'churn': max(v['HirA'] - v['FrmJbGn'], 0)}
    return out


def qwi_state6(path, dim, prefixes):
    """{(naics6, y, q): {class: {emp, gain, loss, churn}}} from QWI state x 6-digit by firm class."""
    out = collections.defaultdict(dict)
    with cond.open_text(cond.require(path)) as fh:
        for r in csv.DictReader(fh):
            if r['geo_level'] != 'S' or r['ind_level'] != '6' or r['ownercode'] != 'A05' or r['sex'] != '0' or r['agegrp'] != 'A00' or r[dim] == '0': continue
            if not any(r['industry'].startswith(p) for p in prefixes): continue
            v = {x: cond.qwi_value(r, x) for x in ('Emp', 'EmpEnd', 'FrmJbGn', 'FrmJbLs', 'HirA')}
            if None in v.values(): continue
            out[(r['industry'], int(r['year']), int(r['quarter']))][r[dim]] = {'emp': (v['Emp'] + v['EmpEnd']) / 2, 'gain': v['FrmJbGn'], 'loss': v['FrmJbLs'], 'churn': max(v['HirA'] - v['FrmJbGn'], 0)}
    return out


def panel_agg(pdir, tag):
    """{(version, county, sector, y, q): {'size': {class: {...}}, 'age': {...}}, 'n6': {naics6: emp}}"""
    out = collections.defaultdict(lambda: {'size': collections.defaultdict(collections.Counter), 'age': collections.defaultdict(collections.Counter), 'n6': collections.Counter()})
    with cond.open_text(cond.require(os.path.join(pdir, f'panel_{tag}.csv.gz'))) as fh:
        for r in csv.DictReader(fh):
            if r['event'] == 'initial': continue
            k = (int(r['version']), r['county'], priors.bds_sector(r['naics6']), int(r['year']), int(r['quarter']))
            e = (float(r['emp_prev_m3'] or 0) + float(r['emp_m3'])) / 2
            for dim, col in (('size', 'firm_size_class'), ('age', 'firm_age_class')):
                c = out[k][dim][r[col]]
                c['emp'] += e; c['gain'] += float(r['job_gain']); c['loss'] += float(r['job_loss']); c['churn'] += float(r['churn'])
            out[k]['n6'][r['naics6']] += e
    with cond.open_text(cond.require(os.path.join(pdir, f'exits_{tag}.csv.gz'))) as fh:
        for r in csv.DictReader(fh):
            if r['event'] != 'exit': continue
            k = (int(r['version']), r['county'], priors.bds_sector(r['naics6']), int(r['year']), int(r['quarter']))
            x = float(r['emp_prev_m3'])
            for dim, col in (('size', 'firm_size_class'), ('age', 'firm_age_class')):
                c = out[k][dim][r[col]]; c['loss'] += x; c['emp'] += x / 2
    return out


def scores(pred, obs):
    """TVD of employment shares; mean |gap| in rates (percentage points), employment-weighted by observed shares."""
    cls = [c for c in CLASSES if c in obs]          # published classes only; panel renormalised to the same classes
    tp = sum(pred[c]['emp'] for c in cls if c in pred); to = sum(obs[c]['emp'] for c in cls)
    if tp <= 0 or to <= 0: return None
    tvd = sum(abs(pred.get(c, {}).get('emp', 0) / tp - obs[c]['emp'] / to) for c in cls) / 2
    res = {'tvd': tvd}
    for f in ('gain', 'loss', 'churn'):
        g = []; w = []
        for c in CLASSES:
            if c in obs and obs[c]['emp'] > 0 and pred.get(c, {}).get('emp', 0) > 0:
                g.append(abs(100 * pred[c][f] / pred[c]['emp'] - 100 * obs[c][f] / obs[c]['emp'])); w.append(obs[c]['emp'])
        res[f] = sum(a * b for a, b in zip(g, w)) / sum(w) if w else None
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('qwi'); ap.add_argument('out')
    ap.add_argument('--panel', action='append', required=True, help='label=directory')
    ap.add_argument('--only', default='24047:7211,24047:7225,24031:5417'); ap.add_argument('--tag', default='slices')
    a = ap.parse_args()
    cond.start_run(a.qwi)
    only = [tuple(x.split(':')) for x in a.only.split(',') if x]
    keys = {(c, priors.bds_sector(p)) for c, p in only}
    prefixes = [p for _, p in only]
    files = {'size': ('qwi_md_sa_fs_gc_ns_op_u_2021-2024.csv.gz', 'qwi_md_sa_fs_gs_n6_op_u_2021-2024.csv.gz', 'firmsize'),
             'age': ('qwi_md_sa_fa_gc_ns_op_u_2021-2024.csv.gz', 'qwi_md_sa_fa_gs_n6_op_u_2021-2024.csv.gz', 'firmage')}
    obs = {d: qwi_county_sector(os.path.join(a.qwi, f[0]), f[2], keys) for d, f in files.items()}
    st6 = {d: qwi_state6(os.path.join(a.qwi, f[1]), f[2], prefixes) for d, f in files.items()}
    panels = {lab: panel_agg(p, a.tag) for lab, p in (x.split('=', 1) for x in a.panel)}
    rows = []; S = collections.defaultdict(list); cover = collections.defaultdict(list); detail = collections.defaultdict(list)
    for lab, P in panels.items():
        for (m, c, sec, y, q), agg in P.items():
            for dim in ('size', 'age'):
                o = obs[dim].get((c, sec, y, q))
                if not o or len(o) < 4: continue
                sc = scores(agg[dim], o)
                if sc: S[(lab, dim, c, sec)].append(sc)
                if lab == list(panels)[0] and m == 1:
                    cover[(c, sec)].append(sum(v['emp'] for v in agg[dim].values()) / sum(v['emp'] for v in o.values()))
                    # state baseline: slice industries' state class shares and rates, weighted by slice employment
                    base = collections.defaultdict(collections.Counter)
                    for n6, e in agg['n6'].items():
                        s6 = st6[dim].get((n6, y, q))
                        if not s6: continue
                        tot = sum(v['emp'] for v in s6.values())
                        for cl, v in s6.items():
                            if tot > 0 and v['emp'] > 0:
                                ec = e * v['emp'] / tot; base[cl]['emp'] += ec
                                for f in ('gain', 'loss', 'churn'): base[cl][f] += ec * v[f] / v['emp']
                    sb = scores(base, o)
                    if sb: S[('state baseline', dim, c, sec)].append(sb)
                for cl in CLASSES:
                    pv = agg[dim].get(cl, {}); ov = o.get(cl, {})
                    rows.append([lab, dim, m, c, sec, y, q, cl, round(pv.get('emp', 0), 2), round(ov.get('emp', 0), 2),
                                 *(round(100 * pv[f] / pv['emp'], 3) if pv.get('emp') else '' for f in ('gain', 'loss', 'churn')),
                                 *(round(100 * ov[f] / ov['emp'], 3) if ov.get('emp') else '' for f in ('gain', 'loss', 'churn'))])
                    if pv.get('emp') and ov.get('emp'):
                        detail[(lab, dim, c, sec, cl)].append((pv['emp'], ov['emp'], 100 * pv['gain'] / pv['emp'], 100 * ov['gain'] / ov['emp'],
                                                                100 * pv['loss'] / pv['emp'], 100 * ov['loss'] / ov['emp']))
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, f'heldout_{a.tag}.csv'), 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['panel', 'dimension', 'version', 'county', 'sector', 'year', 'quarter', 'class', 'emp_panel', 'emp_qwi',
                    'gain_rate_panel', 'loss_rate_panel', 'churn_rate_panel', 'gain_rate_qwi', 'loss_rate_qwi', 'churn_rate_qwi'])
        w.writerows(rows)
    labs = list(panels) + ['state baseline']
    L = ['Step 3e: held-out validation against QWI county x sector by firm size and by firm age (tables neither version uses)',
         'Scores per county x sector x quarter (2021 Q2 - 2024 Q4), mean over quarters (and versions; [min-max over versions]). Lower is better.',
         '  share TVD: firm-class employment shares; gain / loss / churn: mean |panel - QWI| rate by class, percentage points per quarter', '']
    for c, sec in sorted(keys):
        L.append(f'{c} sector {sec}: slice covers {st.median(cover[(c, sec)]):.0%} of the QWI county sector employment (median over quarters)')
        for dim in ('size', 'age'):
            L.append(f'  by firm {dim}:   {"":16s} {"share TVD":>16s} {"gain":>16s} {"loss":>16s} {"churn":>16s}')
            for lab in labs:
                v = S.get((lab, dim, c, sec))
                if not v: continue
                by_v = collections.defaultdict(list)
                if lab in panels:
                    # per version means
                    P = panels[lab]
                    for (m, cc, ss, y, q), agg in P.items():
                        if (cc, ss) != (c, sec): continue
                        o = obs[dim].get((c, sec, y, q))
                        if o and len(o) >= 4:
                            sc = scores(agg[dim], o)
                            if sc: by_v[m].append(sc)
                def cellfmt(f):
                    xs = [x[f] for x in v if x[f] is not None]
                    if not xs: return f'{"":>16s}'
                    s = f'{st.mean(xs):.3f}' if f == 'tvd' else f'{st.mean(xs):.2f}'
                    if by_v:
                        ms = [st.mean([x[f] for x in vv if x[f] is not None]) for vv in by_v.values()]
                        s += f' [{min(ms):.2f}-{max(ms):.2f}]' if f != 'tvd' else f' [{min(ms):.3f}-{max(ms):.3f}]'
                    return f'{s:>16s}'
                L.append(f'    {lab:24s} ' + ' '.join(cellfmt(f) for f in ('tvd', 'gain', 'loss', 'churn')))
        L.append('')
    L.append('Employment share and job-gain / job-loss rates by class, mean over quarters (panel versions pooled) vs QWI county x sector:')
    for c, sec in sorted(keys):
        for dim in ('size', 'age'):
            lab_names = SIZE_LABEL if dim == 'size' else AGE_LABEL
            L.append(f'  {c} {sec} by firm {dim}: class ' + '  '.join(f'{lab_names[k]:>22s}' for k in CLASSES))
            for lab in panels:
                parts = []
                for k in CLASSES:
                    d = detail.get((lab, dim, c, sec, k))
                    if not d: parts.append(f'{"":>22s}'); continue
                    parts.append(f'{st.mean(x[2] for x in d):5.1f}/{st.mean(x[4] for x in d):5.1f} (QWI {st.mean(x[3] for x in d):4.1f}/{st.mean(x[5] for x in d):4.1f})')
                L.append(f'    {lab:12s} gain/loss %   ' + '  '.join(f'{p:>22s}' for p in parts))
    open(os.path.join(a.out, f'heldout_report_{a.tag}.txt'), 'w').write('\n'.join(L) + '\n')
    print('\n'.join(L))


if __name__ == '__main__':
    main()
