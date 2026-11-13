"""Run a command; append its elapsed seconds and peak memory (maxrss) to a timing file. No /usr/bin/time on Skipjack.
Usage: python3 code/slurm/timed.py <timing_file> <label> <log_file> -- <command ...>"""
import resource, subprocess, sys, time
out, label, log = sys.argv[1:4]; cmd = sys.argv[sys.argv.index('--') + 1:]
t = time.time()
with open(log, 'w') as fh: rc = subprocess.call(cmd, stdout=fh, stderr=subprocess.STDOUT)
kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
with open(out, 'a') as fh: fh.write(f'{label} rc={rc} elapsed_s={time.time() - t:.0f} maxrss_gb={kb / 2**20:.2f}\n')
sys.exit(rc)
