"""Compare QWI and QCEW for Maryland, state x NAICS 6-digit, private sector, 2022 Q2.

Usage: python3 compare_qwi_qcew_state6d.py <qwi_extract.csv> <qcew_24000.csv> <out_dir>
qwi_extract: rows of qwi_md_sa_f_gs_n6_op_u (all sexes, all ages) for 2022.
Pairs compared (QWI / QCEW):
  Emp     (beginning-of-quarter jobs) / month1_emplvl (April)
  EmpEnd  (end-of-quarter jobs)       / month3_emplvl (June)
  Payroll (total quarterly earnings)  / total_qtrly_wages   (QWI suppresses Payroll at state 6d)
  3*EarnBeg*Emp (derived payroll of beginning-of-quarter jobs) / total_qtrly_wages
"""
import csv, sys, statistics as st, collections

qwi_path, qcew_path, out_dir = sys.argv[1:4]
q = {}
for r in csv.DictReader(open(qwi_path)):
    if r['year'] == '2022' and r['quarter'] == '2' and r['sex'] == '0' and r['agegrp'] == 'A00':
        q[r['industry']] = r
c = {r['industry_code']: r for r in csv.DictReader(open(qcew_path))
     if r['agglvl_code'] == '58' and r['own_code'] == '5'}

# NAICS vintage check: codes that exist only in NAICS 2017 vs only in NAICS 2022
only17 = {'452210', '452311', '452319', '454110', '511110', '511120', '511130', '517311', '517312', '519130'}
only22 = {'455110', '455211', '455219', '513110', '513120', '513130', '517111', '517112', '519290', '516210'}
print('QWI codes, NAICS2017-only present:', sorted(only17 & set(q)), '| NAICS2022-only present:', sorted(only22 & set(q)))
print('QCEW codes, NAICS2017-only present:', sorted(only17 & set(c)), '| NAICS2022-only present:', sorted(only22 & set(c)))
print('6d codes: QWI', len(q), 'QCEW', len(c), 'both', len(set(q) & set(c)),
      'QWI only', len(set(q) - set(c)), 'QCEW only', len(set(c) - set(q)))

pairs = [('Emp', 'sEmp', 'month1_emplvl'), ('EmpEnd', 'sEmpEnd', 'month3_emplvl'), ('Payroll', 'sPayroll', 'total_qtrly_wages')]
flagcount = {p[0]: collections.Counter(r[p[1]] for r in q.values()) for p in pairs}
print('QWI status flags 2022Q2:', {k: dict(v) for k, v in flagcount.items()})
print('QCEW disclosure 2022Q2:', dict(collections.Counter(r['disclosure_code'] for r in c.values())))

rows = []
for ind in sorted(set(q) & set(c)):
    a, b = q[ind], c[ind]
    row = {'industry_code': ind, 'qcew_estabs': b['qtrly_estabs'], 'qcew_disclosure': b['disclosure_code']}
    for qv, qf, cv in pairs:
        row[qv] = a[qv]; row[qf] = a[qf]; row[cv] = b[cv] if b['disclosure_code'] == '' else ''
        ok = a[qv] not in ('',) and a[qf] in ('1', '9') and row[cv] not in ('', '0')
        row['ratio_' + qv] = round(float(a[qv]) / float(row[cv]), 4) if ok else ''
    ok = a['EarnBeg'] != '' and a['Emp'] != '' and a['sEarnBeg'] in ('1', '9') and a['sEmp'] in ('1', '9') and row['total_qtrly_wages'] not in ('', '0')
    row['EarnBeg'] = a['EarnBeg']; row['sEarnBeg'] = a['sEarnBeg']
    row['ratio_derivedPayroll'] = round(3 * float(a['EarnBeg']) * float(a['Emp']) / float(row['total_qtrly_wages']), 4) if ok else ''
    rows.append(row)
with open(out_dir + '/qwi_vs_qcew_md_2022q2_state6d_private.csv', 'w', newline='') as fh:
    w = csv.DictWriter(fh, list(rows[0])); w.writeheader(); w.writerows(rows)

def summ(vals, wts=None):
    vals = sorted(vals); n = len(vals)
    pct = lambda p: vals[min(n - 1, int(p * n))]
    return f'n={n} median={st.median(vals):.3f} p10={pct(.1):.3f} p25={pct(.25):.3f} p75={pct(.75):.3f} p90={pct(.9):.3f}'

print()
for qv, qf, cv in pairs:
    for flag in ('1', '9'):
        v = [r['ratio_' + qv] for r in rows if r['ratio_' + qv] != '' and r[qf] == flag]
        if v: print(f'{qv}/{cv} [QWI flag {flag}]:', summ(v))
    if not any(r['ratio_' + qv] != '' for r in rows):
        print(f'{qv}: no QWI values published at this level'); continue
    tq = sum(float(r[qv]) for r in rows if r['ratio_' + qv] != '')
    tc = sum(float(r[cv]) for r in rows if r['ratio_' + qv] != '')
    print(f'  aggregate over matched cells: QWI {tq:,.0f} / QCEW {tc:,.0f} = {tq / tc:.4f}')
v = [r['ratio_derivedPayroll'] for r in rows if r['ratio_derivedPayroll'] != '']
print('3*EarnBeg*Emp / total_qtrly_wages:', summ(v))
# how ratio dispersion varies with size
print()
for lo, hi in ((0, 50), (50, 250), (250, 1000), (1000, 5000), (5000, 1e9)):
    v = [r['ratio_Emp'] for r in rows if r['ratio_Emp'] != '' and lo <= float(r['month1_emplvl']) < hi]
    if len(v) > 4: print(f'Emp/April by QCEW size [{lo},{hi}):', summ(v))
# QCEW-suppressed cells where QWI publishes a value
qs = [r for r in rows if r['qcew_disclosure'] == 'N']
print('\nQCEW-suppressed state 6d cells:', len(qs), '| of which QWI Emp published (flag 1/9):',
      sum(r['sEmp'] in ('1', '9') and r['Emp'] != '' for r in qs))
