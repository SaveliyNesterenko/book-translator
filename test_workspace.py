import argparse
import copy
import io
import json
import logging
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
import yaml

import book_translator as bt
import web_app as web
import web_features as api
import workspace as ws


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        cfg=bt.load_config(Path(__file__).with_name('config.yaml'))
        cfg.pop('_config_dir');self.path=self.root/'config.yaml';self.path.write_text(yaml.safe_dump(cfg),encoding='utf-8')
        self.patches=[patch.object(web,'CONFIG_PATH',self.path),patch.object(web,'SOURCES_DIR',self.root/'sources'),patch.object(web,'manager',web.TranslationManager())]
        for p in self.patches:p.start()
        self.cfg=web.config()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def create(self):
        text='CHAPTER 1\n\n'+('An old sailor crossed the quiet harbour. '*18)+'\n\nCHAPTER 2\n\n'+('She listened and looked at the sea. '*20)
        return api.create_from_bytes(text.encode(),'novel.txt','Novel','utf-8-sig',False,'[]')['name']
    def conn(self,name):return web.open_project_connection(name)[0]
    def test_same_titles_are_independent_and_empty_import_leaves_no_project(self):
        self.assertNotEqual(self.create(),self.create())
        before=len(web.list_projects()['projects'])
        with self.assertRaises(ValueError):api.create_from_bytes(b'  ','empty.txt','Empty','utf-8-sig',False,'[]')
        self.assertEqual(len(web.list_projects()['projects']),before)
    def test_migration_is_atomic_and_rejects_future_schema(self):
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
        c.execute('CREATE TABLE chunks(position INTEGER,translated_text TEXT,translated_at TEXT)')
        c.execute("INSERT INTO chunks VALUES(1,'keep',NULL)");c.execute('CREATE TABLE versions(id INTEGER)');c.commit()
        with self.assertRaises(sqlite3.OperationalError):ws.migrate(c)
        self.assertNotIn('active_version',[r[1] for r in c.execute('pragma table_info(chunks)')])
        self.assertEqual(c.execute('select translated_text from chunks').fetchone()[0],'keep')
        c.execute('pragma user_version=999')
        with self.assertRaises(ValueError):ws.migrate(c)
        c.close()
    def test_os_lock_blocks_another_process(self):
        name=self.create();db=web.project_paths(name)['db']
        with ws.project_lock(db):
            result=subprocess.run([sys.executable,'-c','import workspace,sys;print(workspace.is_busy(sys.argv[1]))',str(db)],capture_output=True,text=True,cwd=Path(__file__).parent)
            self.assertEqual(result.stdout.strip(),'True',result.stderr)
        self.assertFalse(ws.is_busy(db))
    def test_preview_opens_done_instead_of_pending_and_ignores_stale_export(self):
        name=self.create();c=self.conn(name)
        ws.save_version(c,1,'Готовый текст','manual');c.commit();c.close()
        paths=web.project_paths(name);paths['partial'].parent.mkdir();paths['partial'].write_text('STALE')
        p=web.project_snapshot(name)
        self.assertEqual(p['current']['position'],1);self.assertEqual(p['partial_count'],1)
        result=api.make_export(name,{'kind':'prefix'})
        path=Path(api.get_export(name, result['id']).path)
        self.assertEqual(path.read_text(encoding='utf-8').strip(),'Готовый текст')
    def test_manual_versions_review_restore_and_conflict(self):
        name=self.create()
        first=api.edit_chunk(name,1,{'text':'Первая версия','expected_version':None})
        reviewed=api.edit_chunk(name,1,{'action':'review','expected_version':first['active_version']})
        self.assertEqual(reviewed['reviewed_version'],first['active_version'])
        second=api.edit_chunk(name,1,{'text':'Вторая версия','expected_version':first['active_version']})
        self.assertIsNone(second['reviewed_version']);self.assertEqual(len(second['versions']),2)
        with self.assertRaises(ws.BusyError):api.edit_chunk(name,1,{'text':'Конфликт','expected_version':first['active_version']})
        restored=api.edit_chunk(name,1,{'action':'restore','version':first['active_version'],'expected_version':second['active_version']})
        self.assertEqual(restored['translated_text'],'Первая версия');self.assertEqual(len(restored['versions']),3)
    def test_pause_in_retry_prevents_next_request_and_preserves_history(self):
        name=self.create();c=self.conn(name);row=c.execute('select * from chunks limit 1').fetchone();stop=threading.Event()
        class Client:
            calls=0
            def translate(self,*a):self.calls+=1;raise requests.ConnectionError('offline')
        client=Client()
        def progress(phase,*args,**kwargs):
            if phase=='retry_wait':stop.set()
        status,_=ws.translate_one(c,row,client,'model',self.cfg,logging.getLogger('test'),stop,progress=progress)
        self.assertEqual(status,'paused');self.assertEqual(client.calls,1)
        self.assertEqual(c.execute('select count(*) from attempts').fetchone()[0],1)
        self.assertEqual(c.execute('select attempts from chunks where position=1').fetchone()[0],1)
        c.close()
    def test_retry_keeps_failed_response_and_existing_manual_version(self):
        name=self.create();c=self.conn(name);ws.save_version(c,1,'Сохранённая ручная правка','manual');c.commit()
        class Client:
            def translate(self,*a):raise bt.NonRetryableTranslationError('truncated')
        row=c.execute('select * from chunks where position=1').fetchone()
        status,_=ws.translate_one(c,row,Client(),'model',self.cfg,logging.getLogger('test'))
        self.assertEqual(status,'failed');self.assertEqual(c.execute('select translated_text from chunks where position=1').fetchone()[0],'Сохранённая ручная правка')
        bt.reset_for_retry(c);self.assertEqual(c.execute('select count(*) from attempts').fetchone()[0],1);c.close()
    def test_restore_archive_and_reject_tampering(self):
        name=self.create();c=self.conn(name);ws.save_version(c,1,'Сохранённый перевод','manual');c.commit();c.close()
        result=api.make_backup(name);raw=(self.root/'backups'/result['filename']).read_bytes()
        restored=api.restore_bytes(raw)
        self.assertNotEqual(name,restored['name']);self.assertEqual(api.chunk(restored['name'],1)['translated_text'],'Сохранённый перевод')
        self.assertTrue((web.project_paths(restored['name'])['project_dir']/'source.txt').exists())
        import zipfile
        output=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as source,zipfile.ZipFile(output,'w') as target:
            for item in source.namelist():target.writestr(item,b'changed' if item=='source.txt' else source.read(item))
        with self.assertRaises(ValueError):api.restore_bytes(output.getvalue())
    def test_reviewed_export_rejects_unreviewed_and_holes(self):
        name=self.create();c=self.conn(name);ws.save_version(c,1,'Первый перевод','manual');c.commit();c.close()
        with self.assertRaises(ValueError):api.make_export(name,{'kind':'reviewed','positions':[1]})
        with self.assertRaises(ValueError):api.make_export(name,{'kind':'draft'})
        c=api.chunk(name,1);api.edit_chunk(name,1,{'action':'review','expected_version':c['active_version']})
        self.assertIn('url',api.make_export(name,{'kind':'reviewed','positions':[1]}))

    def test_cli_and_web_export_the_same_included_versions(self):
        name = self.create()
        conn, paths = web.open_project_connection(name)
        try:
            with conn:
                ws.save_version(conn, 1, 'Готовый перевод.', 'manual')
                conn.execute('UPDATE chunks SET excluded=1 WHERE position=2')
            bt.write_final(conn, paths['final'])
        finally:
            conn.close()
        exported = api.make_export(name, {'kind': 'draft'})
        output = Path(api.get_export(name, exported['id']).path).read_bytes()
        self.assertEqual(output, 'Готовый перевод.\n'.encode())
        self.assertEqual(paths['final'].read_bytes(), output)

    def test_cli_returns_error_when_translation_fails(self):
        source = self.root / 'cli.txt'
        source.write_text('The sailor watched the sea.', encoding='utf-8')
        args = argparse.Namespace(book=str(source), project_name='cli', rebuild=False,
                                  max_chunks=None, start_chunk=None, verbose=False)
        with patch.object(bt.LMStudioClient, 'resolve_model', return_value='model'), \
             patch.object(bt.LMStudioClient, 'translate', side_effect=bt.NonRetryableTranslationError('Обрезанный перевод')), \
             patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
            result = bt.cmd_translate(args, self.cfg)
        self.assertEqual(result, 1)
        self.assertFalse(web.project_paths('cli')['final'].exists())
        self.assertEqual(web.project_snapshot('cli')['counts']['failed'], 1)

    def test_restore_in_another_library_keeps_versions_and_requires_new_exports(self):
        name = self.create()
        conn = self.conn(name)
        try:
            with conn:
                ws.save_version(conn, 1, 'Ручная редакция.', 'manual')
                saved = json.loads(bt.get_meta(conn, 'profile_json'))
                saved['lmstudio']['base_url'] = 'http://127.0.0.1:9/v1'
                bt.set_meta(conn, 'profile_json', json.dumps(saved))
                self.assertEqual(ws.effective_config(conn, self.cfg)['lmstudio']['base_url'],
                                 self.cfg['lmstudio']['base_url'])
        finally:
            conn.close()
        exported = api.make_export(name, {'kind':'prefix'})
        backup = api.make_backup(name)
        raw = (self.root/'backups'/backup['filename']).read_bytes()
        other_root = self.root/'other-library'
        other_cfg = {**self.cfg, '_config_dir': str(other_root)}
        with patch.object(web, 'config', return_value=other_cfg):
            restored = api.restore_bytes(raw)['name']
            prior_export = api.history(restored)['exports'][0]
            self.assertEqual(prior_export['id'], exported['id'])
            self.assertFalse(prior_export['available'])
            self.assertEqual(api.chunk(restored, 1)['translated_text'], 'Ручная редакция.')
            new_export = api.make_export(restored, {'kind':'prefix'})
            self.assertEqual(Path(api.get_export(restored, new_export['id']).path).read_bytes(),
                             'Ручная редакция.\n'.encode())

    def test_restore_write_failure_leaves_no_new_book(self):
        name = self.create()
        backup = api.make_backup(name)
        raw = (self.root/'backups'/backup['filename']).read_bytes()
        directory = web.project_paths(name)['project_dir'].parent
        before = sorted(directory.iterdir())
        with patch.object(Path, 'rename', side_effect=PermissionError('cannot publish')):
            with self.assertRaises(PermissionError):
                api.restore_bytes(raw)
        self.assertEqual(sorted(directory.iterdir()), before)
        self.assertEqual(web.project_snapshot(name)['total'], 2)
    def test_verse_split_preserves_linebreaks_and_long_word(self):
        text=('First line of verse\nSecond line of verse\n'*30).strip()
        pieces=bt.split_oversized_paragraph(text,90)
        self.assertEqual(''.join(pieces),text)
        self.assertEqual(''.join(bt.split_oversized_paragraph('a'*301,100)),'a'*301)
    def test_recovery_does_not_change_done_versions(self):
        name=self.create();c=self.conn(name);version=ws.save_version(c,1,'Keep','manual')
        c.execute("update chunks set status='processing' where position=2")
        c.execute("INSERT INTO runs (id,started_at,state,profile,selection) VALUES ('interrupted',?,'translating','{}','[2]')", (ws.now(),))
        c.commit()
        c.close()
        snapshot = web.project_snapshot(name)
        self.assertEqual(snapshot['runtime']['phase'], 'interrupted')
        self.assertEqual(snapshot['counts']['pending'], 1)
        c = self.conn(name)
        self.assertEqual(c.execute('select active_version from chunks where position=1').fetchone()[0],version)
        c.close()
    def test_saved_profile_is_used_after_global_change(self):
        name=self.create();c=self.conn(name);changed=copy.deepcopy(self.cfg);changed['lmstudio']['model']='different'
        self.assertEqual(ws.project_config(c,changed)['lmstudio']['model'],self.cfg['lmstudio']['model']);c.close()
    def test_signals_are_warnings_not_automatic_rejections(self):
        source='This is a long English paragraph that was not translated at all. '*3
        self.assertTrue(ws.warnings(source,source));self.assertEqual(bt.validate_translation(source,source,self.cfg),[])

    def test_manual_book_is_translatable_before_markup_is_complete(self):
        text=('The sea was quiet and the old sailor watched the horizon. '*180).strip()
        name=api.create_from_bytes(text.encode(),'manual.txt','Manual','utf-8-sig',False,'[]','manual','[]',False)['name']
        initial=api.manual_state(name)
        self.assertEqual(initial['cursor'],0);self.assertEqual(initial['boundaries'],[])
        first=api.update_manual(name,{'action':'add','offset':3500,'expected_cursor':0})
        self.assertEqual(len(first['boundaries']),1);self.assertFalse(first['complete'])
        conn=self.conn(name);row=conn.execute('select * from chunks where position=1').fetchone()
        ws.save_version(conn,1,'Переведённый первый фрагмент','manual');conn.commit();conn.close()
        second=api.update_manual(name,{'action':'add','offset':7000,'expected_cursor':first['cursor']})
        self.assertEqual(len(second['boundaries']),2)
        conn=self.conn(name)
        self.assertIsNotNone(conn.execute('select active_version from chunks where position=1').fetchone()[0])
        self.assertEqual(conn.execute('select status from chunks where position=2').fetchone()[0],'pending')
        prefix=''.join(r[0]+r[1] for r in conn.execute('select source_text,joiner_after from chunks order by position'))
        self.assertEqual(prefix,second['text'][:second['cursor']]);conn.close()

    def test_manual_last_boundary_is_removable_until_work_starts_and_finish_adds_tail(self):
        text=('A complete sentence beside the harbour. '*220).strip()
        name=api.create_from_bytes(text.encode(),'manual.txt','Manual','utf-8-sig',False,'[]','manual','[]',False)['name']
        one=api.update_manual(name,{'action':'add','offset':3000,'expected_cursor':0})
        removed=api.update_manual(name,{'action':'remove_last','expected_cursor':one['cursor']})
        self.assertEqual(removed['cursor'],0);self.assertEqual(removed['boundaries'],[])
        one=api.update_manual(name,{'action':'add','offset':3500,'expected_cursor':0})
        conn=self.conn(name);conn.execute('update chunks set attempts=1 where position=1');conn.commit();conn.close()
        with self.assertRaises(ValueError):api.update_manual(name,{'action':'remove_last','expected_cursor':one['cursor']})
        two=api.update_manual(name,{'action':'add','offset':7000,'expected_cursor':one['cursor']})
        finished=api.update_manual(name,{'action':'finish','expected_cursor':two['cursor']})
        self.assertTrue(finished['complete']);self.assertEqual(finished['cursor'],len(finished['text']))
        conn=self.conn(name);whole=''.join(r[0]+r[1] for r in conn.execute('select source_text,joiner_after from chunks order by position'))
        self.assertEqual(whole,finished['text']);conn.close()
        self.assertFalse(web.project_snapshot(name)['final_ready'])

    def test_manual_boundary_can_be_added_while_previous_fragment_is_translating(self):
        text=('The lighthouse keeper watched the distant sea. '*180).strip()
        name=api.create_from_bytes(text.encode(),'manual.txt','Manual','utf-8-sig',False,'[]','manual','[]',False)['name']
        first=api.update_manual(name,{'action':'add','offset':2500,'expected_cursor':0})
        entered=threading.Event();release=threading.Event();failure=[]
        def translate(client,model,source):
            entered.set();release.wait(5);return ('Русский перевод '*max(1,len(source)//20),10,20)
        def worker():
            try:ws.run(web.project_paths(name)['db'],self.cfg)
            except BaseException as exc:failure.append(exc)
        with patch.object(bt.LMStudioClient,'list_models',return_value=[self.cfg['lmstudio']['model']]),patch.object(bt.LMStudioClient,'translate',translate):
            thread=threading.Thread(target=worker);thread.start();self.assertTrue(entered.wait(3))
            second=api.update_manual(name,{'action':'add','offset':5000,'expected_cursor':first['cursor']})
            self.assertEqual(len(second['boundaries']),2);release.set();thread.join(5)
        self.assertFalse(thread.is_alive());self.assertEqual(failure,[])
        conn=self.conn(name);self.assertEqual(conn.execute('select status from chunks where position=1').fetchone()[0],'done')
        self.assertEqual(conn.execute('select status from chunks where position=2').fetchone()[0],'pending');conn.close()

    def test_crlf_import_does_not_double_linebreaks(self):
        text='CHAPTER 1\r\n\r\nThe harbour was quiet.\r\nThe keeper was waiting.\r\n'
        name=api.create_from_bytes(text.encode(),'crlf.txt','CRLF','utf-8-sig',False,'[]')['name']
        source=(web.project_paths(name)['project_dir']/'source.txt').read_bytes()
        self.assertNotIn(b'\r\r\n',source)
        self.assertEqual(source,text.encode())

    def test_short_book_sample_avoids_contents(self):
        raw=(Path(__file__).parent/'tests/fixtures/harbour.txt').read_bytes()
        name=api.create_from_bytes(raw,'short.txt','Short','utf-8-sig',False,'[]')['name']
        sample=api.sample_selection(name,{})['chunks']
        self.assertEqual([r['position'] for r in sample],[2,3,4])

    def test_preferences_and_notes_are_persistent(self):
        name=self.create();api.save_preferences({'book':name,'fontSize':'21',f'position:{name}':2})
        self.assertEqual(api.preferences()['book'],name)
        api.edit_chunk(name,1,{'action':'note','note':'Проверить имя героя','expected_version':None})
        self.assertEqual(api.chunk(name,1)['note'],'Проверить имя героя')

    def test_300_chunks_pause_resume_without_duplicate_versions(self):
        name=self.create();conn=self.conn(name)
        conn.executemany('INSERT INTO chunks(position,source_text) VALUES(?,?)',[(i,'A sailor watched the sea. '*8) for i in range(3,301)])
        conn.commit();conn.close();stop=threading.Event()
        def progress(phase,message,**detail):
            if detail.get('position')==50:stop.set()
        def translate(client,model,source):return ('Русский перевод '*max(1,len(source)//15),10,20)
        with patch.object(bt.LMStudioClient,'list_models',return_value=[self.cfg['lmstudio']['model']]),patch.object(bt.LMStudioClient,'translate',translate):
            ws.run(web.project_paths(name)['db'],self.cfg,stop=stop,progress=progress)
            self.assertEqual(web.project_snapshot(name)['done'],50)
            ws.run(web.project_paths(name)['db'],self.cfg)
        conn=self.conn(name)
        self.assertEqual(conn.execute('SELECT count(*) FROM versions').fetchone()[0],300)
        self.assertEqual(conn.execute('SELECT count(*) FROM attempts').fetchone()[0],300)
        self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0],'ok');conn.close()
        self.assertTrue(web.project_snapshot(name)['final_ready'])
        self.assertIn('url',api.make_export(name,{'kind':'draft'}))


if __name__=='__main__':unittest.main()
