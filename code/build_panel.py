"""Step 3b: link the step-3a establishments across quarters into a panel (random baseline).

Starts from the step-3a cross-section of the first quarter (2021 Q1, version m) and moves each county x 6-digit cell
forward one quarter at a time. Every quarter reproduces QCEW exactly (establishment count N, months 1-3, wages) and
takes gross flows from QWI (downscaled county x 6-digit, draw m) as RATES applied to QCEW levels.

Events per quarter (DATA_CONDITIONS C9):
  administrative (no worker or job flows; identity linked through pred_id):
    reclassification  establishment moves to another 6-digit cell of the same county (slice family), employment carried;
                      rates per establishment-quarter from calibrate_reclass.py (--reclass-rates; step 3c), else the
                      provisional Q1 2022 0.6%, other Q1 0.25%, Q2-Q4 0.04%
    split             a reporting unit breaks out into two (multi-site breakout); employment divided
    merge             two reporting units consolidate; employment combined
  economic (counted in job and worker flows):
    entry / exit      births and deaths at BLS BED Maryland quarterly rates (not seasonally adjusted; industry group;
                      openings / closings x the births / deaths share), fallback BDS (annual / 4); binomial draws;
    temporary closing / reopening   the rest of BED openings and closings: a continuer drops to zero employment and
                      stays in the count with its id (QCEW counts are far less seasonal than employment); dormant
                      units reopen at the BED rate (job and worker flows, but not births or deaths);
                      entrant size exponential with the BDS mean entrant size (job creation from births / entries)
  Count arbitration: the QCEW count change N_t - N_(t-1) not explained by the BDS-rate draws is
    Q1    administrative (splits if positive, merges if negative): Q1 count excess is mostly reporting changes (Table L)
    Q2-Q4 economic (extra entries or exits)
  QWI as arbiter (all quarters): QWI flows are corrected for successor-predecessor changes of employer IDs, so exits
    whose employment exceeds QWI-implied job losses are reclassified as merges, and entrants are scaled to fit within
    job gains.

Job flows (cell): net change D = QCEW month-3 change net of reclassification moves; gross reallocation
  R = max((QWI FrmJbGn + FrmJbLs) / QWI base x QCEW base, |D|); gains G = (R + D) / 2, losses L = (R - D) / 2.
  Bases: QWI (Emp + EmpEnd) / 2, QCEW (month 3 of t-1 + month 3 of t) / 2.
Worker flows: churn C = (QWI HirA - FrmJbGn) / QWI base x QCEW base (= Sep - FrmJbLs, B1); hires = G + C,
  separations = L + C. Within the cell (random baseline): each continuer grows or shrinks with probability G_c / (G_c +
  L_c), amounts proportional to size x Exp(1) (shrinkers capped at their employment); churn shared in proportion to size.
Months: continuers linear from month 3 of t-1 to month 3 of t, entrants from 0; then months 1 and 2 scaled to QCEW.
Firm size class is held through the year and updated in Q1 only (March reference), so seasonal peaks don't move a
  firm up a class permanently. Firm age advances 0.25 years a quarter; entrants are new firms (age 0, size class 1 if feasible) with the BDS share
  of entries at firm age 0, otherwise establishments of existing firms (age and size drawn from QWI margins).

Outputs (<out_dir>): panel_<tag>.csv.gz (version x quarter x establishment, incl. flows), exits_<tag>.csv.gz (exits and
merged-away units), cells_<tag>.csv.gz (cell x quarter accounting incl. the naive unlinked counterfactual), report_<tag>.txt
Usage: python3 build_panel.py <qcew_raw_root> <qcew_imputed_dir> <qcew_complete_dir> <cbp_dir> <qwi_dir> <qwi_downscaled.csv.gz>
                              <bds_st_sec_fa.csv.gz> <establishments_3a.csv.gz> <out_dir> [--draws 5] [--seed 13] [--tag slices]
"""
import argparse, collections, csv, gzip, math, os, statistics as st, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond
import create_establishments as ce
import priors

RECLASS_RATE = {'q1_2022': 0.006, 'q1': 0.0025, 'other': 0.0004}   # provisional; replaced by --reclass-rates (step 3c)
AGE_BOUNDS = {'1': (0, 2), '2': (2, 4), '3': (4, 6), '4': (6, 11), '5': (11, 40)}
FS_UPPER = {k: hi for k, lo, hi in ce.FIRM_SIZE}
BDS_SECTOR = {'31': '31-33', '32': '31-33', '33': '31-33', '44': '44-45', '45': '44-45', '48': '48-49', '49': '48-49'}


def age_class(years):
    return next(k for k, (lo, hi) in AGE_BOUNDS.items() if years < hi or k == '5')


def num(v):
    try: return float(v)
    except ValueError: return None


def load_bds(path):
    """{(sector, year): rates} from BDS state x sector x firm age (Maryland): quarterly entry and exit probabilities,
    share of entries at firm age 0, mean entrant size."""
    rows = collections.defaultdict(list)
    with cond.open_text(path) as fh:
        for r in csv.DictReader(fh): rows[(r['sector'], int(r['year']))].append(r)
    out = {}
    for k, R in rows.items():
        est = sum(num(x['estabs']) or 0 for x in R); ent = sum(num(x['estabs_entry']) or 0 for x in R)
        ex = sum(num(x['estabs_exit']) or 0 for x in R)
        e0 = sum(num(x['estabs_entry']) or 0 for x in R if x['fage'].startswith('a)'))
        jcb = sum(num(x['job_creation_births']) or 0 for x in R)
        if est > 0 and ent > 0:
            out[k] = {'entry_q': ent / est / 4, 'exit_q': ex / est / 4, 'fage0_share': e0 / ent, 'entrant_size': jcb / ent}
    return out


BED_GROUP = {'11': '100010', '21': '100010', '23': '100020', '31': '100030', '32': '100030', '33': '100030', '42': '200010',
             '44': '200020', '45': '200020', '48': '200030', '49': '200030', '22': '200040', '51': '200050', '52': '200060',
             '53': '200060', '54': '200070', '55': '200070', '56': '200070', '61': '200080', '62': '200080', '71': '200090',
             '72': '200090', '81': '200100'}


def load_bed(bed_dir):
    """{(BED industry group, y, q): rates} for Maryland private establishments, not seasonally adjusted: births and deaths
    (permanent; = openings / closings x the births / deaths share of Maryland total private, seasonally adjusted levels)
    and temporary closings and reopenings (the rest of closings and openings). Probabilities per establishment-quarter."""
    ser = {}
    with open(cond.require(os.path.join(bed_dir, 'bd.series'))) as fh:
        for r in csv.DictReader(fh, delimiter='\t'):
            r = {k.strip(): v.strip() for k, v in r.items()}
            if r['state_code'] == '24' and r['periodicity_code'] == 'Q' and r['dataelement_code'] == '2': ser[r['series_id']] = r
    v = collections.defaultdict(dict)
    data = [f for f in os.listdir(bed_dir) if f.startswith('bed_md_') and f.endswith('.csv.gz')]
    if not data: sys.exit(f'no bed_md_*.csv.gz in {bed_dir}')
    with cond.open_text(os.path.join(bed_dir, sorted(data)[-1])) as fh:
        for r in csv.DictReader(fh):
            s_ = ser.get(r['series_id']); x = num(r['value'])
            if s_ and x is not None:
                v[(s_['seasonal'], s_['industry_code'], s_['dataclass_code'], s_['ratelevel_code'])][(int(r['year']), int(r['period'][1:]))] = x
    tot = lambda dc: v[('S', '000000', dc, 'L')]
    bshare = {k: tot('07')[k] / tot('03')[k] for k in tot('07') if tot('03').get(k)}
    dshare = {k: tot('08')[k] / tot('06')[k] for k in tot('08') if tot('06').get(k)}
    mb, md = st.mean(bshare.values()), st.mean(dshare.values())
    out = {}
    for (sa, ind, dc, rl), d in v.items():
        if sa != 'U' or rl != 'R' or dc != '03': continue
        for k, o in d.items():
            c = v[('U', ind, '06', 'R')].get(k)
            if c is None: continue
            b, dd = bshare.get(k, mb), dshare.get(k, md)
            out[(ind,) + k] = {'birth_q': o / 100 * b, 'death_q': c / 100 * dd, 'reopen_q': o / 100 * (1 - b), 'tempclose_q': c / 100 * (1 - dd)}
    return out


def load_qwi_flows(path, draws, keep):
    """{(m, county, naics6, y, q): {Emp, EmpEnd, FrmJbGn, FrmJbLs, HirA, Sep}} from the downscaled QWI file.
    `draws`: number of versions (1..draws) or a list of versions (F13)."""
    out = {}; vs = set(range(1, draws + 1)) if isinstance(draws, int) else set(draws)
    with cond.open_text(path) as fh:
        for r in csv.DictReader(fh):
            m = int(r['draw'])
            if m in vs and keep(r['area_fips'], r['industry_code']):
                out[(m, r['area_fips'], r['industry_code'], int(r['year']), int(r['quarter']))] = \
                    {v: float(r[v]) for v in ('Emp', 'EmpEnd', 'FrmJbGn', 'FrmJbLs', 'HirA', 'Sep')}
    return out


def qwi_rates(flows, m, c, n, y, q):
    """(gain rate, loss rate, churn rate) per unit of QWI base employment; falls back to the cell's mean over quarters."""
    def rates(f):
        b = (f['Emp'] + f['EmpEnd']) / 2
        if b <= 0: return None
        return f['FrmJbGn'] / b, f['FrmJbLs'] / b, max(f['HirA'] - f['FrmJbGn'], 0.0) / b
    f = flows.get((m, c, n, y, q))
    r = rates(f) if f else None
    if r: return r, 'cell'
    alt = [rates(v) for (mm, cc, nn, _, _), v in flows.items() if (mm, cc, nn) == (m, c, n)]
    alt = [a for a in alt if a]
    if alt: return tuple(st.mean(a[i] for a in alt) for i in range(3)), 'cell_mean'
    return (0.0, 0.0, 0.0), 'none'


def binom(rng, n, p):
    return sum(1 for _ in range(n) if rng.random() < p)


def bounded_split(total, weights, caps):
    """Shares of `total` proportional to weights, each capped (iterative redistribution)."""
    x = [0.0] * len(weights); free = [i for i, w in enumerate(weights) if w > 0 and caps[i] > 0]; rem = total
    while rem > 1e-9 and free:
        wt = sum(weights[i] for i in free)
        if wt <= 0: break
        nxt = []
        add = {i: rem * weights[i] / wt for i in free}
        rem = 0.0
        for i in free:
            room = caps[i] - x[i]
            if add[i] >= room: x[i] = caps[i]; rem += add[i] - room
            else: x[i] += add[i]; nxt.append(i)
        free = nxt
    return x, rem


# per-establishment uniforms (cond.uniforms): fixed positions, so arms draw the same numbers for the same decision
U_RECLASS, U_DEST, U_EXIT, U_CLOSE, U_SIGN, U_MAG = range(6); U_N = 6
# cell-level steps, each with its own stream per version x county x industry x quarter
CELL_STEPS = ('count', 'events', 'swap', 'merge', 'split', 'reopen', 'growth', 'firm_size', 'firm_age')


class Est:
    __slots__ = ('id', 'cell', 'e_prev', 'e', 'fs', 'age', 'event', 'pred', 'm1', 'm2', 'gain', 'loss', 'churn', 'u')

    def __init__(self, id_, cell, e, fs, age, event='continuer', pred=''):
        self.id, self.cell, self.e, self.fs, self.age, self.event, self.pred = id_, cell, e, fs, age, event, pred
        self.e_prev = e; self.m1 = self.m2 = 0.0; self.gain = self.loss = self.churn = 0.0; self.u = None


def main():
    ap = argparse.ArgumentParser()
    for x in ('raw', 'imp', 'complete', 'cbp', 'qwi', 'qwi_down', 'bds', 'est3a', 'out'): ap.add_argument(x)
    ap.add_argument('--reclass-rates', default='', help='reclass_rates.csv from calibrate_reclass.py (step 3c)')
    ap.add_argument('--reclass-bound', default='rho', choices=('rho', 'rho_lo90', 'rho_hi90'))
    ap.add_argument('--prior', default='random', choices=('random', 'systematic'),
                    help='systematic: exit, growth and churn by firm age and size (BDS, QWI; step 3d)')
    ap.add_argument('--class-targets', type=int, default=1, help='1: split gains, losses and churn across firm size classes and '
                    'young / mature age groups to QWI state 6-digit counts (step 3d v1); 0: earlier allocation')
    ap.add_argument('--margin-feedback', type=float, default=2.0,
                    help='gamma: exit risk x (current / QWI-margin employment share)^gamma per firm age and size class (0 = off)')
    ap.add_argument('--county-size-cal', default='data/processed/county_size_calibration/county_size_calibration.csv',
                    help="D11: transfer variances from calibrate_county_size.py; '' = state 6-digit firm-size margins only")
    ap.add_argument('--bds-fz', default='data/raw/external/bds/bds2023_st_sec_fz_md_2019-2023.csv.gz')
    ap.add_argument('--bed', default='', help='BED directory (bd.series + bed_md_*.csv.gz); empty = BDS rates only')
    ap.add_argument('--only', default='24047:7211,24047:7225,24031:5417'); ap.add_argument('--draws', type=int, default=5)
    ap.add_argument('--seed', type=int, default=13); ap.add_argument('--tag', default='slices')
    ap.add_argument('--versions', default='', help="'' = 1..draws; 'A-B' = only these versions (one Slurm array task per version; merge_versions.py)")
    a = ap.parse_args()
    cond.start_run(*[v for v in vars(a).values() if isinstance(v, str) and os.path.exists(v)])  # F11
    only = [tuple(x.split(':')) for x in a.only.split(',') if x]
    keep = (lambda c, n: any(c == oc and n.startswith(op) for oc, op in only)) if only else (lambda c, n: True)   # no --only: all cells
    V = cond.parse_versions(a.versions, a.draws)
    qcew = ce.load_qcew(a.raw, a.imp, a.complete, V, keep)
    flows = load_qwi_flows(cond.require(a.qwi_down), V, keep)
    bds = load_bds(cond.require(a.bds))
    bed = load_bed(a.bed) if a.bed else {}
    DYN = CH = None
    if a.prior == 'systematic':
        DYN = priors.dynamics(a.bds, a.bds_fz)
        CH = priors.churn(os.path.join(a.qwi, 'qwi_md_sa_fa_gs_n6_op_u_2021-2024.csv.gz'), os.path.join(a.qwi, 'qwi_md_sa_fs_gs_n6_op_u_2021-2024.csv.gz'))
    CT = None
    if a.class_targets:
        CT = {'size': priors.class_flow_rates(os.path.join(a.qwi, 'qwi_md_sa_fs_gs_n6_op_u_2021-2024.csv.gz'), 'firmsize'),
              'age': priors.class_flow_rates(os.path.join(a.qwi, 'qwi_md_sa_fa_gs_n6_op_u_2021-2024.csv.gz'), 'firmage')}
    one = collections.defaultdict(lambda: 1.0)
    rel = lambda tab, sec, key: (tab.get(sec, {}).get(key) or one) if tab else one
    if a.reclass_rates:
        with open(cond.require(a.reclass_rates)) as fh:
            RECLASS_RATE.update({r['group']: float(r[a.reclass_bound]) for r in csv.DictReader(fh)})
    log_rates = dict(RECLASS_RATE)
    fsz = ce.qwi_margins(os.path.join(a.qwi, 'qwi_md_sa_fs_gs_n6_op_u_2021-2024.csv.gz'), 'firmsize')
    # D11: firm-size targets consistent with QWI county x sector shares (reliability-weighted), unless disabled
    CST = {}
    if a.county_size_cal:
        cs_keep = {(c, priors.bds_sector(p_)) for c, p_ in only}
        qcew_sector = ce.load_qcew(a.raw, a.imp, a.complete, V, (lambda c, n_: (c, priors.bds_sector(n_)) in cs_keep) if only else (lambda c, n_: True))
        CST = priors.county_size_targets(qcew_sector, fsz, priors.county_sector_size(os.path.join(a.qwi, 'qwi_md_sa_fs_gc_ns_op_u_2021-2024.csv.gz')),
                                         priors.load_transfer_var(a.county_size_cal))
    fs_target = lambda m_, c_, n_, y_, q_: CST.get((m_, c_, n_, y_, q_)) or fsz.get((n_, y_, q_))
    fag = ce.qwi_margins(os.path.join(a.qwi, 'qwi_md_sa_fa_gs_n6_op_u_2021-2024.csv.gz'), 'firmage')
    quarters = sorted(qcew)
    y0, q0 = quarters[0]

    # first quarter = step-3a cross-section
    init = collections.defaultdict(list)
    with cond.open_text(cond.require(a.est3a)) as fh:
        for r in csv.DictReader(fh):
            m = int(r['version'])
            if m in V and (int(r['year']), int(r['quarter'])) == (y0, q0):
                init[m].append(r)
    if not init: sys.exit(f'no step-3a establishments for {y0}Q{q0} in {a.est3a}')

    os.makedirs(a.out, exist_ok=True)
    paths = {k: os.path.join(a.out, f'{k}_{a.tag}.csv.gz') for k in ('panel', 'exits', 'cells')}
    fh_p = gzip.open(paths['panel'] + '.part', 'wt', newline=''); wp = csv.writer(fh_p)
    fh_x = gzip.open(paths['exits'] + '.part', 'wt', newline=''); wx = csv.writer(fh_x)
    fh_c = gzip.open(paths['cells'] + '.part', 'wt', newline=''); wc = csv.writer(fh_c)
    wp.writerow(['version', 'year', 'quarter', 'county', 'naics6', 'est_id', 'event', 'pred_id', 'emp_prev_m3', 'emp_m1', 'emp_m2',
                 'emp_m3', 'emp_avg', 'wages', 'job_gain', 'job_loss', 'hires', 'separations', 'churn', 'firm_size_class',
                 'firm_age_class', 'firm_age_years'])
    wx.writerow(['version', 'year', 'quarter', 'county', 'naics6', 'est_id', 'event', 'emp_prev_m3', 'successor_id', 'firm_size_class', 'firm_age_class'])
    CELL_COLS = ['version', 'year', 'quarter', 'county', 'naics6', 'qtype', 'N_prev', 'N', 'emp_prev_m3', 'emp_m3', 'base',
                 'reclass_in', 'reclass_out', 'reclass_in_emp', 'reclass_out_emp', 'splits', 'split_emp', 'merges', 'merge_emp',
                 'entries', 'entry_emp', 'exits', 'exit_emp', 'exits_to_merges', 'gains', 'losses', 'churn', 'hires', 'separations',
                 'qwi_gain_rate', 'qwi_loss_rate', 'qwi_churn_rate', 'qwi_rate_source', 'R_raised', 'naive_gains', 'naive_losses',
                 'temp_closings', 'temp_closing_emp', 'reopenings', 'entry_exit_rates', 'deaths_swapped_to_dormant']
    wc.writerow(CELL_COLS)
    log = collections.Counter(); margin_gap = collections.defaultdict(list); exact = []

    # Random streams (F4, S4a): one per version x county x industry x quarter for cell-level draws, plus a fixed set of
    # uniforms per establishment x quarter (EU) for its own decisions, so the same establishment gets the same draws under
    # every prior and a difference in one cell never shifts the draws of another (common random numbers across arms).
    EU = lambda m, id_, y, q: cond.uniforms(U_N, a.seed, m, id_, y, q)
    for m in V:
        ests = {}
        for r in init[m]:
            lo, hi = AGE_BOUNDS[r['firm_age_class']]
            e = Est(r['est_id'], r['naics6'], float(r['emp_m3']), r['firm_size_class'], lo + cond.uniforms(1, a.seed, m, r['est_id'], 'age0')[0] * (hi - lo), 'initial')
            e.m1, e.m2 = float(r['emp_m1']), float(r['emp_m2'])
            ests[e.id] = (r['county'], e)
        counties = sorted({c for c, _ in ests.values()})
        seq = collections.Counter()
        # write first quarter
        for c, e in ests.values():
            avg = (e.m1 + e.m2 + e.e) / 3
            cell_q = qcew[(y0, q0)][m][(c, e.cell)]
            T = sum(cell_q[1:4]) / 3
            wp.writerow([m, y0, q0, c, e.cell, e.id, 'initial', '', '', cond.out_value(e.m1), cond.out_value(e.m2), cond.out_value(e.e),
                         cond.out_value(avg), cond.out_value(cell_q[4] * avg / T if T > 0 else 0), '', '', '', '', '', e.fs, age_class(e.age),
                         round(e.age, 2)])

        for (yp, qp), (y, q) in zip(quarters[:-1], quarters[1:]):
            qtype = 'q1_2022' if (y, q) == (2022, 1) else ('q1' if q == 1 else 'other')
            cells_t = qcew[(y, q)][m]; cells_p = qcew[(yp, qp)][m]
            for c in counties:
                fam = sorted({n for (cc, n) in list(cells_t) + list(cells_p) if cc == c})
                by_cell = collections.defaultdict(list)
                for cc, e in ests.values():
                    if cc == c: by_cell[e.cell].append(e); e.u = EU(m, e.id, y, q)
                acc = {n: collections.Counter() for n in fam}
                prev_m3 = {n: sum(e.e for e in by_cell[n]) for n in fam}
                for n in fam:
                    for e in by_cell[n]: e.e_prev = e.e; e.event = 'continuer'; e.pred = ''; e.gain = e.loss = e.churn = 0.0
                # 1. reclassification within the county family (administrative; employment carried)
                for n in fam:
                    for e in list(by_cell[n]):
                        if len(fam) > 1 and e.event != 'reclass_in' and e.u[U_RECLASS] < RECLASS_RATE[qtype]:   # at most once a quarter
                            dest = [d for d in fam if d != n and (c, d) in cells_t and priors.bds_sector(d) == priors.bds_sector(n)]   # within sector (step 3c calibration)
                            if not dest: continue
                            w = [4 if d[:5] == n[:5] else (2 if d[:4] == n[:4] else 1) for d in dest]
                            u = e.u[U_DEST] * sum(w); d = dest[-1]
                            for dd, ww in zip(dest, w):
                                u -= ww
                                if u <= 0: d = dd; break
                            by_cell[n].remove(e); by_cell[d].append(e); e.cell = d; e.event = 'reclass_in'; e.pred = e.id
                            acc[n]['reclass_out'] += 1; acc[n]['reclass_out_emp'] += e.e
                            acc[d]['reclass_in'] += 1; acc[d]['reclass_in_emp'] += e.e
                for n in fam:
                    cur = by_cell[n]; A = acc[n]
                    RS = {k: cond.rng_for(a.seed, m, c, n, y, q, k) for k in CELL_STEPS}   # one stream per step (S4a)
                    if (c, n) not in cells_t:
                        for e in cur:
                            wx.writerow([m, y, q, c, n, e.id, 'exit', cond.out_value(e.e), '', e.fs, age_class(e.age)]); del ests[e.id]
                            A['exits'] += 1; A['exit_emp'] += e.e
                        log['cell_disappeared'] += 1
                        continue
                    est, m1, m2, m3, wages = cells_t[(c, n)]
                    N_t = int(est) + (1 if RS['count'].random() < est - int(est) else 0)
                    sec = n[:2]; br = bds.get((BDS_SECTOR.get(sec, sec), min(y, 2023)))
                    if br is None: br = {'entry_q': 0.025, 'exit_q': 0.025, 'fage0_share': 0.6, 'entrant_size': 5.0}; log['bds_missing'] += 1
                    N0 = len(cur)
                    # 2. economic entry and exit at BDS rates, then count arbitration
                    bq = bed.get((BED_GROUP.get(sec, ''), y, q))
                    if bq: p_exit, p_entry = bq['death_q'], bq['birth_q']; log['rates_bed'] += 1
                    else: p_exit, p_entry = br['exit_q'], br['entry_q']; log['rates_bds'] += 1
                    X = binom(RS['events'], N0, p_exit); B = binom(RS['events'], N0, p_entry)
                    r = (N_t - N0) - (B - X)
                    splits = merges = 0
                    if r > 0:
                        if q == 1: splits = r
                        else: B += r
                    elif r < 0:
                        if q == 1: merges = -r
                        else: X += -r
                    X = min(X, N0); merges = min(merges, max(N0 - X - 1, 0))
                    if N_t == 0: X, B, splits, merges = N0, 0, 0, 0; log['cell_count_zero_all_exit'] += 1   # QCEW count 0: every establishment exits
                    if N0 - X - merges < 1: splits = 0
                    if N0 - X - merges + B + splits != N_t:  # limits bound: fill with economic events
                        B += N_t - (N0 - X - merges + B + splits); log['count_fill_economic'] += 1
                        if B < 0: X += -B; B = 0
                    # exits: weighted sampling without replacement (key u^(1/w), largest first); w = 1 is a uniform random order
                    pool = sorted(cur, key=lambda e: -e.u[U_EXIT])
                    if DYN:  # systematic: exit risk by firm age and size (BDS), weighted sampling without replacement
                        ra, rs = rel(DYN, priors.bds_sector(n), 'exit_age'), rel(DYN, priors.bds_sector(n), 'exit_size')
                        if CT: rg_ = rel(DYN, priors.bds_sector(n), 'exit_agegrp'); ra = {k: rg_[priors.age_group(k)] for k in priors.AGES}
                        keys = [e.u[U_EXIT] ** (1 / max(ra[age_class(e.age)] * rs[e.fs], 1e-6)) for e in pool]
                        pool = [e for _, e in sorted(zip(keys, pool), key=lambda t: -t[0])]
                    if a.margin_feedback > 0 and X > 0:  # exits pull the firm age / size composition towards the QWI margins
                        ta, ts = fag.get((n, y, q)), fs_target(m, c, n, y, q)
                        tot_e = sum(e.e for e in pool)
                        if ta and ts and tot_e > 0:
                            ca_, cs2 = collections.Counter(), collections.Counter()
                            for e in pool: ca_[age_class(e.age)] += e.e; cs2[e.fs] += e.e
                            kap = lambda cur, t: min(max(((cur / tot_e) / t) ** a.margin_feedback if t > 0 else 10.0, 0.1), 10.0)
                            ka = {k: kap(ca_[k], ta[k]) for k in priors.AGES}; ks = {k: kap(cs2[k], ts[k]) for k in priors.SIZES}
                            if DYN:
                                ra, rs = rel(DYN, priors.bds_sector(n), 'exit_age'), rel(DYN, priors.bds_sector(n), 'exit_size')
                                if CT: rg_ = rel(DYN, priors.bds_sector(n), 'exit_agegrp'); ra = {k: rg_[priors.age_group(k)] for k in priors.AGES}
                            else:
                                ra = rs = one
                            keys = [e.u[U_EXIT] ** (1 / max(ra[age_class(e.age)] * rs[e.fs] * ka[age_class(e.age)] * ks[e.fs], 1e-6)) for e in pool]
                            pool = [e for _, e in sorted(zip(keys, pool), key=lambda t: -t[0])]
                    exits, rest = pool[:X], pool[X:]
                    # 3. QWI arbiter for exits: job losses implied by QWI must cover exits' employment
                    cont_prev = sum(e.e for e in rest)
                    D = m3 - (cont_prev + sum(e.e for e in exits))
                    (g, l, ch), src = qwi_rates(flows, m, c, n, y, q)
                    base = (prev_m3[n] + m3) / 2
                    R = max((g + l) * base, abs(D)); R_raised = (g + l) * base < abs(D)
                    G, L = (R + D) / 2, (R - D) / 2
                    exits.sort(key=lambda e: -e.e)
                    # deaths whose employment exceeds QWI job losses: first swap for the death of a dormant unit
                    # (BED: a death is a closing without employment in the next four quarters), then merges
                    dormant = [e for e in rest if e.e <= 1e-9]
                    while exits and dormant and sum(e.e for e in exits) > L + 1e-9:
                        big = exits.pop(0); d_ = dormant.pop(int(RS['swap'].random() * len(dormant)))
                        rest.remove(d_); rest.append(big); exits.append(d_); A['deaths_swapped_to_dormant'] += 1
                        exits.sort(key=lambda e: -e.e)
                    to_merge = []
                    while exits and sum(e.e for e in exits) > L + 1e-9 and len(rest) > len(to_merge):
                        to_merge.append(exits.pop(0)); A['exits_to_merges'] += 1
                    if exits and sum(e.e for e in exits) > L + 1e-9:
                        L = sum(e.e for e in exits); G = L + D; log['L_raised_for_exits'] += 1; R_raised = True

                    def merge(a_, b_):
                        """b_ consolidates into a_ (administrative): employment and history carried, no flows."""
                        a_.e += b_.e; a_.e_prev += b_.e_prev
                        if a_.event == 'continuer': a_.event = 'merge_survivor'
                        a_.pred = (a_.pred + ';' if a_.pred else a_.id + ';') + b_.id
                        if b_ in rest: rest.remove(b_)
                        del ests[b_.id]
                        wx.writerow([m, y, q, c, n, b_.id, 'merged_away', cond.out_value(b_.e), a_.id, b_.fs, age_class(b_.age)])
                        A['merges'] += 1; A['merge_emp'] += a_.e
                    # exits that QWI's job losses can't cover: consolidated into a random continuer
                    for b_ in to_merge: merge(rest[int(RS['merge'].random() * len(rest))], b_)
                    # Q1 count residual: random pairs consolidate
                    for _ in range(merges):
                        if len(rest) < 2: break
                        i, j = int(RS['merge'].random() * len(rest)), int(RS['merge'].random() * (len(rest) - 1))
                        j = j + 1 if j >= i else j
                        a_, b_ = (rest[i], rest[j]) if rest[i].e >= rest[j].e else (rest[j], rest[i])
                        merge(a_, b_)
                    # splits: one reporting unit breaks out into two (administrative)
                    for _ in range(splits):
                        cand = [e for e in rest if e.e > 0] or rest
                        if not cand: break
                        p = cand[int(RS['split'].random() * len(cand))]; seq[(c, n)] += 1
                        f = 0.2 + 0.6 * RS['split'].random()
                        nid = f'{c}-{n}-{y}q{q}-s{seq[(c, n)]:04d}'
                        s = Est(nid, n, p.e * f, p.fs, p.age, 'split_successor', p.id); s.e_prev = p.e_prev * f; s.u = EU(m, nid, y, q)
                        A['split_emp'] += p.e
                        p.e *= 1 - f; p.e_prev *= 1 - f
                        rest.append(s); ests[nid] = (c, s); A['splits'] += 1
                    for e in exits:
                        wx.writerow([m, y, q, c, n, e.id, 'exit', cond.out_value(e.e), '', e.fs, age_class(e.age)]); del ests[e.id]
                        A['exits'] += 1; A['exit_emp'] += e.e
                    # 4. entrants: exponential sizes with the BDS mean, scaled to fit within gains
                    ent = []
                    for k in range(B):
                        seq[(c, n)] += 1
                        rng = cond.rng_for(a.seed, m, c, n, y, q, 'entrant', k)   # entrant k gets the same numbers in every arm
                        size = -br['entrant_size'] * math.log(1 - rng.random())
                        if rng.random() < br['fage0_share']: age, fs = 0.0, 'new'
                        else:
                            fa_t = fag.get((n, y, q)) or {k_: 0.2 for k_ in ce.FIRM_AGE}
                            ks = [k_ for k_ in ('2', '3', '4', '5')]; w = [fa_t.get(k_, 0) + 1e-9 for k_ in ks]
                            u = rng.random() * sum(w); cls = ks[-1]
                            for k_, w_ in zip(ks, w):
                                u -= w_
                                if u <= 0: cls = k_; break
                            lo, hi = AGE_BOUNDS[cls]; age = lo + rng.random() * (hi - lo); fs = 'draw'
                        ent.append(Est(f'{c}-{n}-{y}q{q}-e{seq[(c, n)]:04d}', n, size, fs, age, 'entry')); ent[-1].e_prev = 0.0
                    se = sum(e.e for e in ent)
                    if se > G or (not rest and se > 0):  # no continuers: entrants take all the gains
                        for e in ent: e.e *= G / se
                        log['entrants_scaled_to_gains'] += 1
                    se = sum(e.e for e in ent)
                    for e in ent: e.gain = e.e
                    # 5. continuers' job gains and losses
                    Gc, Lc = G - se, L - sum(e.e for e in exits)
                    cap = sum(e.e for e in rest)
                    if Lc > cap + 1e-9:
                        cut = Lc - cap; Lc -= cut; Gc -= cut; log['reallocation_capped'] += 1
                        if Gc < 0 and se > 0:  # gains now below the entrants' employment: scale entrants down
                            f_ = max(se + Gc, 0.0) / se
                            for e in ent: e.e *= f_; e.gain = e.e
                            se = sum(e.e for e in ent); Gc = 0.0; log['entrants_scaled_after_cap'] += 1
                    # temporary closings (continuer to zero, stays in the count, keeps its id) and reopenings of dormant
                    # units, at BED rates; both are economic (job and worker flows) but not births or deaths
                    forced = {}
                    if bq and rest:
                        dorm = [i for i, e in enumerate(rest) if e.e <= 1e-9]
                        n_re = binom(RS['reopen'], len(rest), bq['reopen_q'])
                        if Gc > 1e-9:
                            for i in dorm[:n_re] if len(dorm) <= n_re else [dorm[j] for j in sorted(range(len(dorm)), key=lambda _: RS['reopen'].random())[:n_re]]:
                                forced[i] = 'grow'; A['reopenings'] += 1; rest[i].event = 'reopening'
                        budget = Lc
                        for i, e in enumerate(rest):
                            if i not in forced and e.e > 1e-9 and e.e <= budget and e.u[U_CLOSE] < bq['tempclose_q']:
                                forced[i] = 'close'; budget -= e.e; A['temp_closings'] += 1; A['temp_closing_emp'] += e.e
                    cls_rates = None
                    if CT and rest and (Gc > 1e-9 or Lc > 1e-9):
                        (rsz, lv_s), (rag, lv_a) = CT['size'](n, y, q), CT['age'](n, y, q)
                        log[f'class_rates_size_level_{lv_s}'] += 1; log[f'class_rates_age_level_{lv_a}'] += 1
                        if rsz and rag: cls_rates = (rsz, rag)
                    if cls_rates:
                        allocate_by_class(rest, ent, exits, forced, Gc, Lc, G, L, cls_rates, RS['growth'], log, age_class)
                    elif rest and (Gc > 1e-9 or Lc > 1e-9):
                        pg = Gc / (Gc + Lc) if Gc + Lc > 0 else 0.5
                        if DYN:  # systematic: growth propensity and size by firm age and size (BDS continuers)
                            sec_ = priors.bds_sector(n)
                            ca, cs_ = rel(DYN, sec_, 'jc_age'), rel(DYN, sec_, 'jc_size')
                            da, ds_ = rel(DYN, sec_, 'jd_age'), rel(DYN, sec_, 'jd_size')
                            cr = [ca[age_class(e.age)] * cs_[e.fs] for e in rest]; dr = [da[age_class(e.age)] * ds_[e.fs] for e in rest]
                            pgs = [Gc * x / (Gc * x + Lc * z) if Gc * x + Lc * z > 0 else 0.5 for x, z in zip(cr, dr)]
                        else:
                            cr = dr = [1.0] * len(rest); pgs = [pg] * len(rest)
                        sign = [forced[i] == 'grow' if i in forced else rest[i].u[U_SIGN] < pgs[i] for i in range(len(rest))]
                        if Gc > 1e-9 and not any(sign): sign[int(RS['growth'].random() * len(rest))] = True
                        mag = [max(e.e, 0.5) * -math.log(1 - e.u[U_MAG]) for e in rest]
                        mag = [mm * (x if sg else z) for mm, sg, x, z in zip(mag, sign, cr, dr)]
                        gw = [mm if sg else 0 for mm, sg in zip(mag, sign)]
                        lw = [mm if not sg and i not in forced else 0 for i, (mm, sg) in enumerate(zip(mag, sign))]
                        closed = sum(rest[i].e for i, f_ in forced.items() if f_ == 'close')
                        loss, left = bounded_split(Lc - closed, lw, [e.e for e in rest])
                        loss = [rest[i].e if forced.get(i) == 'close' else x for i, x in enumerate(loss)]
                        if left > 1e-9:  # not enough capacity among shrinkers: let growers shrink too
                            extra, left = bounded_split(left, [max(e.e - x, 0) for e, x in zip(rest, loss)], [e.e - x for e, x in zip(rest, loss)])
                            loss = [x + y_ for x, y_ in zip(loss, extra)]
                        gain = [Gc * w / sum(gw) for w in gw] if sum(gw) > 0 else [0.0] * len(rest)
                        for e, gg, ll in zip(rest, gain, loss):
                            d = gg - ll; e.gain, e.loss = max(d, 0.0), max(-d, 0.0); e.e = e.e + d
                    # 6. churn (worker flows beyond job flows), shared in proportion to size
                    C = ch * base
                    sz = [(e.e_prev + e.e) / 2 for e in rest]
                    if cls_rates:  # churn split across firm size classes by QWI churn rates (second priority)
                        rsz = cls_rates[0]
                        sz = [x * rsz[e.fs][2] for x, e in zip(sz, rest)]
                        if CH:  # systematic: within the size margin, churn varies with firm age (QWI)
                            ha = rel(CH, priors.bds_sector(n), 'age'); sz = [x * ha[age_class(e.age)] for x, e in zip(sz, rest)]
                    elif CH:  # systematic: churn per job by firm age and size (QWI)
                        ha, hs = rel(CH, priors.bds_sector(n), 'age'), rel(CH, priors.bds_sector(n), 'size')
                        sz = [x * ha[age_class(e.age)] * hs[e.fs] for x, e in zip(sz, rest)]
                    if rest and sum(sz) > 0:
                        for e, s_ in zip(rest, sz): e.churn = C * s_ / sum(sz)
                    elif C > 0: log['churn_without_continuers'] += 1; C = 0.0
                    # 7. months: linear paths, months 1 and 2 scaled to QCEW
                    live = rest + ent
                    if not live: log['empty_cell_quarter'] += 1; continue
                    for e in live:
                        e.m1 = e.e_prev + (e.e - e.e_prev) / 3; e.m2 = e.e_prev + 2 * (e.e - e.e_prev) / 3
                    for attr, tot in (('m1', m1), ('m2', m2)):
                        s_ = sum(getattr(e, attr) for e in live)
                        for e in live: setattr(e, attr, getattr(e, attr) * tot / s_ if s_ > 0 else tot / len(live))
                    # firm attributes: survivors keep their class; entrants (and, in Q1, establishments that outgrew
                    # their class) draw a class with probabilities raked so the cell returns towards the QWI margin
                    for e in rest: e.age += 0.25
                    movers = [e for e in ent if e.fs != 'new'] + [e for e in rest if q == 1 and e.e > FS_UPPER[e.fs]]
                    log['firm_size_bumped'] += sum(1 for e in movers if e.event != 'entry')
                    for e in ent:
                        if e.fs == 'new': e.fs = next(k for k, lo, hi in ce.FIRM_SIZE if hi >= e.e)
                    fs_t = fs_target(m, c, n, y, q)
                    if movers:
                        sz = [max((e.m1 + e.m2 + e.e) / 3, 1e-6) for e in movers]
                        feas = [[k for k, lo, hi in ce.FIRM_SIZE if hi >= e.e] for e in movers]
                        tgt = {k: 0.2 for k, _, _ in ce.FIRM_SIZE}
                        if fs_t:
                            fixed = collections.Counter()
                            for e in live:
                                if e not in movers: fixed[e.fs] += (e.m1 + e.m2 + e.e) / 3
                            Ttot = sum(fixed.values()) + sum(sz)
                            res = {k: max(fs_t[k] * Ttot - fixed[k], 0.0) for k in fs_t}
                            if sum(res.values()) > 0: tgt = {k: v / sum(res.values()) for k, v in res.items()}
                        for e, k in zip(movers, ce.rake_assign(sz, feas, tgt, [RS['firm_size'].random() for _ in movers])): e.fs = k
                    fa_t = fag.get((n, y, q)); old_firm = [e for e in ent if e.age > 0]
                    if fa_t and old_firm:  # existing-firm entrants: age class raked to the residual of the margin
                        fixed = collections.Counter()
                        for e in live:
                            if e not in old_firm: fixed[age_class(e.age)] += (e.m1 + e.m2 + e.e) / 3
                        sz = [max((e.m1 + e.m2 + e.e) / 3, 1e-6) for e in old_firm]; Ttot = sum(fixed.values()) + sum(sz)
                        res = {k: max(fa_t[k] * Ttot - fixed[k], 0.0) for k in ('2', '3', '4', '5')}
                        if sum(res.values()) > 0:
                            tgt = {k: v / sum(res.values()) for k, v in res.items()}
                            for e, k in zip(old_firm, ce.rake_assign(sz, [list(tgt)] * len(old_firm), tgt, [RS['firm_age'].random() for _ in old_firm])):
                                lo, hi = AGE_BOUNDS[k]; e.age = lo + RS['firm_age'].random() * (hi - lo)
                    for e in ent: ests[e.id] = (c, e)
                    T = (m1 + m2 + m3) / 3
                    for e in live:
                        avg = (e.m1 + e.m2 + e.e) / 3
                        wp.writerow([m, y, q, c, n, e.id, e.event, e.pred, cond.out_value(e.e_prev), cond.out_value(e.m1), cond.out_value(e.m2),
                                     cond.out_value(e.e), cond.out_value(avg), cond.out_value(wages * avg / T if T > 0 else wages / len(live)),
                                     cond.out_value(e.gain), cond.out_value(e.loss), cond.out_value(e.gain + e.churn), cond.out_value(e.loss + e.churn),
                                     cond.out_value(e.churn), e.fs, age_class(e.age), round(e.age, 2)])
                    gains = sum(e.gain for e in live); losses = sum(e.loss for e in rest) + A['exit_emp']
                    # naive unlinked view: every administrative event counted as exits and entries
                    naive_g = gains + A['split_emp'] + A['merge_emp'] + A['reclass_in_emp']
                    naive_l = losses + A['split_emp'] + A['merge_emp'] + A['reclass_out_emp']
                    A.update({'entries': len(ent), 'entry_emp': se})
                    wc.writerow([m, y, q, c, n, qtype, N0 + A['reclass_out'] - A['reclass_in'], len(live), prev_m3[n], m3, base,
                                 A['reclass_in'], A['reclass_out'], A['reclass_in_emp'], A['reclass_out_emp'], A['splits'], A['split_emp'],
                                 A['merges'], A['merge_emp'], A['entries'], A['entry_emp'], A['exits'], A['exit_emp'], A['exits_to_merges'],
                                 gains, losses, C, gains + C, losses + C, g, l, ch, src, int(R_raised), naive_g, naive_l,
                                 A['temp_closings'], A['temp_closing_emp'], A['reopenings'], 'BED' if bq else 'BDS', A['deaths_swapped_to_dormant']])
                    if len(live) != N_t: log['count_mismatch'] += 1
                    exact.append((len(live) - N_t, sum(e.m1 for e in live) - m1, sum(e.m2 for e in live) - m2, sum(e.e for e in live) - m3,
                                  gains - losses - (m3 - prev_m3[n] - A['reclass_in_emp'] + A['reclass_out_emp'])))
                    # firm size and age employment shares vs QWI margins
                    for dim, tgt in (('size', fs_target(m, c, n, y, q)), ('age', fag.get((n, y, q)))):
                        if tgt and T >= 50:
                            got = collections.Counter()
                            for e in live: got[e.fs if dim == 'size' else age_class(e.age)] += (e.m1 + e.m2 + e.e) / 3
                            margin_gap[(dim, y, q)].append(sum(abs(got[k] / T - tgt[k]) for k in tgt) / 2)
                    log['cell_quarters'] += 1
    for f in (fh_p, fh_x, fh_c): f.close()
    for k, p in paths.items(): os.replace(p + '.part', p)
    log['reclass_rates'] = log_rates
    exact = [max(abs(x[i]) for x in exact) for i in range(5)]
    save_diag(a, V, log, exact, margin_gap)
    report(a, paths['cells'], exact, margin_gap, log)


def save_diag(a, V, log, exact, margin_gap):
    """Log, exactness maxima and margin gaps, so merge_versions.py can rebuild the report over all versions (F13)."""
    import json
    with gzip.open(os.path.join(a.out, f'diag_{a.tag}.json.gz'), 'wt') as fh:
        json.dump({'args': vars(a), 'versions': V, 'log': dict(log), 'exact': exact, 'run': cond.run_line(),
                   'margin_gap': [[list(k), v] for k, v in margin_gap.items()]}, fh)


def load_diag(path):
    import json
    d = json.load(gzip.open(path, 'rt'))
    d['margin_gap'] = {tuple(k): v for k, v in d['margin_gap']}
    return d


def report(a, cells_path, exact, margin_gap, log, run=None):
    """exact: max abs of (N, month1, month2, month3, gains - losses vs economic net) over cell-quarters."""
    rows = list(csv.DictReader(cond.open_text(cells_path)))
    f = lambda r, k: float(r[k])
    L = [f'Step 3b random-baseline panel ({a.tag}): {len(rows):,} cell-quarter-versions with flows; log {dict(log)}',
         'exactness (max abs): N {:.0f}, month1 {:.2e}, month2 {:.2e}, month3 {:.2e}, gains-losses vs economic net {:.2e}'.format(
             *exact)]
    L.append('\nA. Flow rates (% of QCEW base employment, sums over cells and versions)')
    L.append(f'{"":10s} {"gains":>7s} {"losses":>7s} {"churn":>7s} {"hires":>7s} {"seps":>7s} {"churn/hires":>11s} {"hires/gains":>11s} {"R raised":>8s}')
    groups = collections.defaultdict(list)
    for r in rows: groups[(r['naics6'][:4], r['qtype'])].append(r); groups[('all', r['qtype'])].append(r)
    for (ind, qt), R in sorted(groups.items()):
        b = sum(f(r, 'base') for r in R) or 1
        G, Ls, C = (sum(f(r, k) for r in R) for k in ('gains', 'losses', 'churn'))
        L.append(f'{ind + " " + qt:10s} {100 * G / b:7.1f} {100 * Ls / b:7.1f} {100 * C / b:7.1f} {100 * (G + C) / b:7.1f} {100 * (Ls + C) / b:7.1f} '
                 f'{100 * C / (G + C) if G + C else 0:10.1f}% {(G + C) / G if G else 0:11.2f} {sum(int(r["R_raised"]) for r in R) / len(R):8.1%}')
    L.append('\nB. Establishment events per 1,000 establishments per quarter (all cells, all versions)')
    L.append('  (entries / exits = births / deaths; temporary closings and reopenings keep the id and stay in the count)')
    L.append(f'{"":8s} {"entries":>8s} {"exits":>8s} {"splits":>8s} {"merges":>8s} {"exit->merge":>11s} {"reclass":>8s} {"admin share of count events":>28s}')
    for qt in ('q1_2022', 'q1', 'other'):
        R = [r for r in rows if r['qtype'] == qt]; n = sum(f(r, 'N_prev') for r in R) or 1
        ev = {k: sum(f(r, k) for r in R) for k in ('entries', 'exits', 'splits', 'merges', 'exits_to_merges', 'reclass_in', 'temp_closings', 'reopenings')}
        adm = ev['splits'] + ev['merges'] + ev['reclass_in']; tot = adm + ev['entries'] + ev['exits']
        L.append(f'{qt:8s} ' + ' '.join(f'{1000 * ev[k] / n:8.1f}' for k in ('entries', 'exits', 'splits', 'merges')) +
                 f' {1000 * ev["exits_to_merges"] / n:11.1f} {1000 * ev["reclass_in"] / n:8.1f} {adm / tot if tot else 0:27.1%}'
                 f'   temp closings {1000 * ev["temp_closings"] / n:.1f}, reopenings {1000 * ev["reopenings"] / n:.1f}')
    L.append('\nC. Danger check: flows if administrative events were read as economic (naive, unlinked) vs linked panel')
    L.append(f'{"":8s} {"gains linked":>12s} {"gains naive":>11s} {"inflation":>9s} {"losses naive/linked":>19s} {"hires naive/linked":>18s}')
    for qt in ('q1_2022', 'q1', 'other'):
        R = [r for r in rows if r['qtype'] == qt]; b = sum(f(r, 'base') for r in R) or 1
        G, NG, Ls, NL, C = (sum(f(r, k) for r in R) for k in ('gains', 'naive_gains', 'losses', 'naive_losses', 'churn'))
        L.append(f'{qt:8s} {100 * G / b:11.1f}% {100 * NG / b:10.1f}% {NG / G if G else 0:9.2f} {NL / Ls if Ls else 0:19.2f} {(NG + C) / (G + C) if G + C else 0:18.2f}')
    L.append('\nD. Firm size / age employment shares vs QWI state 6-digit margins (TVD, cells >= 50 jobs, median by quarter)')
    for dim in ('size', 'age'):
        ks = sorted(k for k in margin_gap if k[0] == dim)
        L.append(f'  {dim}: ' + ', '.join(f'{y}Q{q} {st.median(margin_gap[(d, y, q)]):.3f}' for d, y, q in ks))
    L += flows_by_class(a)
    L.append(run or cond.run_line())
    print('\n'.join(L))
    open(os.path.join(a.out, f'report_{a.tag}.txt'), 'w').write('\n'.join(L) + '\n')


def allocate_by_class(rest, ent, exits, forced, Gc, Lc, G, L, cls_rates, rng, log, age_class):
    """Step 3d v1: split the cell's continuer job gains Gc and losses Lc across groups (firm size class x young / mature)
    so that, together with entrants' gains and exits' losses, gains and losses by firm size class and by age group are
    proportional to QWI state 6-digit rates x the cell's employment in the class (IPF over the two margins). Within a
    group: grow / shrink drawn with the group's odds, amounts proportional to size x Exp(1), shrinkers capped at their
    employment. Totals Gc and Lc are exact. Sets e.gain, e.loss, e.e for continuers."""
    rsz, rag = cls_rates
    grp = [(e.fs, priors.age_group(age_class(e.age))) for e in rest]
    B = collections.Counter()
    for e, k in zip(rest, grp): B[k] += e.e + 0.5
    Ball = {'s': collections.Counter(), 'a': collections.Counter()}
    for (fs, ag), v in B.items(): Ball['s'][fs] += v; Ball['a'][ag] += v
    entG = {'s': collections.Counter(), 'a': collections.Counter()}; exL = {'s': collections.Counter(), 'a': collections.Counter()}
    for e in ent:
        entG['s'][e.fs] += e.e; entG['a'][priors.age_group(age_class(e.age))] += e.e
        Ball['s'][e.fs] += e.e / 2; Ball['a'][priors.age_group(age_class(e.age))] += e.e / 2
    for e in exits:
        exL['s'][e.fs] += e.e; exL['a'][priors.age_group(age_class(e.age))] += e.e
        Ball['s'][e.fs] += e.e; Ball['a'][priors.age_group(age_class(e.age))] += e.e

    def margin(total_cell, idx, dimkey, rates, fixed, cont_total):
        present = sorted({k[idx] for k in B})   # sorted: set order varies between runs (F4)
        w = {c: rates[c][dimkey] * Ball['s' if idx == 0 else 'a'][c] for c in rates}
        tw = sum(w.values())
        t = {c: max(total_cell * w[c] / tw - fixed[c], 0.0) if tw > 0 else 0.0 for c in present}
        st_ = sum(t.values())
        if st_ <= 0: t = {c: sum(v for k, v in B.items() if k[idx] == c) for c in present}; st_ = sum(t.values())
        return {c: cont_total * v / st_ for c, v in t.items()} if st_ > 0 else {}
    AG = priors.ipf(dict(B), margin(G, 0, 0, rsz, entG['s'], Gc), margin(G, 1, 0, rag, entG['a'], Gc)) if Gc > 1e-9 else {}
    AL = priors.ipf(dict(B), margin(L, 0, 1, rsz, exL['s'], Lc), margin(L, 1, 1, rag, exL['a'], Lc)) if Lc > 1e-9 else {}
    sg = sum(AG.values())
    AG = {k: v * Gc / sg for k, v in AG.items()} if sg > 0 else ({k: Gc * v / sum(B.values()) for k, v in B.items()} if Gc > 1e-9 else {})
    closed_k = collections.Counter()
    for i, f_ in forced.items():
        if f_ == 'close': closed_k[grp[i]] += rest[i].e
    closed = sum(closed_k.values())
    ALr = {k: max(AL.get(k, 0.0) - closed_k[k], 0.0) for k in B}
    sl = sum(ALr.values()); need = max(Lc - closed, 0.0)
    ALr = {k: v * need / sl for k, v in ALr.items()} if sl > 0 else {k: 0.0 for k in B}
    if sl <= 0 and need > 0: ALr = {k: need * B[k] / sum(B.values()) for k in B}
    members = collections.defaultdict(list)
    for i, k in enumerate(grp): members[k].append(i)
    sign = [None] * len(rest)
    for k, idx in members.items():
        g_, l_ = AG.get(k, 0.0), ALr.get(k, 0.0)
        pgk = g_ / (g_ + l_) if g_ + l_ > 0 else 0.5
        for i in idx: sign[i] = (forced[i] == 'grow') if i in forced else (rest[i].u[U_SIGN] < pgk)
        free = [i for i in idx if i not in forced]
        if g_ > 1e-9 and not any(sign[i] for i in idx) and free: sign[free[int(rng.random() * len(free))]] = True
        if l_ > 1e-9 and not any((not sign[i]) and rest[i].e > 0 for i in free):
            growers_k = [i for i in free if sign[i]]
            cand = [i for i in free if rest[i].e > 0 and (len(growers_k) > 1 or g_ <= 1e-9)]
            if cand: sign[cand[int(rng.random() * len(cand))]] = False
    mag = [max(e.e, 0.5) * -math.log(1 - e.u[U_MAG]) for e in rest]
    gain = [0.0] * len(rest); loss = [0.0] * len(rest); left_g = 0.0; left_l = 0.0
    for k, idx in members.items():
        gi = [i for i in idx if sign[i]]
        if AG.get(k, 0.0) > 0:
            if gi:
                tw = sum(mag[i] for i in gi)
                for i in gi: gain[i] = AG[k] * mag[i] / tw
            else: left_g += AG[k]
        li = [i for i in idx if not sign[i] and i not in forced]
        if ALr.get(k, 0.0) > 0:
            x, rem = bounded_split(ALr[k], [mag[i] for i in li], [rest[i].e for i in li]) if li else ([], ALr[k])
            for i, v in zip(li, x): loss[i] = v
            left_l += rem
    for i, f_ in forced.items():
        if f_ == 'close': loss[i] = rest[i].e
    if left_g > 1e-9:  # a group with gains but no grower: share over all growers (or everyone)
        gi = [i for i in range(len(rest)) if sign[i]] or list(range(len(rest)))
        tw = sum(mag[i] for i in gi)
        for i in gi: gain[i] += left_g * mag[i] / tw
        log['class_gains_spilled'] += 1
    if left_l > 1e-9:  # not enough capacity in a group: other shrinkers, then growers
        for pool_ in ([i for i in range(len(rest)) if not sign[i] and i not in forced], [i for i in range(len(rest)) if i not in forced]):
            if left_l <= 1e-9: break
            x, left_l = bounded_split(left_l, [max(rest[i].e - loss[i], 0) for i in pool_], [rest[i].e - loss[i] for i in pool_])
            for i, v in zip(pool_, x): loss[i] += v
        log['class_losses_spilled'] += 1
    for e, gg, ll in zip(rest, gain, loss):
        d = gg - ll; e.gain, e.loss = max(d, 0.0), max(-d, 0.0); e.e = e.e + d


def flows_by_class(a):
    """E. Job gains, losses and churn per job by firm age and size class: panel vs a WITHIN-CELL QWI benchmark.
    Benchmark: in each cell-quarter (version x county x 6-digit x quarter), the panel's own total gains, losses and
    churn are split across the cell's firm classes in proportion to QWI state 6-digit class rates x the cell's class
    employment; then summed over cells. Pooling cells directly would confound class with cell mix (small firms sit in
    high-flow seasonal cells; C11). Profiles are relative to the all-class rate. With --class-targets 1 the size
    classes are constraints, so this checks the fit (and the C11 residual), not a held-out test (that is step 3e)."""
    out = ['', 'E. Job flows by firm class, relative to the all-class rate: panel vs within-cell QWI benchmark (state 6-digit class rates',
           '   applied inside each cell to the panel\'s own cell totals; avoids cell-mix confounding, C11). Not a held-out test (step 3e).']
    for dim, col, fname, qdim in (('age', 'firm_age_class', 'fa', 'firmage'), ('size', 'firm_size_class', 'fs', 'firmsize')):
        look = priors.class_flow_rates(os.path.join(a.qwi, f'qwi_md_sa_{fname}_gs_n6_op_u_2021-2024.csv.gz'), qdim, coarse_age=False)
        cells = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
        with cond.open_text(os.path.join(a.out, f'panel_{a.tag}.csv.gz')) as fh:
            for r in csv.DictReader(fh):
                if r['event'] == 'initial': continue
                c = cells[(r['version'], r['year'], r['quarter'], r['naics6'])][r[col]]
                c['base'] += (float(r['emp_prev_m3'] or 0) + float(r['emp_m3'])) / 2
                c['gain'] += float(r['job_gain']); c['loss'] += float(r['job_loss']); c['churn'] += float(r['churn'])
        with cond.open_text(os.path.join(a.out, f'exits_{a.tag}.csv.gz')) as fh:
            for r in csv.DictReader(fh):
                if r['event'] == 'exit' and r.get(col):
                    c = cells[(r['version'], r['year'], r['quarter'], r['naics6'])][r[col]]
                    c['loss'] += float(r['emp_prev_m3']); c['base'] += float(r['emp_prev_m3']) / 2
        P = collections.defaultdict(collections.Counter); B = collections.defaultdict(collections.Counter); used = skipped = 0
        for (m, y, q, n), cl in cells.items():
            rates, _ = look(n, int(y), int(q))
            if not rates: skipped += 1; continue
            used += 1
            for k, c in cl.items():
                P[k]['base'] += c['base']; B[k]['base'] += c['base']
                for f in ('gain', 'loss', 'churn'): P[k][f] += c[f]
            for ix, f in enumerate(('gain', 'loss', 'churn')):
                tot = sum(c[f] for c in cl.values()); w = {k: rates[k][ix] * c['base'] for k, c in cl.items() if k in rates}
                tw = sum(w.values())
                for k in w: B[k][f] += tot * w[k] / tw if tw > 0 else 0.0
        def rel(S_, k, f):
            T = sum(S_[x][f] for x in priors.AGES); Tb = sum(S_[x]['base'] for x in priors.AGES)
            return S_[k][f] / S_[k]['base'] / (T / Tb) if S_[k]['base'] > 0 and T > 0 and Tb > 0 else float('nan')
        out.append(f'  by firm {dim}: class ' + '  '.join(f'{k:>17s}' for k in priors.AGES) + f'   (gains / losses / churn, relative; {used} cell-quarters, {skipped} without QWI class rates)')
        for name, S_ in (('panel', P), ('benchmark', B)):
            out.append(f'    {name:9s}       ' + '  '.join(f"{'/'.join(f'{rel(S_, k, f):.2f}' for f in ('gain', 'loss', 'churn')):>17s}" for k in priors.AGES))
        gaps = [(B[k]['base'], abs(rel(P, k, f) - rel(B, k, f))) for k in priors.AGES for f in ('gain', 'loss', 'churn')
                if B[k]['base'] > 0 and rel(P, k, f) == rel(P, k, f) and rel(B, k, f) == rel(B, k, f)]
        out.append(f'    employment-weighted mean |panel - benchmark| of the relative rates: {sum(w * g for w, g in gaps) / sum(w for w, _ in gaps):.3f}')
        if dim == 'size':
            k = '1'
            out.append(f'    C11 residual, firms of 0-19: panel / benchmark gains {rel(P, k, "gain") / rel(B, k, "gain"):.2f}, losses {rel(P, k, "loss") / rel(B, k, "loss"):.2f}')
    return out


if __name__ == '__main__':
    main()
