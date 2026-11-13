"""Event rates per 1,000 comparable cells per quarter, by break class and quarter group, from classify_breaks.py output.
Input of calibrate_reclass.py (its 'all breaks' column). Saved as a script 2026-10-31; first run inline on 2026-10-16.
Usage: python3 event_rates.py <breaks_dir>      (writes <breaks_dir>/event_rates.txt and prints it)"""
import csv, os, re, sys

D = sys.argv[1]
rows = list(csv.DictReader(open(os.path.join(D, 'breaks_classified.csv'))))
cells = {}
for l in open(os.path.join(D, 'breaks_summary.txt')):
    m = re.match(r'\s+(\d{4})Q(\d): cells\s+(\d+)', l)
    if m: cells[(int(m[1]), int(m[2]))] = int(m[3])
groups = {'Q1 2022': [(2022, 1)], 'Q1 2023-24': [(2023, 1), (2024, 1)], 'Q2-Q4': [k for k in cells if k[1] != 1]}
classes = ['reclassification', 'relocation', 'birth_death', 'seasonal', 'shock', 'other']
L = ['Event rates per 1,000 comparable cells per quarter (private county 6-digit, >= 20 jobs), and jobs moved per 1,000 jobs in comparable cells is not computed here',
     f'{"group":12s} {"cells":>6s} ' + ' '.join(f'{c:>16s}' for c in classes) + f' {"all breaks":>11s}']
base = {}
for g, qs in groups.items():
    n = sum(cells[q] for q in qs)
    r = [x for x in rows if (int(x['year']), int(x['quarter'])) in qs]
    rates = {c: 1000 * sum(x['class'] == c for x in r) / n for c in classes}; base[g] = rates
    L.append(f'{g:12s} {n / len(qs):6.0f} ' + ' '.join(f'{rates[c]:16.1f}' for c in classes) + f' {1000 * len(r) / n:11.1f}')
L += ['', 'Q1 excess over Q2-Q4 (per 1,000 cells):']
for g in ('Q1 2022', 'Q1 2023-24'):
    L.append(f'{g:12s} ' + ' '.join(f'{c} {base[g][c] - base["Q2-Q4"][c]:+.1f}' for c in classes))
open(os.path.join(D, 'event_rates.txt'), 'w').write('\n'.join(L) + '\n'); print('\n'.join(L))
