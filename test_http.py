import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

import requests


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        cls.base=f'http://127.0.0.1:{port}'
        cls.process=subprocess.Popen([sys.executable,'audit/ui_test_server.py','--port',str(port)],cwd=Path(__file__).parent,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                response=requests.get(cls.base+'/api/session',timeout=.3)
                if response.ok:break
            except requests.RequestException:pass
            time.sleep(.05)
        else:
            cls.process.terminate();raise RuntimeError('Test server did not start')
        cls.headers={'X-Book-Token':response.json()['token']}
    @classmethod
    def tearDownClass(cls):
        cls.process.terminate();cls.process.wait(timeout=10)
    def test_foreign_origin_and_missing_token_are_rejected(self):
        self.assertEqual(requests.post(self.base+'/api/restore',timeout=3).status_code,403)
        self.assertEqual(requests.get(self.base+'/api/projects',headers={'Origin':'https://foreign.invalid'},timeout=3).status_code,403)
    def test_prepare_translate_edit_review_export_backup_restore(self):
        source=b'CHAPTER 1\n\n'+b'A sailor walked along the harbour. '*30+b'\n\nCHAPTER 2\n\n'+b'She waited for his return. '*30
        preview=requests.post(self.base+'/api/preview',headers=self.headers,files={'file':('test.txt',source)},timeout=5)
        self.assertEqual(preview.status_code,200,preview.text)
        result=requests.post(self.base+'/api/import',headers=self.headers,files={'file':('test.txt',source)},data={'display_name':'HTTP scenario'},timeout=5)
        self.assertEqual(result.status_code,201,result.text);name=result.json()['name'];url=self.base+'/api/projects/'+name
        start=requests.post(url+'/start',headers=self.headers,json={},timeout=5);self.assertEqual(start.status_code,200,start.text)
        for _ in range(100):
            snap=requests.get(url,timeout=3).json()
            if not snap['runtime']['active']:break
            time.sleep(.05)
        self.assertTrue(snap['final_ready'],snap)
        c=requests.get(url+'/chunks/1',timeout=3).json()
        edit=requests.post(url+'/chunks/1',headers=self.headers,json={'text':'Проверенная ручная версия','expected_version':c['active_version']},timeout=3)
        self.assertEqual(edit.status_code,200,edit.text)
        version=edit.json()['active_version']
        review=requests.post(url+'/chunks/1',headers=self.headers,json={'action':'review','expected_version':version},timeout=3)
        self.assertEqual(review.status_code,200,review.text)
        exported=requests.post(url+'/export',headers=self.headers,json={'kind':'reviewed','positions':[1]},timeout=3)
        self.assertEqual(exported.status_code,200,exported.text)
        download=requests.get(self.base+exported.json()['url'],timeout=3)
        self.assertEqual(download.content.decode('utf-8').strip(),'Проверенная ручная версия')
        self.assertEqual(requests.get(url+'/final',timeout=3).status_code,200)
        history=requests.get(url+'/history',timeout=3).json()
        self.assertEqual(len(history['exports']),1)
        backup=requests.post(url+'/backup',headers=self.headers,json={},timeout=3)
        self.assertEqual(backup.status_code,200,backup.text)
        archive=requests.get(self.base+backup.json()['url'],timeout=3).content
        restored=requests.post(self.base+'/api/restore',headers=self.headers,files={'file':('backup.zip',archive)},timeout=5)
        self.assertEqual(restored.status_code,200,restored.text)
        self.assertNotEqual(restored.json()['name'],name)
        archived=requests.post(url+'/settings',headers=self.headers,json={'archived':True},timeout=5)
        self.assertTrue(archived.json()['archived'])
        self.assertEqual(requests.post(url+'/start',headers=self.headers,json={},timeout=3).status_code,409)
        returned=requests.post(url+'/settings',headers=self.headers,json={'archived':False},timeout=5)
        self.assertFalse(returned.json()['archived'])
        self.assertEqual(requests.delete(url,headers=self.headers,timeout=3).status_code,405)


if __name__=='__main__':unittest.main()
