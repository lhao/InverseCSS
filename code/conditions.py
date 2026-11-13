"""Shared handling of known data conditions (see docs/DATA_CONDITIONS.md).

Every script imports this module, so a fix made here applies everywhere. Each function names the register
entry it implements. Run `python3 code/conditions.py` to execute the self-tests.
"""
import csv, gzip, hashlib, io, math, os, random, re, shutil, subprocess, sys

# ---------------------------------------------------------------- F1, F5: files
QUARTER_DIR = re.compile(r'^(\d{4})q([1-4])$')
MD_AREA_FILE = re.compile(r'^24\d{3}\.csv$')


def quarter_dirs(root):
    """[(year, qtr, path)] for folders named like 2022q2; ignores .DS_Store and anything else (F1)."""
    out = []
    for name in sorted(os.listdir(root)):
        m = QUARTER_DIR.match(name)
        if m and os.path.isdir(os.path.join(root, name)):
            out.append((int(m.group(1)), int(m.group(2)), os.path.join(root, name)))
    return out


def area_files(qdir, include_state=False):
    """QCEW area files in a quarter folder (24xxx.csv); the statewide 24000 only if asked (F1)."""
    return [os.path.join(qdir, f) for f in sorted(os.listdir(qdir))
            if MD_AREA_FILE.match(f) and (include_state or f != '24000.csv')]


def require(path):
    """Fail loudly if an expected input is missing; explain the common cause (F5)."""
    if os.path.exists(path):
        return path
    hint = ''
    if path.endswith('.gz') and os.path.exists(path[:-3]):
        hint = f' -- an uncompressed copy {os.path.basename(path[:-3])} exists; gzip it (do not unpack inside data/)'
    raise SystemExit(f'missing input: {path}{hint}')


def open_text(path):
    """Open .csv or .csv.gz for reading as text."""
    require(path)
    return io.TextIOWrapper(gzip.open(path, 'rb'), encoding='utf-8', newline='') if path.endswith('.gz') \
        else open(path, newline='', encoding='utf-8')


# ---------------------------------------------------------------- A: status codes
QCEW_STATUS = {'': 'published', 'N': 'suppressed', '-': 'true_zero'}                 # A1, A2
QWI_OK_FLAGS = ('1', '9')                                                             # A5: usable values
QWI_STATUS = {'1': 'published', '9': 'noise_distorted', '5': 'suppressed', '-1': 'not_available', '-2': 'no_data',
              '6': 'derived', '7': 'derived_distorted', '10': 'aggregate', '11': 'aggregate_not_released',
              '12': 'aggregate_distorted'}


def qcew_status(r):
    return QCEW_STATUS.get(r['disclosure_code'], 'unknown')


def qcew_value(r, var):
    """QCEW value, or None if suppressed (A1: BLS writes 0 for suppressed cells; never read it as 0)."""
    s = qcew_status(r)
    if s == 'suppressed': return None
    if s == 'true_zero': return 0.0
    return float(r[var])


def qwi_value(r, var):
    """QWI value if its status flag marks it usable, else None (A5)."""
    return float(r[var]) if r.get(var, '') != '' and r.get('s' + var) in QWI_OK_FLAGS else None


# ---------------------------------------------------------------- QCEW levels and QWI rows
COUNTY_AGGLVL = {'70': 0, '71': 1, '74': 2, '75': 3, '76': 4, '77': 5, '78': 6}
STATE_AGGLVL = {'50': 0, '51': 1, '54': 2, '55': 3, '56': 4, '57': 5, '58': 6}
SECTOR = {'31': '31-33', '32': '31-33', '33': '31-33', '44': '44-45', '45': '44-45', '48': '48-49', '49': '48-49'}


def county_agglvl(level):
    """QCEW county aggregation code for an industry level (2..6 digits)."""
    return str(74 + int(level) - 2)


def parent_industry(code, level):
    """Industry one level up: 6->5, 5->4, 4->3, 3->sector (31-33 etc.), sector->'10' (total)."""
    level = int(level)
    if level == 2: return '10'
    if level == 3: return SECTOR.get(code[:2], code[:2])
    return code[:level - 1]


def qwi_area(geo_level, geography):
    """QWI geography -> QCEW area_fips (D4): state '24' -> '24000'; county kept as 5 digits."""
    return geography.zfill(2) + '000' if geo_level == 'S' else geography.zfill(5)


def is_qwi_total(r, tab='sa'):
    """All-worker row of a QWI demographic file."""
    return {'sa': r['sex'] == '0' and r['agegrp'] == 'A00', 'se': r['sex'] == '0' and r['education'] == 'E0',
            'rh': r['race'] == 'A0' and r['ethnicity'] == 'A0'}[tab]


def load_qwi(path, geo, ind_level, ownercode='A05', tab='sa', firmage='0', firmsize='0'):
    """All-worker rows at one geography level and industry level: {(area_fips, industry, year, qtr): row}.
    ind_level: 'A' (all), 'S' (sector), '3', '4', '5', '6'."""
    out = {}
    with open_text(path) as fh:
        for r in csv.DictReader(fh):
            if (r['geo_level'] == geo and r['ind_level'] == ind_level and r['ownercode'] == ownercode and is_qwi_total(r, tab)
                    and r.get('firmage', '0') == firmage and r.get('firmsize', '0') == firmsize):
                out[(qwi_area(geo, r['geography']), r['industry'], int(r['year']), int(r['quarter']))] = r
    return out


# ---------------------------------------------------------------- B1: QWI identities within +-2
FLOW_CORE = ('Emp', 'EmpEnd', 'EmpS', 'HirA', 'Sep', 'FrmJbGn', 'FrmJbLs')


def make_consistent(pv):
    """Adjust a QWI cell (dict) minimally so the identities hold exactly (B1). Returns the total adjustment.
    FrmJbC = FrmJbGn - FrmJbLs = EmpEnd - Emp = HirA - Sep = HirAEnd - SepBeg; HirA = HirN + HirR;
    FrmJbGn >= max(FrmJbC, 0); HirA >= FrmJbGn; FrmJbGn <= HirAEnd <= min(HirA, EmpEnd); EmpS <= min(Emp, EmpEnd)."""
    C = pv['EmpEnd'] - pv['Emp']
    adj = 0.0
    if pv['FrmJbGn'] < max(C, 0): adj += max(C, 0) - pv['FrmJbGn']; pv['FrmJbGn'] = max(C, 0)
    if pv['HirA'] < pv['FrmJbGn']: adj += pv['FrmJbGn'] - pv['HirA']; pv['HirA'] = pv['FrmJbGn']
    pv['FrmJbLs'] = pv['FrmJbGn'] - C
    pv['Sep'] = pv['HirA'] - C
    pv['FrmJbC'] = C
    if pv.get('EmpS') is not None and pv['EmpS'] > min(pv['Emp'], pv['EmpEnd']):
        adj += pv['EmpS'] - min(pv['Emp'], pv['EmpEnd']); pv['EmpS'] = min(pv['Emp'], pv['EmpEnd'])
    if pv.get('HirN') is not None and pv.get('HirR') is not None:
        pv['HirN'] = min(max(pv['HirN'], 0.0), pv['HirA']); pv['HirR'] = pv['HirA'] - pv['HirN']
    if pv.get('HirAEnd') is not None and pv.get('SepBeg') is not None:
        # HirAEnd >= FrmJbGn (so SepBeg >= FrmJbLs): holds in 100% of published county 4-digit cells (B10)
        pv['HirAEnd'] = min(max(pv['HirAEnd'], pv['FrmJbGn']), pv['HirA'], max(pv['EmpEnd'], pv['FrmJbGn'])); pv['SepBeg'] = pv['HirAEnd'] - C
    return adj


def identity_violations(d, tol=1e-6):
    """Number of identity violations in one record (R4)."""
    bad = abs(d['FrmJbGn'] - d['FrmJbLs'] - d['FrmJbC']) > tol or abs(d['HirA'] - d['Sep'] - d['FrmJbC']) > tol \
          or abs(d['EmpEnd'] - d['Emp'] - d['FrmJbC']) > tol
    if d.get('HirAEnd') not in (None, '') and d.get('SepBeg') not in (None, ''):
        bad = bad or abs(d['HirAEnd'] - d['SepBeg'] - d['FrmJbC']) > tol
    if d.get('HirN') not in (None, '') and d.get('HirR') not in (None, ''):
        bad = bad or abs(d['HirN'] + d['HirR'] - d['HirA']) > tol
    return int(bad)


# Additivity across the industry hierarchy (B8, B9): which QWI variables add up from children to parent
ADDITIVE_HARD = ('Emp', 'EmpEnd', 'EmpS', 'HirA', 'HirN', 'HirR', 'Sep', 'HirAEnd', 'SepBeg')   # within +-2
ADDITIVE_SOFT = ('FrmJbGn', 'FrmJbLs')   # children sum >= parent by a few jobs (gains per employer within a cell)

# ---------------------------------------------------------------- B5: QWI noise vs QCEW, by size
SIGMA_STOCK = ((20, 0.410), (50, 0.286), (100, 0.265), (250, 0.206), (1000, 0.176), (5000, 0.123), (float('inf'), 0.111))
SIGMA_STATE = ((50, 0.19), (250, 0.12), (1000, 0.09), (5000, 0.075), (float('inf'), 0.055))


def sigma(table, size):
    return next(s for hi, s in table if size < hi)


# ---------------------------------------------------------------- C2: reclassification breaks
RECLASS_RATIO = 1.5


def is_break(m1, prev_m3, ratio=RECLASS_RATIO):
    """True if this quarter's month 1 and the previous quarter's month 3 differ by more than `ratio`x (C2)."""
    if prev_m3 is None or prev_m3 <= 0 or m1 is None or m1 <= 0: return prev_m3 not in (None, 0) or m1 not in (None, 0)
    return not (1 / ratio <= m1 / prev_m3 <= ratio)


START_RULES = ('hybrid', 'prev', 'm1', 'min')


def start_weight(m1, prev_m3, ratio=RECLASS_RATIO, rule='hybrid'):
    """Start-of-quarter employment weight (E1, C2). Returns (weight, break_flag).
    rule 'hybrid': previous month 3 unless it differs from this month 1 by more than `ratio`x, then month 1;
         'prev'  : previous month 3 (fallback month 1);  'm1': this quarter's month 1;
         'min'   : min(previous month 3, month 1) -- jobs present on both sides of the quarter boundary, the concept
                   closest to QWI's beginning-of-quarter employment; 0 for an establishment born this quarter."""
    brk = prev_m3 is not None and m1 is not None and is_break(m1, prev_m3, ratio) and (m1 > 0 or prev_m3 > 0)
    m1v = m1 if m1 is not None else 0.0
    if rule == 'm1': return m1v, brk
    if rule == 'prev': return (prev_m3 if prev_m3 is not None and prev_m3 > 0 else m1v), brk
    if rule == 'min': return (min(prev_m3, m1v) if prev_m3 is not None else m1v), brk
    if prev_m3 is not None and prev_m3 > 0 and m1v > 0 and not brk:
        return prev_m3, False
    return m1v, brk


# ---------------------------------------------------------------- F4: reproducible random streams
def rng_for(*parts):
    """Random generator seeded from a string, reproducible across runs (never use hash(); F4)."""
    return random.Random('|'.join(str(p) for p in parts))


def uniforms(k, *parts):
    """k (<= 8) uniforms in [0, 1) keyed by `parts` (F4). Fixed count per key, so one unit's draws never depend on how
    many draws other units used: common random numbers across priors and arms (ANALYSIS_STRATEGY S4a)."""
    d = hashlib.blake2b('|'.join(str(p) for p in parts).encode(), digest_size=8 * k).digest()
    return [int.from_bytes(d[i:i + 8], 'big') / 2 ** 64 for i in range(0, 8 * k, 8)]


# ---------------------------------------------------------------- F6: output values
def out_value(x, digits=2):
    """Rounded value for output; '' for missing; turns -0.0 into 0.0 (F6)."""
    if x is None or x == '': return ''
    return round(x, digits) + 0.0


def earnings_out(earn, stable_jobs, digits=2):
    """Average earnings are undefined without stable jobs (F6)."""
    return '' if stable_jobs is None or stable_jobs < 0.005 else out_value(earn, digits)


# ---------------------------------------------------------------- F11: Mac-safe runs
MIN_FREE_GB = float(os.environ.get('ICSS_MIN_FREE_GB', 50))   # full-size runs
TEST_MIN_FREE_GB = 5   # test runs (any output under students/): the Step 6 chain writes ~25 MB
SF_DATALESS = 0x40000000   # macOS file flag: content not downloaded (cloud-only)


def keep_awake():
    """Keep the Mac awake while this process runs (F11): `caffeinate -i -w <pid>` ends by itself when the process
    ends. No system setting changes; closing the lid still sleeps the Mac (runs pause and resume). No-op elsewhere."""
    if sys.platform != 'darwin' or not shutil.which('caffeinate'): return None
    return subprocess.Popen(['caffeinate', '-i', '-w', str(os.getpid())])


def preflight(*inputs, out=None, min_free_gb=MIN_FREE_GB):
    """Stop before a run (F5, F11) if free disk space is below `min_free_gb` or an input file is cloud-only."""
    # Measure the local disk too: on Windows, Google Drive is a virtual drive (G:) whose "free space" is the Drive
    # account quota, not the computer's disk. Take the larger of the output folder's volume and the home folder's disk.
    paths = [q for q in (out, os.path.expanduser('~')) if q and os.path.exists(q)]
    free = max(shutil.disk_usage(q).free for q in paths) / 2 ** 30
    if free < min_free_gb:
        sys.exit(f'preflight: only {free:.0f} GB free on disk (need {min_free_gb:g}); stopping to keep the computer usable '
                 f'[measured: ' + '; '.join(f'{q} {shutil.disk_usage(q).free / 2 ** 30:.1f} GB' for q in paths) + ']')
    cloud = []
    for p in inputs:
        files = [os.path.join(d, f) for d, _, fs in os.walk(p) for f in fs] if os.path.isdir(p) else [p]
        cloud += [f for f in files if os.path.exists(f) and getattr(os.stat(f), 'st_flags', 0) & SF_DATALESS]
    if cloud: sys.exit(f'preflight: {len(cloud)} input files are cloud-only (e.g. {cloud[0]}); wait for Drive to download them')
    return free


def is_test_run(argv=None):
    """A run that writes under a students/ folder (HOW_TO_TEST_RUN) is a test: small outputs, lower disk minimum."""
    return any('students/' in a.replace('\\', '/') for a in (sys.argv[1:] if argv is None else argv))


def start_run(*inputs, out=None):
    """Standard start of a long script: preflight checks, then keep the Mac awake for the run (F11)."""
    need = min(MIN_FREE_GB, TEST_MIN_FREE_GB) if is_test_run() else MIN_FREE_GB
    free = preflight(*inputs, out=out or os.getcwd(), min_free_gb=need)
    keep_awake()
    print(f'preflight ok: {free:.0f} GB free (need {need:g})' + ('; Mac kept awake during the run' if sys.platform == 'darwin' else ''), file=sys.stderr)


# ---------------------------------------------------------------- F13: cluster runs (versions per job, run record)
def parse_versions(spec, draws):
    """Versions to build: '' = 1..draws; 'A-B' or 'A' = that range (one Slurm array task per version, F13).
    Each version has its own random streams (rng_for keyed by version), so a version built alone equals the same
    version built with the others."""
    if not spec: return list(range(1, draws + 1))
    lo, _, hi = spec.partition('-')
    v = list(range(int(lo), int(hi or lo) + 1))
    if not v or v[0] < 1 or v[-1] > 20: sys.exit(f'--versions {spec}: must lie within 1-20')
    return v


def versions_label(versions):
    v = sorted(versions)
    return f'{v[0]}..{v[-1]}' if v == list(range(v[0], v[-1] + 1)) else ','.join(map(str, v))


def run_line():
    """One line for every report: the exact command, host, Slurm job and time (F13)."""
    import shlex, socket, time
    job = os.environ.get('SLURM_JOB_ID', '')
    if os.environ.get('SLURM_ARRAY_TASK_ID'): job = f"{os.environ.get('SLURM_ARRAY_JOB_ID', job)}_{os.environ['SLURM_ARRAY_TASK_ID']}"
    return (f'run: {shlex.join([os.path.basename(sys.executable)] + sys.argv)} | host {socket.gethostname()}'
            + (f' | slurm job {job}' if job else '') + f' | {time.strftime("%Y-%m-%d %H:%M:%S")}')


# ---------------------------------------------------------------- self-tests
def _tests():
    # A1/A2
    assert qcew_value({'disclosure_code': 'N', 'x': '0'}, 'x') is None
    assert qcew_value({'disclosure_code': '-', 'x': '0'}, 'x') == 0.0
    assert qcew_value({'disclosure_code': '', 'x': '12'}, 'x') == 12.0
    # A5
    assert qwi_value({'Emp': '5', 'sEmp': '5'}, 'Emp') is None and qwi_value({'Emp': '5', 'sEmp': '1'}, 'Emp') == 5.0
    # industry hierarchy
    assert parent_industry('722513', 6) == '72251' and parent_industry('311', 3) == '31-33' and parent_industry('31-33', 2) == '10'
    assert county_agglvl(4) == '76' and county_agglvl(6) == '78'
    # D4
    assert qwi_area('S', '24') == '24000' and qwi_area('C', '24047') == '24047'
    # B1: net change 5 but published gains 4 -> gains raised to 5, losses 0
    pv = {'Emp': 100.0, 'EmpEnd': 105.0, 'EmpS': 90.0, 'HirA': 10.0, 'Sep': 6.0, 'FrmJbGn': 4.0, 'FrmJbLs': 0.0,
          'HirN': 8.0, 'HirR': 2.0, 'HirAEnd': 8.0, 'SepBeg': 3.0}
    adj = make_consistent(pv)
    assert adj == 1.0 and pv['FrmJbGn'] == 5.0 and pv['FrmJbLs'] == 0.0 and pv['Sep'] == 5.0 and identity_violations(pv) == 0
    pv2 = {'Emp': 50.0, 'EmpEnd': 48.0, 'EmpS': 40.0, 'HirA': 12.0, 'Sep': 14.0, 'FrmJbGn': 6.0, 'FrmJbLs': 8.0, 'HirAEnd': 4.0, 'SepBeg': 6.0}
    make_consistent(pv2)
    assert pv2['HirAEnd'] == 6.0 and pv2['SepBeg'] == 8.0 >= pv2['FrmJbLs'] and identity_violations(pv2) == 0   # B10
    # C2: 148 -> 60 is a break; 100 -> 120 is not
    assert is_break(60, 148) and not is_break(120, 100)
    assert start_weight(60, 148) == (60, True) and start_weight(120, 100) == (100, False)
    assert start_weight(60, 148, rule='min') == (60, True) and start_weight(120, 100, rule='min') == (100, False)
    assert start_weight(40, 0, rule='min')[0] == 0 and start_weight(40, 0, rule='prev')[0] == 40
    # F4: same seed string, same numbers
    assert rng_for(1, 'a').random() == rng_for(1, 'a').random()
    # F6
    assert str(out_value(-1e-9)) == '0.0' and earnings_out(3000, 0) == ''
    # F11
    assert preflight(os.path.dirname(os.path.abspath(__file__)), min_free_gb=0) >= 0
    assert is_test_run(['data/raw', 'students/ana/test_runs']) and is_test_run(['students\\ana\\t']) and not is_test_run(['data/raw', 'out'])
    k = keep_awake()
    if k: assert k.poll() is None; k.terminate(); k.wait()
    # F13
    assert parse_versions('', 5) == [1, 2, 3, 4, 5] and parse_versions('3', 5) == [3] and parse_versions('2-4', 5) == [2, 3, 4]
    assert versions_label([1, 2, 3]) == '1..3' and versions_label([1, 3]) == '1,3' and run_line().startswith('run: ')
    print('conditions.py: all self-tests passed')


if __name__ == '__main__':
    _tests()
