import subprocess, sys, unittest
from pathlib import Path
class KitChecks(unittest.TestCase):
    def test_static_validator(self):
        root=Path(__file__).resolve().parents[1]
        p=subprocess.run([sys.executable,str(root/'scripts'/'validate_kit.py')],cwd=root,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.assertEqual(p.returncode,0,p.stdout+'\n'+p.stderr)
if __name__=='__main__': unittest.main()
