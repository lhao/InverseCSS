"""Compare QWI with QCEW, private sector, for every quarter in a QWI extract.

Usage:
  python3 compare_qwi_qcew.py <qwi_sa_f_file.csv.gz> <qcew_root> <state|county> <out_dir> [<qcew_complete_dir>]
  qcew_complete_dir: NAICS-2022 converted quarters (convert_qcew_naics.py); their 'published' cells are used
  state  : QWI geo_level S, 6-digit  vs QCEW 24000 agglvl 58
  county : QWI geo_level C, 4-digit  vs QCEW county files agglvl 76
Uses QWI rows for all sexes and all ages (sex 0, agegrp A00), ownership A05 (private) = QCEW own 5.
Pairs: Emp / QCEW month 3 of the previous quarter (start of quarter), Emp / month1,
EmpEnd / month3, 3*EarnBeg*Emp / total_qtrly_wages.
Writes one row per cell-quarter, plus a summary printed per quarter and by size class.
"""
import csv, gzip, os, statistics as st, sys, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond   # shared handling of known data conditions (docs/DATA_CONDITIONS.md)

qwi_path, root, level, out_dir = sys.argv[1:5]
complete_dir = sys.argv[5] if len(sys.argv) > 5 else ''      # NAICS-2022 converted quarters (C1), optional
geo, ind_lvl, agg = ('S', '6', '58') if level == 'state' else ('C', '4', '76')

qwi = {(y, q, area, ind): r for (area, ind, y, q), r in cond.load_qwi(qwi_path, geo, ind_lvl).items()}


def qcew(y, q):
    """Published private QCEW cells {(area, industry): row} at the comparison level. For quarters converted to
    NAICS 2022 (complete_dir), version 1 of the complete file, cells with status 'published' only."""
    cp = os.path.join(complete_dir, f'qcew_complete_{y}q{q}_M20.csv.gz') if complete_dir else ''
    out = {}
    if cp and os.path.exists(cp):
        lv = '6' if level == 'state' else '4'
        with cond.open_text(cp) as fh:
            for r in csv.DictReader(fh):
                if r['draw'] == '1' and r['own_code'] == '5' and r['level'] == lv and (r['area_fips'] == '24000') == (level == 'state'):
                    out[(r['area_fips'], r['industry_code'])] = dict(r, disclosure_code='' if r['status'] == 'published' else 'N')
        return out
    d = os.path.join(root, f'{y}q{q}')
    if not os.path.isdir(d): return {}
    files = [os.path.join(d, '24000.csv')] if level == 'state' else cond.area_files(d)
    for f in files:
        with cond.open_text(f) as fh:
            for r in csv.DictReader(fh):
                if r['agglvl_code'] == agg and r['own_code'] == '5':
                    out[(r['area_fips'], r['industry_code'])] = r
    return out


def ok(r, v, flag):
    return r[v] not in ('',) and r[flag] in ('1', '9')


quarters = sorted({(y, q) for y, q, _, _ in qwi})
rows, cache = [], {}
for y, q in quarters:
    py, pq = (y, q - 1) if q > 1 else (y - 1, 4)
    cur = cache.setdefault((y, q), qcew(y, q)); prev = cache.setdefault((py, pq), qcew(py, pq))
    for (yy, qq, area, ind), a in qwi.items():
        if (yy, qq) != (y, q) or (area, ind) not in cur: continue
        b = cur[(area, ind)]; p = prev.get((area, ind))
        pub = b['disclosure_code'] == ''
        row = {'year': y, 'quarter': q, 'area_fips': area, 'industry_code': ind, 'qcew_disclosure': b['disclosure_code'],
               'qcew_estabs': b['qtrly_estabs'], 'qcew_month1': b['month1_emplvl'] if pub else '', 'qwi_Emp': a['Emp'], 'qwi_sEmp': a['sEmp']}
        def ratio(num, den):
            return round(num / den, 4) if den else ''
        if pub and ok(a, 'Emp', 'sEmp'):
            row['r_Emp_month1'] = ratio(float(a['Emp']), float(b['month1_emplvl']))
            if p and p['disclosure_code'] == '': row['r_Emp_prevmonth3'] = ratio(float(a['Emp']), float(p['month3_emplvl']))
        if pub and ok(a, 'EmpEnd', 'sEmpEnd'): row['r_EmpEnd_month3'] = ratio(float(a['EmpEnd']), float(b['month3_emplvl']))
        if pub and ok(a, 'EarnBeg', 'sEarnBeg') and ok(a, 'Emp', 'sEmp'):
            row['r_derivedPayroll_wages'] = ratio(3 * float(a['EarnBeg']) * float(a['Emp']), float(b['total_qtrly_wages']))
        rows.append(row)

os.makedirs(out_dir, exist_ok=True)
cols = ['year', 'quarter', 'area_fips', 'industry_code', 'qcew_disclosure', 'qcew_estabs', 'qcew_month1', 'qwi_Emp', 'qwi_sEmp',
        'r_Emp_prevmonth3', 'r_Emp_month1', 'r_EmpEnd_month3', 'r_derivedPayroll_wages']
out = os.path.join(out_dir, f'qwi_vs_qcew_md_{level}_{quarters[0][0]}-{quarters[-1][0]}_private.csv')
with open(out, 'w', newline='') as fh:
    w = csv.DictWriter(fh, cols, restval=''); w.writeheader(); w.writerows(rows)
print('wrote', out, len(rows), 'cell-quarters')


def summ(v):
    v = sorted(v); n = len(v)
    return f'n={n:5d} median={st.median(v):.3f} mid50%=[{v[int(.25*n)]:.3f},{v[int(.75*n)]:.3f}] mid80%=[{v[int(.1*n)]:.3f},{v[min(n-1,int(.9*n))]:.3f}]' if n else 'n=0'

print(f'\n{level}: ratio QWI/QCEW by quarter (QWI Emp vs QCEW month 3 of previous quarter; derived payroll vs wages)')
for y, q in quarters:
    rs = [r for r in rows if (r['year'], r['quarter']) == (y, q)]
    print(f' {y}Q{q} Emp/prev-m3 {summ([r["r_Emp_prevmonth3"] for r in rs if r.get("r_Emp_prevmonth3", "") != ""])} | '
          f'payroll {summ([r["r_derivedPayroll_wages"] for r in rs if r.get("r_derivedPayroll_wages", "") != ""])}')
print(f'\n{level}: Emp/prev-m3 by QCEW size (all quarters)')
for lo, hi in ((0, 50), (50, 250), (250, 1000), (1000, 5000), (5000, 1e12)):
    v = [r['r_Emp_prevmonth3'] for r in rows if r.get('r_Emp_prevmonth3', '') != '' and lo <= float(r['qcew_month1']) < hi]
    print(f'  [{lo},{hi:g}) {summ(v)}')
sup = [r for r in rows if r['qcew_disclosure'] == 'N']
print(f'\nQCEW-suppressed {level} private cells (all quarters): {len(sup)}; QWI Emp published for {sum(r["qwi_sEmp"] in ("1", "9") and r["qwi_Emp"] != "" for r in sup)}')
