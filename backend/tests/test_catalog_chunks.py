"""Native chunk API on an isolated fixture; never opens production data."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import unittest
import httpx
from app import database
import test_senses as fixtures

BINARY=Path(__file__).resolve().parents[2]/'rust/target/debug/english-server'
class CatalogChunksTests(unittest.TestCase):
    def setUp(self):
        fixtures.SenseLearningTests.setUp(self)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        self.server=subprocess.Popen([str(BINARY),'--database',str(database.DB_PATH),'--enable-api','--listen',f'127.0.0.1:{port}'],env={**os.environ,'COOKIE_SECURE':'0'},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop)
        self.http=httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False);self.addCleanup(self.http.close)
        for _ in range(100):
            try:
                if self.http.get('/api/health').status_code==200:break
            except httpx.ConnectError:pass
            time.sleep(.05)
        response=self.http.post('/api/auth/login',json={'username':'learner','password':'test-password-123'})
        self.assertEqual(response.status_code,200,response.text)
    def stop(self):
        self.server.terminate();self.server.wait(timeout=5)
    def test_manifest_blocks_morphology_and_auth(self):
        url='/api/study/meaning-catalog?format=chunks'
        first=self.http.get(url);self.assertEqual(first.status_code,200,first.text)
        manifest=first.json();self.assertEqual(manifest['format'],1)
        self.assertEqual(manifest['words'],2)
        words=[]
        for block in manifest['blocks']:
            response=self.http.get('/api/study/meaning-catalog?chunk='+block['hash'])
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(len(response.content),block['bytes'])
            self.assertEqual(hashlib.sha256(response.content).hexdigest(),block['hash'])
            words.extend(response.json()['words'])
        self.assertTrue(words[0]['morphology_entries'])
        unchanged=self.http.get(url,headers={'If-None-Match':first.headers['etag']})
        self.assertEqual(unchanged.status_code,304)
        with database.connect() as conn:
            unit=conn.execute('SELECT id FROM lexical_units ORDER BY id LIMIT 1').fetchone()[0]
            conn.execute("INSERT INTO word_forms(lexical_unit_id,spelling,normalized_form,form_type,verification,evidence_kind,provenance_json,active) VALUES(?, 'addresses', 'addresses','plural','verified','manual','{}',1)",(unit,))
        changed=self.http.get(url,headers={'If-None-Match':first.headers['etag']})
        self.assertEqual(changed.status_code,200,changed.text)
        self.assertNotEqual(changed.headers['etag'],first.headers['etag'])
        other=self.http.post('/api/auth/register',json={'username':'other','password':'test-password-123'})
        self.assertEqual(other.status_code,200,other.text)
        forbidden=self.http.get('/api/study/meaning-catalog?chunk='+manifest['blocks'][0]['hash'])
        self.assertNotEqual(forbidden.status_code,200)
        self.http.cookies.clear()
        self.assertEqual(self.http.get(url).status_code,401)

    def test_streaming_export_resets_api_row_budget_per_word(self):
        # These metadata fields are read by morphology but are not exported.
        # Each word fits the API budget; the whole dictionary exceeds it.
        with database.connect() as conn:
            units = conn.execute("SELECT * FROM lexical_units WHERE id IN (SELECT MIN(id) FROM lexical_units GROUP BY word_id)").fetchall()
            self.assertGreater(len(units), 1)
            for unit in units:
                for index in range(4):
                    values = dict(unit)
                    values.pop('id')
                    values['unit_key'] += f':budget-{index}'
                    columns = list(values)
                    conn.execute(f"INSERT INTO lexical_units({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", list(values.values()))
            conn.execute("UPDATE lexical_units SET provenance_json=?", (json.dumps({"fixture": "x" * (1024 * 1024)}),))
        response = self.http.get('/api/study/meaning-catalog?format=chunks', timeout=60)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['words'], 2)
