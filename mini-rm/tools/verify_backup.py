"""Read-only verification of a restored backup against the current local database."""
import argparse
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from minirm.core import Service

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('backup')
parser.add_argument('--source',default=str(root/'data'/'minirm.sqlite3'))
args=parser.parse_args()
tables=('users','workspaces','memberships','requirements','requirement_versions','reviews','baselines','baseline_items','change_requests','change_items','artifacts','trace_links','audit_events','agent_runs')
connections=[]
try:
    for filename in (args.source,args.backup):
        db=sqlite3.connect(Path(filename).resolve().as_uri()+'?mode=ro',uri=True)
        db.row_factory=sqlite3.Row
        db.execute('BEGIN')
        connections.append(db)
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    a,b=connections
    for table in tables:
        before=[tuple(r) for r in a.execute(f'SELECT * FROM {table} ORDER BY rowid')]
        restored=[tuple(r) for r in b.execute(f'SELECT * FROM {table} ORDER BY rowid')]
        assert before==restored,table+' differs'
    assert Service(a,3,1).baseline(1)==Service(b,3,1).baseline(1)
    print('Backup reopened successfully; 14 business tables, foreign keys and B1 frozen snapshot match.')
finally:
    for db in connections:db.close()
