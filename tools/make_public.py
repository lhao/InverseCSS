"""Make the repository copy suitable for a public release (run after tools/sync_from_drive.sh).
The Drive master is never changed. Replaces the PI's cluster account and paths with placeholders
and installs the public Slurm README. Stops if any private string remains."""
import pathlib, re, shutil, sys
repo = pathlib.Path(__file__).resolve().parent.parent
shutil.copyfile(repo / 'tools/public/slurm_README.md', repo / 'code/slurm/README.md')
subs = [(r'--account=lhao2_inversecss', '--account=YOUR_ACCOUNT')]
for f in list((repo / 'code').rglob('*.sbatch')) + list((repo / 'code').rglob('*.sh')) + list((repo / 'docs').glob('*.md')):
    s = f.read_text(); t = s
    for a, b in subs: t = re.sub(a, b, t)
    if t != s: f.write_text(t); print('generalized', f.relative_to(repo))
private = re.compile(r'lhao2|/weka/|/Volumes/|@jh\.edu|@gmail', re.I)
bad = [str(f.relative_to(repo)) for f in repo.rglob('*') if f.is_file() and '.git' not in f.parts
       and f.name != 'make_public.py' and f.suffix in ('.py', '.sh', '.sbatch', '.md', '.txt', '.cff') and private.search(f.read_text(errors='ignore'))]
if bad: print('STOP: private strings remain in', bad); sys.exit(1)
print('public check passed')
