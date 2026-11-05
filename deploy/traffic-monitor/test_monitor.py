import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('monitor',Path(__file__).with_name('monitor.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class MonitorTests(unittest.TestCase):
    def test_baseline_restart_and_persistent_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)
            def sample(g,rx,tx,t):
                return m.collect(state,{'host':(g,rx,tx)},t)
            self.assertEqual(sample('a',100,200,3600)['sources'][0]['rx'],0)
            a=sample('a',160,320,3660)['sources'][0]
            self.assertEqual((a['rx'],a['tx'],a['rx_rate']), (60,120,1))
            a=sample('b',10,20,3720)['sources'][0]
            self.assertEqual((a['rx'],a['tx']), (70,140))
            snapshot=json.loads((state/'snapshot.json').read_text())
            self.assertEqual(snapshot['history'][0]['rx'],70)
            self.assertEqual((state/'traffic.sqlite').stat().st_mode & 0o777,0o600)
    def test_missing_counter_is_unavailable_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            m.collect(Path(tmp),{'host':('a',10,20)},3600)
            s=m.collect(Path(tmp),{},3660)
            self.assertFalse(s['sources'][0]['available'])
            self.assertFalse(s['sources'][1]['available'])
    def test_reset_does_not_create_negative_traffic(self):
        self.assertEqual(m.difference(('a',100,200),'a',10,20),(10,20))
    def test_auth_fails_closed(self):
        self.assertEqual(m.admin_status(''),401)
        with patch.object(m.http.client,'HTTPConnection') as c:
            r=c.return_value.getresponse.return_value
            r.status=200;r.read.return_value=b'{"user":{"role":"learner"}}'
            self.assertEqual(m.admin_status('session=fake'),403)
            r.read.return_value=b'{"user":{"role":"admin"}}'
            self.assertEqual(m.admin_status('session=fake'),200)
            r.read.return_value=b'not json'
            self.assertEqual(m.admin_status('session=fake'),503)
            r.status=401
            self.assertEqual(m.admin_status('session=fake'),401)

if __name__=='__main__':unittest.main()
