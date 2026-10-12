"""Create a consistent SQLite backup, including while the local app is running."""
import argparse
import sqlite3
from pathlib import Path
from datetime import datetime

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--source', default=str(root/'data'/'minirm.sqlite3'))
parser.add_argument('--output', default=str(root/'backups'/('minirm-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.sqlite3')))
args = parser.parse_args()
source, output = Path(args.source).resolve(), Path(args.output).resolve()
if not source.is_file() or output.exists() or source == output:
    parser.error('source must exist and output must be a new, different file')
output.parent.mkdir(parents=True, exist_ok=True)
src = sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)
dest = sqlite3.connect(output)
try:
    src.backup(dest)
    result = dest.execute('PRAGMA integrity_check').fetchone()[0]
    if result != 'ok':
        raise RuntimeError('Backup integrity check failed')
finally:
    src.close()
    dest.close()
print(output)
