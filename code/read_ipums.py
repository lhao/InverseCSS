"""Read an IPUMS fixed-width extract (.dat.gz) using its DDI codebook (.xml) and save it as Parquet.

Outputs (in <out_dir>):
  <name>.parquet           all records, all variables (numeric: integers, implied decimals applied;
                           character variables such as OCCSOC and INDNAICS kept as text)
  <name>_labels.json       {variable: {"label": ..., "values": {code: label}}} from the codebook
  <name>_summary.txt       records, variables, samples (YEAR x MULTYEAR or YEAR x ASECFLAG)

Usage: python3 read_ipums.py <extract.xml> <extract.dat.gz> <out_dir> <name>
"""
import json, os, sys, xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conditions as cond
import pandas as pd


def layout(xml_path):
    root = ET.parse(cond.require(xml_path)).getroot()
    ns = {'d': root.tag.split('}')[0].strip('{')}
    cols, labels = [], {}
    for v in root.findall('.//d:var', ns):
        loc = v.find('d:location', ns)
        name = v.get('name')
        dec = int(v.get('dcml') or 0)
        fmt = v.find('d:varFormat', ns)
        is_char = fmt is not None and fmt.get('type') == 'character'      # e.g. OCCSOC, INDNAICS: keep as text
        cols.append((name, int(loc.get('StartPos')) - 1, int(loc.get('EndPos')), None if is_char else dec))
        lab = v.find('d:labl', ns)
        cats = {}
        for c in v.findall('d:catgry', ns):
            val, cl = c.find('d:catValu', ns), c.find('d:labl', ns)
            if val is not None and val.text is not None:
                cats[val.text.strip()] = (cl.text or '').strip() if cl is not None else ''
        labels[name] = {'label': lab.text.strip() if lab is not None and lab.text else '', 'values': cats}
    return cols, labels


def main():
    xml_path, dat_path, out_dir, name = sys.argv[1:5]
    cols, labels = layout(xml_path)
    df = pd.read_fwf(cond.require(dat_path), colspecs=[(s, e) for _, s, e, _ in cols], names=[n for n, _, _, _ in cols],
                     header=None, dtype=str, compression='gzip')
    for n, _, _, dec in cols:
        if dec is None:                       # character variable: keep the code exactly as published
            df[n] = df[n].str.strip(); continue
        x = pd.to_numeric(df[n], errors='coerce')
        df[n] = x / (10 ** dec) if dec else x.astype('Int64')
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, name + '.parquet.part')
    df.to_parquet(tmp, index=False)
    os.replace(tmp, os.path.join(out_dir, name + '.parquet'))          # written in one step (F8)
    json.dump(labels, open(os.path.join(out_dir, name + '_labels.json'), 'w'), indent=0)
    keys = [k for k in ('YEAR', 'MULTYEAR', 'ASECFLAG') if k in df]
    summ = [f'{name}: {len(df):,} records, {df.shape[1]} variables', 'records by ' + ' x '.join(keys) + ':',
            df.groupby(keys, dropna=False).size().to_string()]
    open(os.path.join(out_dir, name + '_summary.txt'), 'w').write('\n'.join(summ) + '\n')
    print('\n'.join(summ))


if __name__ == '__main__':
    main()
