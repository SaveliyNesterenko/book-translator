"""Book preparation, review and export API for the local workspace."""
import hashlib
import io
import json
import re
import secrets
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

import book_translator as bt
import workspace as ws

router = APIRouter()
TOKEN = secrets.token_urlsafe(32)


def web():
    import web_app
    return web_app


def snapshot(name):
    w=web();conn,paths=w.open_project_connection(name)
    try:
        ws.recover_if_idle(conn, paths['db'])
        rows=conn.execute('SELECT position,status,active_version,reviewed_version,excluded,length(translated_text) chars FROM chunks ORDER BY position').fetchall()
        included=[r for r in rows if not r['excluded']]
        done=sum(bool(r['active_version'] and r['chars']) for r in included)
        reviewed=sum(bool(r['active_version'] and r['active_version']==r['reviewed_version']) for r in included)
        prefix=0
        for row in included:
            if not row['active_version'] or not row['chars']:break
            prefix+=1
        counts={s:sum(r['status']==s for r in included) for s in ['done','pending','processing','failed']}
        current=conn.execute('SELECT position,section,status,source_text,translated_text,error,attempts FROM chunks ORDER BY (active_version IS NOT NULL) DESC, CASE WHEN active_version IS NOT NULL THEN -position ELSE position END LIMIT 1').fetchone()
        totals=conn.execute('SELECT coalesce(sum(duration_seconds),0),coalesce(sum(prompt_tokens),0),coalesce(sum(completion_tokens),0) FROM chunks').fetchone()
        last=conn.execute('SELECT * FROM runs ORDER BY started_at DESC LIMIT 1').fetchone()
        runtime=w.manager.runtime_for(name)
        if not runtime['active'] and ws.is_busy(paths['db']):
            runtime.update(active=True,phase='external',message='Книга занята другим окном или операцией')
        elif not runtime['active'] and last and runtime['phase'] == 'idle':
            runtime.update(phase=last['state'] if last['ended_at'] else 'interrupted',
                           message=last['message'] if last['ended_at'] else 'Предыдущая работа прервана. Можно продолжить.')
        local_config = w.config()
        profile_saved = bool(bt.get_meta(conn, 'profile_json'))
        profile = ws.clean_profile(ws.effective_config(conn, local_config))
        manual=bt.get_meta(conn,'segmentation_mode')=='manual'
        manual_complete=bt.get_meta(conn,'manual_complete')=='1' if manual else True
        manual_cursor=int(bt.get_meta(conn,'manual_cursor') or 0) if manual else None
        source_chars=int(bt.get_meta(conn,'manual_source_chars') or manual_cursor or 0) if manual else None
        return {'name':name,'display_name':bt.get_meta(conn,'display_name') or name,
                'source_name':bt.get_meta(conn, 'source_filename') or Path(bt.get_meta(conn,'source_path') or '').name,
                'counts':counts,'total':len(included),'all_total':len(rows),'done':done,'reviewed':reviewed,
                'percent':round(100*done/len(included),1) if included else 0,
                'current':dict(current) if current else None,'partial_count':prefix,
                'partial_ready':prefix>0,'final_ready':bool(included) and done==len(included) and manual_complete,
                'duration_minutes':round(totals[0]/60,1),'prompt_tokens':totals[1],'completion_tokens':totals[2],
                'runtime':runtime,'last_run':dict(last) if last else None,
                'archived':bt.get_meta(conn,'archived')=='1','notes':json.loads(bt.get_meta(conn,'preprocessing_notes') or '[]'),
                'manual':{'enabled':manual,'complete':manual_complete,'cursor':manual_cursor,'source_chars':source_chars,
                          'remaining':max(0,(source_chars or 0)-(manual_cursor or 0))} if manual else {'enabled':False,'complete':True},
                'profile':profile,'profile_saved':profile_saved,
                'connection':local_config['lmstudio']['base_url'],
                'last_saved':conn.execute('SELECT max(created_at) FROM versions').fetchone()[0]}
    finally:conn.close()


@router.get('/api/session')
def session():return {'token':TOKEN,'version':bt.APP_VERSION}


@router.get('/api/preferences')
def preferences():
    path=Path(web().config()['_config_dir'])/'.ui-preferences.json'
    try:return json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError,ValueError):return {}


@router.post('/api/preferences')
def save_preferences(body: dict):
    root=Path(web().config()['_config_dir'])
    with ws.project_lock(root/'.ui-preferences'):
        values=preferences()
        for key,value in body.items():
            if key not in ('book','fontSize') and not re.fullmatch(r'(position|scroll):[A-Za-z0-9_.:-]{1,120}',key):
                raise ValueError('Некорректное свойство интерфейса')
            if len(json.dumps(value))>300:raise ValueError('Значение слишком большое')
            values[key]=value
        bt.atomic_write_text(root/'.ui-preferences.json',json.dumps(values,ensure_ascii=False))
    return {'ok':True}


async def read_upload(file,limit=50*1024*1024):
    raw=await file.read(limit+1)
    if len(raw)>limit:raise HTTPException(413,'Файл слишком большой')
    return raw


def chunk_limits(cfg):
    chunking=cfg['chunking']
    chars_per_token=float(chunking['chars_per_token'])
    return {'target_chars':max(200,int(int(chunking['target_tokens'])*chars_per_token)),
            'hard_max_chars':max(200,int(int(chunking['hard_max_tokens'])*chars_per_token)),
            'chars_per_token':chars_per_token}


def prepare_preview(raw,encoding='utf-8-sig',strip_wrapper=True,mode='auto'):
    if encoding not in ('utf-8-sig','cp1251','cp1252'):raise ValueError('Выберите поддерживаемую кодировку')
    try:text=raw.decode(encoding)
    except UnicodeDecodeError:raise ValueError('Не удалось прочитать текст. Выберите кодировку и проверьте предпросмотр.')
    if not text.strip() or '\x00' in text:raise ValueError('Файл пуст или не является обычным текстом')
    cfg=web().config();cfg['preprocessing']['strip_gutenberg_wrapper']=strip_wrapper
    cleaned,notes=bt.preprocess_text(text,cfg)
    if not cleaned:raise ValueError('После очистки не осталось текста')
    if mode not in ('auto','manual'):raise ValueError('Неизвестный способ разделения')
    if mode=='manual':
        return text,cleaned,cfg,{'mode':'manual','total':0,'original_chars':len(text),'prepared_chars':len(cleaned),'notes':notes,
                                'original_preview':text[:1800],'prepared_preview':cleaned[:1800],'prepared_text':cleaned,
                                'prepared_sha256':bt.sha256_text(cleaned),**chunk_limits(cfg),
                                'removed_start':text[:text.find(cleaned[:100])] if cleaned[:100] in text else '',
                                'removed_end':text[text.rfind(cleaned[-100:])+100:] if cleaned[-100:] in text else '',
                                'chunks':[]}
    chunks=bt.build_chunks(cleaned,cfg)
    result=[]
    for c in chunks:
        toc=len(re.findall(r'(?im)^CHAPTER\s+\d+',c.source_text))>2
        result.append({'position':c.position,'section':'Оглавление' if toc else c.section or 'Начало книги',
                       'chars':len(c.source_text),'preview':c.source_text[:240],'text_type':'contents' if toc else 'prose'})
    return text,cleaned,cfg,{'mode':'auto','total':len(chunks),'original_chars':len(text),'prepared_chars':len(cleaned),'notes':notes,
                           'original_preview':text[:1800],'prepared_preview':cleaned[:1800],
                           'removed_start':text[:text.find(cleaned[:100])] if cleaned[:100] in text else '',
                           'removed_end':text[text.rfind(cleaned[-100:])+100:] if cleaned[-100:] in text else '',
                           'chunks':result}


@router.post('/api/preview')
async def preview(file: UploadFile=File(...),encoding: str=Form('utf-8-sig'),strip_wrapper: bool=Form(True),mode: str=Form('auto')):
    if not file.filename or not file.filename.lower().endswith('.txt'):raise ValueError('Выберите TXT-файл')
    return (await run_in_threadpool(prepare_preview,await read_upload(file),encoding,strip_wrapper,mode))[3]


async def import_upload(file,display_name,encoding='utf-8-sig',strip_wrapper=True,structure='[]',mode='auto',boundaries='[]',manual_complete=False):
    if not file.filename or not file.filename.lower().endswith('.txt'):raise ValueError('Выберите TXT-файл')
    return await run_in_threadpool(create_from_bytes,await read_upload(file),file.filename,display_name,encoding,strip_wrapper,structure,mode,boundaries,manual_complete)


@router.post('/api/import',status_code=201)
async def import_book(file: UploadFile=File(...),display_name: str=Form(''),encoding: str=Form('utf-8-sig'),strip_wrapper: bool=Form(True),structure: str=Form('[]'),mode: str=Form('auto'),boundaries: str=Form('[]'),manual_complete: bool=Form(False)):
    return await import_upload(file,display_name,encoding,strip_wrapper,structure,mode,boundaries,manual_complete)


def normalize_manual_boundaries(text,boundaries,complete=False):
    if not isinstance(boundaries,list) or len(boundaries)>100000 or any(type(v)!=int for v in boundaries):
        raise ValueError('Некорректные границы ручной разметки')
    result=[]
    for value in boundaries:
        if value<=0 or value>=len(text):raise ValueError('Граница находится за пределами текста')
        if text[value-1].isalnum() and text[value].isalnum():
            left=value-1
            while left>0 and not text[left].isspace():left-=1
            right=value
            while right<len(text) and not text[right].isspace():right+=1
            choices=[v for v in (left,right) if 0<v<len(text)]
            if not choices:raise ValueError('Не удалось поставить границу между словами')
            value=min(choices,key=lambda v:abs(v-value))
        while value<len(text) and text[value].isspace():value+=1
        if value>=len(text):continue
        if not result or value!=result[-1]:result.append(value)
    if result!=sorted(result):raise ValueError('Границы должны идти по порядку')
    if complete and (not result or result[-1]!=len(text)):result.append(len(text))
    return result


def manual_rows(text,boundaries,cfg,start=0,position=1):
    limits=chunk_limits(cfg);rows=[]
    for end in boundaries:
        raw=text[start:end];body=raw.rstrip();joiner=raw[len(body):]
        if not body.strip():raise ValueError('Пустой фрагмент нельзя добавить')
        if len(body)>limits['hard_max_chars']:
            raise ValueError(f'Фрагмент {position} слишком длинный: {len(body)} знаков при максимуме {limits["hard_max_chars"]}')
        rows.append((position,'Ручная разметка',body,joiner,bt.estimate_tokens(body,limits['chars_per_token']),start,start+len(body)))
        start=end;position+=1
    return rows


@contextmanager
def stage_project(paths):
    """Publish the source and database together once both are ready."""
    target = paths['project_dir']
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent, prefix='.pending-book-') as tmp:
        scratch = Path(tmp) / 'book'
        scratch.mkdir()
        yield scratch
        scratch.rename(target)


def managed_source_path(name, paths):
    packaged = paths['project_dir'] / 'source.txt'
    if packaged.is_file():
        return packaged
    return web().SOURCES_DIR / f'{name}.txt'


def create_from_bytes(raw,filename,display_name,encoding,strip_wrapper,structure,mode='auto',boundaries='[]',manual_complete=False):
    w=web();text,cleaned,cfg,info=prepare_preview(raw,encoding,strip_wrapper,mode)
    edits=json.loads(structure)
    if not isinstance(edits,list):raise ValueError('Некорректная структура книги')
    name='book-'+uuid.uuid4().hex[:12];paths=w.project_paths(name)
    source=paths['project_dir']/'source.txt'
    with stage_project(paths) as scratch:
        source_tmp=scratch/'source.txt'
        source_tmp.write_text(text,encoding='utf-8',newline='')
        if mode=='manual':
            conn=bt.connect_db(scratch/'translation.db');bt.init_schema(conn)
            parsed=json.loads(boundaries)
            normalized=normalize_manual_boundaries(cleaned,parsed,bool(manual_complete))
            rows=manual_rows(cleaned,normalized,cfg)
            if rows:conn.executemany('INSERT INTO chunks(position,section,source_text,joiner_after,estimated_tokens,source_start,source_end) VALUES(?,?,?,?,?,?,?)',rows)
            bt.set_meta(conn,'source_sha256',bt.sha256_text(cleaned));bt.set_meta(conn,'app_version',bt.APP_VERSION)
            bt.set_meta(conn,'chunk_count',str(len(rows)));bt.set_meta(conn,'preprocessing_notes',json.dumps(info['notes'],ensure_ascii=False))
            bt.set_meta(conn,'segmentation_mode','manual');bt.set_meta(conn,'manual_cursor',str(normalized[-1] if normalized else 0))
            bt.set_meta(conn,'manual_complete','1' if manual_complete else '0')
            bt.set_meta(conn,'manual_source_chars',str(len(cleaned)))
        else:
            conn=bt.prepare_project(source_tmp,cfg,{**paths,'db':scratch/'translation.db'})
        try:
            bt.set_meta(conn,'source_path',str(source.resolve()))
            bt.set_meta(conn,'source_filename',Path(filename).name)
            bt.set_meta(conn,'display_name',(display_name.strip() or Path(filename).stem)[:160])
            bt.set_meta(conn,'profile_json',json.dumps(ws.clean_profile(cfg),ensure_ascii=False))
            bt.set_meta(conn,'original_encoding',encoding)
            if mode=='auto':
                for c in info['chunks']:conn.execute('UPDATE chunks SET section=?,text_type=? WHERE position=?',(c['section'],c['text_type'],c['position']))
                offset=0
                for row in conn.execute('SELECT position,source_text,joiner_after FROM chunks ORDER BY position').fetchall():
                    end = offset + len(row['source_text'])
                    conn.execute('UPDATE chunks SET source_start=?,source_end=? WHERE position=?', (offset,end,row['position']))
                    offset = end + len(row['joiner_after'])
                for edit in edits:conn.execute('UPDATE chunks SET section=?,excluded=? WHERE position=?',(str(edit['section'])[:240],int(bool(edit.get('excluded'))),int(edit['position'])))
                if not conn.execute('SELECT count(*) FROM chunks WHERE excluded=0').fetchone()[0]:raise ValueError('Выберите хотя бы один фрагмент')
            ws.event(conn,'import','Книга подготовлена; оригинал сохранён' if mode=='auto' else 'Создана книга с ручной последовательной разметкой')
            conn.commit();conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:conn.close()
    return snapshot(name)


@router.get('/api/projects/{name}/chunks')
def chunks(name,q: str=''):
    conn,_=web().open_project_connection(name)
    try:
        rows=[dict(r) for r in conn.execute('SELECT position,section,status,active_version,reviewed_version,excluded,text_type,source_text,translated_text,length(source_text) chars FROM chunks ORDER BY position')]
        terms=conn.execute('SELECT * FROM terms').fetchall()
        dismissed={(r[0],r[1]) for r in conn.execute('SELECT version,code FROM dismissed')}
        for row in rows:
            source=row.pop('source_text');translated=row.pop('translated_text')
            row['warning_count']=sum((row['active_version'],item['code']) not in dismissed for item in ws.warnings(source,translated,terms))
            if q.strip():row['search_match']=q[:200].casefold() in source.casefold() or q[:200].casefold() in (translated or '').casefold()
        return {'chunks':rows}
    finally:conn.close()


@router.post('/api/projects/{name}/sample')
def sample_selection(name,body: dict):
    conn,_=web().open_project_connection(name)
    try:
        rows=conn.execute("SELECT * FROM chunks WHERE excluded=0 AND status IN ('pending','failed') ORDER BY position").fetchall()
        if body.get('positions') is not None:
            wanted=set(body['positions']);rows=[r for r in rows if r['position'] in wanted]
        return {'chunks':[{'position':r['position'],'section':r['section'],'preview':r['source_text'][:160]} for r in ws.choose_sample(rows)]}
    finally:conn.close()


@router.get('/api/projects/{name}/chunks/{position}')
def chunk(name,position: int):
    conn,_=web().open_project_connection(name)
    try:
        row=conn.execute('SELECT * FROM chunks WHERE position=?',(position,)).fetchone()
        if not row:raise HTTPException(404,'Фрагмент не найден')
        result=dict(row)
        result['note']=bt.get_meta(conn,f'note:{position}') or ''
        result['versions']=[dict(r) for r in conn.execute('SELECT * FROM versions WHERE position=? ORDER BY id DESC',(position,))]
        result['attempt_history']=[dict(r) for r in conn.execute('SELECT * FROM attempts WHERE position=? ORDER BY id DESC LIMIT 30',(position,))]
        dismissed={r[0] for r in conn.execute('SELECT code FROM dismissed WHERE version=?',(row['active_version'],))}
        result['warnings']=[{**v,'dismissed':v['code'] in dismissed} for v in ws.warnings(row['source_text'],row['translated_text'],conn.execute('SELECT * FROM terms').fetchall())]
        return result
    finally:conn.close()


@router.post('/api/projects/{name}/chunks/{position}')
def edit_chunk(name,position: int,body: dict):
    with ws.project_lock(web().project_paths(name)['db']):
        conn,_=web().open_project_connection(name)
        try:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM chunks WHERE position=?',(position,)).fetchone()
            if not row:raise HTTPException(404,'Фрагмент не найден')
            if row['active_version']!=body.get('expected_version'):raise ws.BusyError('Версия изменилась в другом окне. Скопируйте правку и обновите фрагмент.')
            action=body.get('action','save')
            if action=='save':ws.save_version(conn,position,str(body.get('text','')),'manual',body.get('expected_version'),True)
            elif action=='restore':
                version=conn.execute('SELECT text FROM versions WHERE id=? AND position=?',(body.get('version'),position)).fetchone()
                if not version:raise ValueError('Версия не найдена')
                ws.save_version(conn,position,version[0],'restore')
            elif action=='review':
                if not row['active_version']:raise ValueError('Сначала сохраните перевод')
                conn.execute('UPDATE chunks SET reviewed_version=active_version WHERE position=?',(position,));ws.event(conn,'review','Версия принята после вычитки',position)
            elif action=='dismiss':
                if not row['active_version']:raise ValueError('Сначала сохраните перевод')
                conn.execute('INSERT OR IGNORE INTO dismissed VALUES(?,?)',(row['active_version'],str(body.get('code'))));ws.event(conn,'dismiss','Предупреждение проверено пользователем',position)
            elif action=='note':
                note=str(body.get('note',''))[:4000]
                bt.set_meta(conn,f'note:{position}',note)
                ws.event(conn,'note','Заметка: '+note,position)
            elif action=='structure':
                conn.execute('UPDATE chunks SET section=?,excluded=? WHERE position=?',(str(body.get('section',row['section']))[:240],int(bool(body.get('excluded',row['excluded']))),position));ws.event(conn,'structure','Изменён раздел или состав книги',position)
                if not conn.execute('SELECT count(*) FROM chunks WHERE excluded=0').fetchone()[0]:
                    raise ValueError('В составе книги должен остаться хотя бы один фрагмент')
            else:raise ValueError('Неизвестное действие')
            conn.commit()
        finally:conn.close()
    return chunk(name,position)


@router.get('/api/projects/{name}/history')
def history(name):
    conn,paths=web().open_project_connection(name)
    try:return {'events':[dict(r) for r in conn.execute('SELECT * FROM events ORDER BY id DESC LIMIT 150')],
                'runs':[dict(r) for r in conn.execute('SELECT * FROM runs ORDER BY started_at DESC LIMIT 30')],
                'exports':[{'id':r['id'], 'created_at':r['created_at'], 'kind':r['kind'], 'manifest':r['manifest'],
                            'available': export_path(paths, name, r['path']) is not None}
                           for r in conn.execute('SELECT id,created_at,kind,manifest,path FROM exports ORDER BY created_at DESC LIMIT 30')],
                'terms':[dict(r) for r in conn.execute('SELECT * FROM terms ORDER BY source')]}
    finally:conn.close()


@router.post('/api/projects/{name}/settings')
def settings(name,body: dict):
    with ws.project_lock(web().project_paths(name)['db']):
        conn,_=web().open_project_connection(name)
        try:
            if 'display_name' in body:
                title=str(body['display_name']).strip()[:160]
                if not title:raise ValueError('Название не может быть пустым')
                bt.set_meta(conn,'display_name',title)
            if 'archived' in body:bt.set_meta(conn,'archived','1' if body['archived'] else '0')
            if 'term' in body:
                term=body['term']
                if not str(term.get('source','')).strip() or not str(term.get('target','')).strip():raise ValueError('Заполните оригинал и перевод термина')
                conn.execute('INSERT INTO terms(source,target) VALUES(?,?) ON CONFLICT(source) DO UPDATE SET target=excluded.target',(str(term['source']).strip()[:200],str(term['target']).strip()[:200]))
            if 'delete_term' in body:conn.execute('DELETE FROM terms WHERE id=?',(int(body['delete_term']),))
            ws.event(conn,'settings','Обновлены свойства книги или глоссарий');conn.commit()
        finally:conn.close()
    return snapshot(name)


def manual_source(conn,name):
    if bt.get_meta(conn,'segmentation_mode')!='manual':raise ValueError('Для этой книги используется автоматическое разделение')
    path=managed_source_path(name, web().project_paths(name))
    if not path.is_file():raise ValueError('Исходный текст книги не найден')
    profile=ws.effective_config(conn, web().config())
    source=path.read_text(encoding='utf-8')
    prepared,_=bt.preprocess_text(source,profile)
    if bt.sha256_text(prepared)!=bt.get_meta(conn,'source_sha256'):raise ValueError('Исходный текст изменился после импорта')
    return prepared,profile


@router.get('/api/projects/{name}/manual')
def manual_state(name):
    conn,_=web().open_project_connection(name)
    try:
        text,cfg=manual_source(conn,name);cursor=int(bt.get_meta(conn,'manual_cursor') or 0)
        rows=conn.execute('SELECT position,status,active_version,attempts,source_start,source_end,length(joiner_after) joiner_chars,length(source_text) chars FROM chunks ORDER BY position').fetchall()
        boundaries=[int(r['source_end'])+int(r['joiner_chars'] or 0) for r in rows]
        locked=max((b for r,b in zip(rows,boundaries) if r['status']=='processing' or r['active_version'] or r['attempts']),default=0)
        selected = running_positions(conn)
        locked = max([locked] + [b for r,b in zip(rows,boundaries) if r['position'] in selected])
        if web().manager.runtime_for(name)['active']:locked=max(locked,cursor)
        return {'text':text,'sha256':bt.sha256_text(text),'cursor':cursor,'boundaries':boundaries,
                'complete':bt.get_meta(conn,'manual_complete')=='1','locked_cursor':locked,**chunk_limits(cfg)}
    finally:conn.close()


def running_positions(conn):
    running = conn.execute('SELECT selection FROM runs WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1').fetchone()
    return set(json.loads(running[0])) if running else set()


@router.post('/api/projects/{name}/manual')
def update_manual(name,body: dict):
    # Deliberately does not take the long-lived translation file lock. SQLite WAL
    # serializes this short append with the worker while it translates older rows.
    conn,_=web().open_project_connection(name)
    try:
        text,cfg=manual_source(conn,name);conn.execute('BEGIN IMMEDIATE')
        cursor=int(bt.get_meta(conn,'manual_cursor') or 0)
        if body.get('expected_cursor')!=cursor:raise ws.BusyError('Разметка изменилась в другом окне. Обновите текст и повторите действие.')
        action=body.get('action','add')
        if action=='add':
            if bt.get_meta(conn,'manual_complete')=='1':raise ValueError('Разметка книги уже завершена')
            values=normalize_manual_boundaries(text,[body.get('offset')])
            if not values or values[0]<=cursor:raise ValueError('Новую границу нужно поставить после последнего готового фрагмента')
            end=values[0]
        elif action=='finish':
            if bt.get_meta(conn,'manual_complete')=='1':raise ValueError('Разметка книги уже завершена')
            end=len(text)
            if end<=cursor:raise ValueError('Весь текст уже размечен')
        elif action=='remove_last':
            row=conn.execute('SELECT * FROM chunks ORDER BY position DESC LIMIT 1').fetchone()
            if not row:raise ValueError('Нет границы, которую можно удалить')
            if row['status']=='processing' or row['active_version'] or row['attempts'] or row['position'] in running_positions(conn):
                raise ValueError('Последний фрагмент уже переводился, поэтому его граница защищена')
            conn.execute('DELETE FROM chunks WHERE position=?',(row['position'],))
            cursor=int(row['source_start'] or 0);bt.set_meta(conn,'manual_cursor',str(cursor));bt.set_meta(conn,'manual_complete','0')
            bt.set_meta(conn,'chunk_count',str(row['position']-1));ws.event(conn,'manual_boundary','Удалена последняя ручная граница')
            conn.commit();return manual_state(name)
        else:raise ValueError('Неизвестное действие разметки')
        position=conn.execute('SELECT coalesce(max(position),0)+1 FROM chunks').fetchone()[0]
        row=manual_rows(text,[end],cfg,start=cursor,position=position)[0]
        conn.execute('INSERT INTO chunks(position,section,source_text,joiner_after,estimated_tokens,source_start,source_end) VALUES(?,?,?,?,?,?,?)',row)
        bt.set_meta(conn,'manual_cursor',str(end));bt.set_meta(conn,'chunk_count',str(position))
        if action=='finish':bt.set_meta(conn,'manual_complete','1')
        ws.event(conn,'manual_boundary','Завершена ручная разметка книги' if action=='finish' else 'Добавлена ручная граница',position)
        conn.commit()
    finally:conn.close()
    return manual_state(name)


def export_download(name,kind):
    conn,paths=web().open_project_connection(name)
    try:
        conn.execute('BEGIN')
        result = ws.assemble_txt(conn, kind)
        if not result.text:
            raise ValueError('У книги ещё нет непрерывного переведённого начала.')
    finally:conn.close()
    filename = (paths['partial'] if kind == 'prefix' else paths['final']).name
    return Response(result.text, media_type='text/plain',
                    headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(filename)}"})


@router.post('/api/projects/{name}/export')
def make_export(name,body: dict):
    conn,paths=web().open_project_connection(name)
    try:
        export_id,path=ws.export_text(conn,paths['partial'].parent/name,body.get('kind','draft'),body.get('positions'))
        return {'id':export_id,'filename':path.name,'url':f'/api/projects/{name}/exports/{export_id}'}
    finally:conn.close()


def export_path(paths, name, saved):
    if not saved:
        return None
    directory = (paths['partial'].parent / name).resolve()
    candidate = (directory / saved).resolve()
    return candidate if candidate.parent == directory and candidate.is_file() else None


@router.get('/api/projects/{name}/exports/{export_id}')
def get_export(name,export_id):
    conn,paths=web().open_project_connection(name)
    try:row=conn.execute('SELECT path,sha256 FROM exports WHERE id=?',(export_id,)).fetchone()
    finally:conn.close()
    if not row:raise HTTPException(404,'Экспорт не найден')
    path=export_path(paths, name, row[0])
    if path is None:raise HTTPException(404,'Файл недоступен. Соберите экспорт заново.')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=row[1]:raise HTTPException(409,'Файл изменён вне приложения. Соберите новую версию.')
    return FileResponse(path,filename=path.name,media_type='text/plain; charset=utf-8')


@router.post('/api/projects/{name}/backup')
def make_backup(name):
    conn,paths=web().open_project_connection(name)
    try:
        source = managed_source_path(name, paths)
        if not source.is_file():
            source = bt.get_meta(conn,'source_path')
    finally:conn.close()
    path=ws.backup(paths['db'],source,Path(web().config()['_config_dir'])/'backups')
    return {'filename':path.name,'url':f'/api/backups/{path.name}'}


@router.get('/api/backups/{filename}')
def download_backup(filename):
    if Path(filename).name!=filename or not filename.startswith('book-backup-') or not filename.endswith('.zip'):raise HTTPException(400,'Некорректное имя')
    path=Path(web().config()['_config_dir'])/'backups'/filename
    if not path.is_file():raise HTTPException(404,'Копия не найдена')
    return FileResponse(path,filename=filename)


@router.post('/api/restore')
async def restore(file: UploadFile=File(...)):
    return await run_in_threadpool(restore_bytes,await read_upload(file,100*1024*1024))


def restore_bytes(raw):
    w=web()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            if sorted(z.namelist()) != ['manifest.json', 'source.txt', 'translation.db']:
                raise ValueError('Это не резервная копия Book Translator')
            if sum(i.file_size for i in z.infolist())>200*1024*1024:raise ValueError('Распакованный архив слишком большой')
            manifest=json.loads(z.read('manifest.json'))
            if not isinstance(manifest, dict) or manifest.get('format')!=1 or not isinstance(manifest.get('files'), dict):
                raise ValueError('Неподдерживаемый формат копии')
            data={name:z.read(name) for name in ('translation.db','source.txt')}
            for name,content in data.items():
                if hashlib.sha256(content).hexdigest()!=manifest['files'].get(name):raise ValueError('Контрольная сумма копии не совпадает')
        source_text = data['source.txt'].decode('utf-8-sig')
        if not source_text.strip() or '\x00' in source_text:
            raise ValueError('Исходный текст в резервной копии повреждён')
    except (zipfile.BadZipFile,KeyError,UnicodeError) as exc:raise ValueError('Это не резервная копия Book Translator') from exc
    name='book-'+uuid.uuid4().hex[:12];paths=w.project_paths(name)
    with stage_project(paths) as scratch:
        (scratch/'source.txt').write_bytes(data['source.txt'])
        tmp=scratch/'translation.db'
        tmp.write_bytes(data['translation.db'])
        conn=bt.connect_db(tmp)
        try:
            if conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('База повреждена')
            if conn.execute("SELECT count(*) FROM sqlite_master WHERE type IN ('trigger','view')").fetchone()[0]:raise ValueError('Неожиданные объекты базы в архиве')
            bt.init_schema(conn)
            ws.make_portable(conn)
            profile = ws.effective_config(conn, w.config())
            if bt.get_meta(conn, 'segmentation_mode') == 'manual':
                prepared, _ = bt.preprocess_text(source_text, profile)
                if bt.sha256_text(prepared) != bt.get_meta(conn, 'source_sha256'):
                    raise ValueError('Исходный текст не соответствует ручной разметке в копии')
            bt.set_meta(conn,'source_path',str((paths['project_dir']/'source.txt').resolve()))
            bt.set_meta(conn,'archived','0')
            ws.recover(conn);ws.event(conn,'restore_backup','Книга восстановлена как отдельный проект')
            conn.commit();conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:conn.close()
    return snapshot(name)


def install(app):
    @app.get('/')
    def home():return FileResponse(web().STATIC_DIR/'index.html')
    @app.middleware('http')
    async def local_access(request,call_next):
        if request.url.hostname not in ('127.0.0.1','localhost','::1','testserver'):
            return JSONResponse({'detail':'Допускается только локальное подключение'},status_code=403)
        origin=request.headers.get('origin')
        if origin and origin!=f'{request.url.scheme}://{request.url.netloc}':
            return JSONResponse({'detail':'Запрос из другого приложения отклонён'},status_code=403)
        if request.method not in ('GET','HEAD','OPTIONS') and request.headers.get('x-book-token')!=TOKEN:
            return JSONResponse({'detail':'Обновите страницу: сеанс приложения изменился'},status_code=403)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response
    @app.exception_handler(ws.BusyError)
    async def busy(request,exc):return JSONResponse({'detail':str(exc)},status_code=409)
    @app.exception_handler(ValueError)
    async def invalid(request,exc):return JSONResponse({'detail':str(exc)},status_code=400)
    @app.exception_handler(sqlite3.DatabaseError)
    async def db_error(request,exc):return JSONResponse({'detail':'Не удалось прочитать или сохранить книгу. Проверьте доступ к папке и резервную копию.'},status_code=500)
    @app.exception_handler(OSError)
    async def file_error(request, exc):
        return JSONResponse({'detail': 'Не удалось прочитать или записать файл. Проверьте доступ к папке и свободное место.'},
                            status_code=500)
    app.include_router(router)
