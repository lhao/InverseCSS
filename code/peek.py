"""Look inside a data file without unpacking or changing it.

Reads .csv or .csv.gz directly (the file stays as it is), and prints:
  - the column names, numbered
  - the first N rows as a table
  - with --count: the number of rows, and for up to --cols columns the distinct values (if few) or min / max

Usage:
  python3 code/peek.py <file> [N]                      first N rows (default 20)
  python3 code/peek.py <file> [N] --count              also count rows and summarise columns (reads the whole file)
  python3 code/peek.py <file> [N] --where col=value    show only rows where a column equals a value
Examples:
  python3 code/peek.py data/processed/qcew_imputed/qcew_imputed_2024q4_M20.csv.gz 10
  python3 code/peek.py data/raw/qwi/R2026Q3/qwi_md_sa_f_gc_n4_op_u_2021-2024.csv.gz 15 --where geography=24047
"""
import argparse, csv, gzip, io, sys


def open_any(path):
    if path.endswith('.gz'):
        return io.TextIOWrapper(gzip.open(path, 'rb'), encoding='utf-8', newline='')
    return open(path, newline='', encoding='utf-8')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('file'); ap.add_argument('n', nargs='?', type=int, default=20)
    ap.add_argument('--count', action='store_true'); ap.add_argument('--where', default='')
    ap.add_argument('--cols', type=int, default=40, help='max columns to summarise with --count')
    ap.add_argument('--width', type=int, default=14, help='max characters per cell in the table')
    a = ap.parse_args()
    cond = tuple(a.where.split('=', 1)) if a.where else None

    with open_any(a.file) as fh:
        rd = csv.reader(fh)
        header = next(rd)
        print(f'{a.file}\n{len(header)} columns:')
        for i in range(0, len(header), 6):
            print('  ' + '  '.join(f'{j + 1:>3}:{header[j]}' for j in range(i, min(i + 6, len(header)))))
        ci = header.index(cond[0]) if cond else None
        shown, total, summ = [], 0, [dict(vals=set(), lo=None, hi=None, num=True) for _ in header[:a.cols]]
        for row in rd:
            if cond and row[ci] != cond[1]: continue
            total += 1
            if len(shown) < a.n: shown.append(row)
            elif not a.count: break
            if a.count:
                for s, v in zip(summ, row):
                    if len(s['vals']) <= 20: s['vals'].add(v)
                    try:
                        x = float(v)
                        s['lo'] = x if s['lo'] is None else min(s['lo'], x); s['hi'] = x if s['hi'] is None else max(s['hi'], x)
                    except ValueError:
                        if v != '': s['num'] = False

    w = a.width
    cut = lambda s: s if len(s) <= w else s[:w - 1] + '~'
    widths = [min(max([len(h)] + [len(r[i]) for r in shown if i < len(r)]), w) for i, h in enumerate(header)]
    print(f'\nfirst {len(shown)} rows' + (f' where {cond[0]} = {cond[1]}' if cond else '') + ':')
    print('  '.join(cut(h).ljust(wd) for h, wd in zip(header, widths)))
    for r in shown:
        print('  '.join(cut(v).ljust(wd) for v, wd in zip(r, widths)))
    if a.count:
        print(f'\nrows' + (f' where {cond[0]} = {cond[1]}' if cond else '') + f': {total:,}')
        print(f'column summary (first {min(a.cols, len(header))} columns):')
        for h, s in zip(header, summ):
            if len(s['vals']) <= 20:
                print(f'  {h:24s} {len(s["vals"])} distinct: {", ".join(sorted(s["vals"]))[:100]}')
            elif s['num'] and s['lo'] is not None:
                print(f'  {h:24s} min {s["lo"]:g}  max {s["hi"]:g}')
            else:
                print(f'  {h:24s} many distinct values')


if __name__ == '__main__':
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
