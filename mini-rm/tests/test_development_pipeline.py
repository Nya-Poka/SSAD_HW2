import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.development_pipeline import validate_files


class ProposalGateTests(unittest.TestCase):
    def proposal(self, files):
        return {'result':{'files':files}}

    def test_paths_outside_whitelist_rejected(self):
        for path in ('../server.py','data/minirm.sqlite3','.env','C:/Windows/a.py'):
            with self.assertRaises(ValueError):validate_files(self.proposal([{'path':path,'content':'pass'}]))

    def test_duplicate_paths_rejected(self):
        file={'path':'server.py','content':'pass'}
        with self.assertRaises(ValueError):validate_files(self.proposal([file,file]))

    def test_invalid_python_syntax_rejected(self):
        with self.assertRaises(SyntaxError):validate_files(self.proposal([{'path':'server.py','content':'def invalid('}]))

    def test_valid_python_is_only_parsed_never_executed(self):
        files=[{'path':'server.py','content':'raise RuntimeError("must never execute")'}]
        self.assertEqual(validate_files(self.proposal(files)),files)


if __name__=='__main__':unittest.main()
