"""Persistent book workspace. Shared execution, versions, events and backups."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager, closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

SCHEMA_VERSION = 1


def now():
    return datetime.now(timezone.utc).isoformat()


def event(conn, kind, message, position=None):
    conn.execute('INSERT INTO events(at,kind,message,position) VALUES(?,?,?,?)',
                 (now(), kind, message, position))


def migrate(conn):
    version = conn.execute('PRAGMA user_version').fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError('Эта книга создана более новой версией приложения. Обновите переводчик.')
    if version == SCHEMA_VERSION:
        return
    conn.execute('BEGIN IMMEDIATE')
    try:
        # A second process may have completed migration while we waited.
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version == SCHEMA_VERSION:
            conn.commit()
            return
        if version > SCHEMA_VERSION:
            raise ValueError('База создана более новой версией приложения')
        for declaration in ['active_version INTEGER', 'reviewed_version INTEGER',
                            'excluded INTEGER NOT NULL DEFAULT 0', 'source_start INTEGER',
                            'source_end INTEGER', "text_type TEXT NOT NULL DEFAULT 'prose'"]:
            conn.execute('ALTER TABLE chunks ADD COLUMN ' + declaration)
        statements = [
            'CREATE TABLE versions(id INTEGER PRIMARY KEY,position INTEGER NOT NULL,text TEXT NOT NULL,kind TEXT NOT NULL,created_at TEXT NOT NULL,parent_id INTEGER)',
            'CREATE TABLE runs(id TEXT PRIMARY KEY,started_at TEXT NOT NULL,ended_at TEXT,state TEXT NOT NULL,profile TEXT NOT NULL,selection TEXT NOT NULL,message TEXT)',
            'CREATE TABLE attempts(id INTEGER PRIMARY KEY,run_id TEXT,position INTEGER NOT NULL,started_at TEXT NOT NULL,ended_at TEXT,status TEXT NOT NULL,error TEXT,response TEXT,prompt_tokens INTEGER,completion_tokens INTEGER,duration REAL)',
            'CREATE TABLE events(id INTEGER PRIMARY KEY,at TEXT NOT NULL,kind TEXT NOT NULL,message TEXT NOT NULL,position INTEGER)',
            'CREATE TABLE exports(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,kind TEXT NOT NULL,manifest TEXT NOT NULL,sha256 TEXT NOT NULL,path TEXT NOT NULL)',
            'CREATE TABLE terms(id INTEGER PRIMARY KEY,source TEXT NOT NULL UNIQUE,target TEXT NOT NULL)',
            'CREATE TABLE dismissed(version INTEGER NOT NULL,code TEXT NOT NULL,PRIMARY KEY(version,code))',
            'CREATE INDEX attempts_position ON attempts(position)',
        ]
        for sql in statements:
            conn.execute(sql)
        for row in conn.execute("SELECT position,translated_text,translated_at FROM chunks WHERE translated_text IS NOT NULL AND translated_text!=''").fetchall():
            cur = conn.execute('INSERT INTO versions(position,text,kind,created_at) VALUES(?,?,?,?)',
                               (row[0], row[1], 'imported', row[2] or now()))
            conn.execute('UPDATE chunks SET active_version=? WHERE position=?', (cur.lastrowid, row[0]))
        for row in conn.execute('SELECT position,section,source_text FROM chunks').fetchall():
            position, section, source = row
            label = kind = None
            if len(re.findall(r'(?im)^CHAPTER\s+\d+', source)) > 2:
                label, kind = 'Оглавление', 'contents'
            elif 'Original Transcriber' in source and len(source) < 1500:
                label, kind = 'Примечания к исходнику', 'frontmatter'
            if label:
                conn.execute('UPDATE chunks SET section=?,text_type=? WHERE position=?', (label,kind,position))
                event(conn,'structure',f'Уточнён автоматически распознанный раздел: {section} → {label}',position)
        event(conn, 'migration', 'Добавлена история версий. Существующие оригиналы и переводы сохранены.')
        conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


class BusyError(RuntimeError):
    pass


@contextmanager
def project_lock(db_path):
    """OS lock survives neither a crash nor process exit; shared by GUI and CLI."""
    path = Path(str(db_path) + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    file = path.open('a+b')
    try:
        if path.stat().st_size == 0:
            file.write(b'0'); file.flush()
        file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BusyError('Книга занята другим окном или выполняющимся действием. Повторите позже.') from exc
        try:
            yield
        finally:
            file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
    finally:
        file.close()


def is_busy(db_path):
    try:
        with project_lock(db_path):
            return False
    except BusyError:
        return True


def recover(conn):
    stamp = now()
    changed = conn.execute("UPDATE chunks SET status=CASE WHEN active_version IS NULL THEN 'pending' ELSE 'done' END,error='Предыдущая попытка прервана' WHERE status='processing'").rowcount
    conn.execute("UPDATE attempts SET status='interrupted',ended_at=?,error='Приложение завершилось до получения результата' WHERE status='processing'", (stamp,))
    runs = conn.execute("UPDATE runs SET state='interrupted',ended_at=?,message='Работа прервана при закрытии приложения' WHERE ended_at IS NULL", (stamp,)).rowcount
    if changed or runs:
        event(conn, 'recovery', 'Незавершённая работа восстановлена. Готовые версии сохранены.')
    conn.commit()


def recover_if_idle(conn, db_path):
    interrupted = conn.execute(
        """SELECT EXISTS(SELECT 1 FROM runs WHERE ended_at IS NULL)
               OR EXISTS(SELECT 1 FROM chunks WHERE status='processing')
               OR EXISTS(SELECT 1 FROM attempts WHERE status='processing')"""
    ).fetchone()[0]
    if interrupted:
        try:
            with project_lock(db_path):
                recover(conn)
        except BusyError:
            pass


PROFILE_KEYS = {
    'lmstudio': ('model', 'source_lang_code', 'target_lang_code'),
    'chunking': ('target_tokens', 'hard_max_tokens', 'chars_per_token'),
    'translation': ('max_output_tokens', 'max_retries', 'retry_backoff_seconds'),
    'validation': ('min_length_ratio', 'max_length_ratio'),
    'preprocessing': ('strip_gutenberg_wrapper',),
}


def clean_profile(cfg):
    """Book settings that may travel with a project."""
    if not isinstance(cfg, dict):
        raise ValueError('Некорректный профиль перевода')
    result = {}
    for section, keys in PROFILE_KEYS.items():
        values = cfg.get(section, {})
        if not isinstance(values, dict):
            raise ValueError('Некорректный профиль перевода')
        result[section] = {key: copy.deepcopy(values[key]) for key in keys if key in values}
    return result


def effective_config(conn, cfg):
    import book_translator as bt
    saved = bt.get_meta(conn, 'profile_json')
    if not saved:
        return cfg
    result = copy.deepcopy(cfg)
    for section, values in clean_profile(json.loads(saved)).items():
        result[section].update(values)
    bt.validate_config(result)
    return result


def project_config(conn, cfg):
    import book_translator as bt
    if bt.get_meta(conn, 'profile_json'):
        return effective_config(conn, cfg)
    bt.ensure_translation_profile(conn, cfg, str(cfg['lmstudio']['model']))
    bt.set_meta(conn, 'profile_json', json.dumps(clean_profile(cfg), ensure_ascii=False))
    conn.commit()
    return cfg


def save_version(conn, position, text, kind, expected=None, check=False):
    row = conn.execute('SELECT * FROM chunks WHERE position=?', (position,)).fetchone()
    if row is None:
        raise ValueError('Фрагмент не найден')
    if check and row['active_version'] != expected:
        raise BusyError('Текст изменён в другом окне. Скопируйте свою правку и обновите фрагмент.')
    if not text.strip():
        raise ValueError('Перевод не может быть пустым')
    cur = conn.execute('INSERT INTO versions(position,text,kind,created_at,parent_id) VALUES(?,?,?,?,?)',
                       (position, text, kind, now(), row['active_version']))
    conn.execute("UPDATE chunks SET translated_text=?,active_version=?,reviewed_version=NULL,status='done',error=NULL,translated_at=? WHERE position=?",
                 (text, cur.lastrowid, now(), position))
    event(conn, kind, 'Сохранена новая версия перевода', position)
    return cur.lastrowid


def warnings(source, text, terms=()):
    if not text:
        return []
    items = []
    if source.strip() == text.strip() and len(source) > 40:
        items.append({'code':'unchanged','message':'Текст совпадает с оригиналом. Проверьте, нужен ли здесь перевод.'})
    letters = re.findall(r'[A-Za-zА-Яа-яЁё]', text)
    if len(letters) > 80 and len(re.findall(r'[А-Яа-яЁё]', text)) / len(letters) < .15:
        items.append({'code':'language','message':'Большая часть текста написана латиницей. Возможны цитаты или непереведённые фрагменты.'})
    if sorted(re.findall(r'\b\d+\b', source)) != sorted(re.findall(r'\b\d+\b', text)):
        items.append({'code':'numbers','message':'Числа отличаются от оригинала. Проверьте: они могли быть записаны словами.'})
    if len(source) > 100 and len(text) / len(source) < .65:
        items.append({'code':'short','message':'Перевод заметно короче оригинала. Проверьте возможные пропуски.'})
    paragraphs = [x.strip() for x in text.split('\n\n') if len(x.strip()) > 60]
    if len(set(paragraphs)) < len(paragraphs):
        items.append({'code':'repeat','message':'В переводе повторяется целый абзац.'})
    for term in terms:
        if term['source'].casefold() in source.casefold() and term['target'].casefold() not in text.casefold():
            items.append({'code':f"term:{term['id']}", 'message':f"Проверьте термин «{term['source']}»: ожидается «{term['target']}». Возможна другая форма слова."})
    return items


def model_request_error(exc):
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        if status in (401, 403):
            return 'LM Studio отклонила доступ. Проверьте настройки подключения.', False
        if status == 429:
            return 'LM Studio занята. Повторите перевод через несколько секунд.', True
        if status >= 500 or status == 408:
            return f'Ошибка LM Studio ({status}). Проверьте сервер и повторите перевод.', True
        return f'LM Studio отклонила запрос ({status}). Проверьте модель и размер фрагмента.', False
    if isinstance(exc, requests.Timeout):
        return 'LM Studio не ответила вовремя. Проверьте состояние модели.', True
    if isinstance(exc, requests.ConnectionError):
        return 'Нет связи с LM Studio. Запустите локальный сервер и повторите перевод.', True
    return 'Не удалось получить ответ LM Studio. Проверьте подключение.', False


def translate_one(conn, row, client, model, cfg, logger, stop=None, run_id=None, progress=None):
    import book_translator as bt
    migrate(conn)
    stop = stop or threading.Event()
    position = row['position']
    source = row['source_text']
    last_error = ''
    with conn:
        conn.execute("UPDATE chunks SET status='processing',error=NULL WHERE position=?", (position,))
    for number in range(1, int(cfg['translation']['max_retries']) + 1):
        if stop.is_set():
            break
        stamp = now()
        with conn:
            cur = conn.execute("INSERT INTO attempts(run_id,position,started_at,status) VALUES(?,?,?,'processing')",
                               (run_id, position, stamp))
            attempt_id = cur.lastrowid
            conn.execute('UPDATE chunks SET attempts=attempts+1 WHERE position=?', (position,))
        if progress:
            progress('translating', f'Фрагмент {position}, попытка {number}', position=position, attempt=number, started_at=stamp)
        started = time.perf_counter()
        result = None
        p = c = None
        try:
            result, p, c = client.translate(model, source)
            issues = bt.validate_translation(source, result, cfg)
            if issues:
                raise bt.NonRetryableTranslationError('; '.join(issues))
        except bt.NonRetryableTranslationError as exc:
            last_error = str(exc)
            result = getattr(exc, 'response_text', result)
            retryable = False
        except requests.RequestException as exc:
            last_error, retryable = model_request_error(exc)
        else:
            duration = time.perf_counter() - started
            with conn:
                save_version(conn, position, result, 'machine')
                conn.execute('UPDATE chunks SET model=?,prompt_tokens=?,completion_tokens=?,duration_seconds=? WHERE position=?',
                             (model, p, c, duration, position))
                conn.execute("UPDATE attempts SET ended_at=?,status='done',response=?,prompt_tokens=?,completion_tokens=?,duration=? WHERE id=?",
                             (now(), result, p, c, duration, attempt_id))
            return 'done', f'{duration:.1f} с'
        logger.warning('Фрагмент %s, попытка %s: %s', position, number, last_error)
        with conn:
            conn.execute("UPDATE attempts SET ended_at=?,status='failed',error=?,response=?,prompt_tokens=?,completion_tokens=?,duration=? WHERE id=?",
                         (now(), last_error[:4000], result, p, c, time.perf_counter()-started, attempt_id))
            event(conn, 'attempt_failed', last_error[:2000], position)
        if not retryable or number == int(cfg['translation']['max_retries']):
            break
        delay = bt.retry_delay(cfg, number)
        if progress:
            progress('retry_wait', f'Фрагмент {position}: повтор через {delay:g} с', position=position, attempt=number, started_at=now())
        if stop.wait(delay):
            break
    interrupted = stop.is_set()
    with conn:
        conn.execute("UPDATE chunks SET status=CASE WHEN active_version IS NOT NULL THEN 'done' ELSE ? END,error=? WHERE position=?",
                     ('pending' if interrupted else 'failed',
                      'Попытка приостановлена' if interrupted else last_error[:4000], position))
    return ('paused', 'Приостановлено') if interrupted else ('failed', last_error)


def choose_sample(selected):
    prose = [r for r in selected if r['text_type'] == 'prose' and len(r['source_text']) > 100
             and not re.search(r'CONTENTS|ETYMOLOGY|EXTRACTS|Transcriber', r['section'], re.I)
             and len(re.findall(r'(?im)^CHAPTER\s+\d+', r['source_text'])) <= 1]
    long_prose = [r for r in prose if len(r['source_text']) > 500]
    candidates = long_prose if len(long_prose) >= 3 else prose or selected
    indices = sorted(set([0, len(candidates)//2, max(0, len(candidates)-1)]))
    return [candidates[i] for i in indices] if candidates else []


@dataclass(frozen=True)
class RunResult:
    state: str
    message: str


def run(db_path, cfg, stop=None, positions=None, sample=False, retranslate=False, progress=None):
    import book_translator as bt
    stop = stop or threading.Event()
    with project_lock(Path(cfg['_config_dir']) / '.translation-worker'), project_lock(db_path):
        conn = bt.connect_db(db_path)
        try:
            bt.init_schema(conn)
            recover(conn)
            cfg = project_config(conn, cfg)
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                if bt.get_meta(conn, 'archived') == '1':
                    raise ValueError('Сначала верните книгу из архива.')
                selected = conn.execute("SELECT * FROM chunks WHERE excluded=0 ORDER BY position").fetchall()
                if positions is not None:
                    wanted = set(positions)
                    selected = [r for r in selected if r['position'] in wanted]
                selected = [r for r in selected if retranslate or r['status'] in ('pending', 'failed')]
                if sample:
                    selected = choose_sample(selected)
                if not selected:
                    return RunResult('no_work', 'В выбранном диапазоне нет фрагментов для перевода.')
                run_id = uuid.uuid4().hex
                conn.execute("INSERT INTO runs(id,started_at,state,profile,selection) VALUES(?,?,'starting',?,?)",
                             (run_id, now(), json.dumps(clean_profile(cfg), ensure_ascii=False),
                              json.dumps([r['position'] for r in selected])))
                event(conn, 'start', f'Начат {"пробный " if sample else ""}перевод: {len(selected)} фрагментов')

            def update(phase, message, **detail):
                conn.execute('UPDATE runs SET state=?,message=? WHERE id=?', (phase, message, run_id))
                conn.commit()
                if progress:
                    progress(phase, message, **detail)
            state, message = 'complete', 'Выбранный объём переведён'
            try:
                logger = bt.setup_logging(Path(db_path).with_name('translation.log'))
                client = bt.LMStudioClient(cfg, logger)
                update('starting', 'Проверяем подключение к модели')
                model = client.resolve_model(cfg['lmstudio']['model'])
                for row in selected:
                    if stop.is_set():
                        state, message = 'paused', 'Перевод приостановлен'; break
                    status, detail = translate_one(conn, row, client, model, cfg, logger, stop, run_id, update)
                    if status == 'failed':
                        state, message = status, f'Фрагмент {row["position"]}. {detail}'
                        break
                    if status == 'paused':
                        state, message = status, detail
                        break
                if stop.is_set() and state == 'complete':
                    state, message = 'paused', 'Результат сохранён. Перевод приостановлен.'
            except requests.RequestException as exc:
                state, message = 'failed', model_request_error(exc)[0]
            except BaseException as exc:
                state, message = ('interrupted', 'Остановлено пользователем') if isinstance(exc, KeyboardInterrupt) else ('failed', str(exc))
                raise
            finally:
                conn.execute('UPDATE runs SET ended_at=?,state=?,message=? WHERE id=?', (now(), state, message, run_id))
                event(conn, state, message)
                conn.commit()
                if progress:
                    progress(state, message)
                if 'client' in locals():
                    client.session.close()
                if 'logger' in locals():
                    for handler in logger.handlers:
                        handler.close()
                    logger.handlers.clear()
                recover(conn)
            return RunResult(state, message)
        finally:
            conn.close()


@dataclass(frozen=True)
class TextExport:
    text: str
    manifest: list[dict]
    total: int


def assemble_txt(conn, kind='draft', positions=None):
    """Read a consistent selection of saved versions without writing files."""
    import book_translator as bt
    if kind not in ('draft', 'reviewed', 'prefix'):
        raise ValueError('Неизвестный вид экспорта')
    if bt.get_meta(conn,'segmentation_mode')=='manual' and bt.get_meta(conn,'manual_complete')!='1' and kind in ('draft','reviewed'):
        raise ValueError('Ручная разметка ещё не завершена. Пока можно сохранить только непрерывное переведённое начало.')
    rows = conn.execute('SELECT position, excluded, active_version, reviewed_version, translated_text, joiner_after '
                        'FROM chunks ORDER BY position').fetchall()
    if positions is not None:
        if not isinstance(positions, list) or not positions or any(type(p) is not int or p < 1 for p in positions):
            raise ValueError('Укажите номера фрагментов для экспорта')
        wanted = set(positions)
        if not wanted <= {row['position'] for row in rows}:
            raise ValueError('В диапазоне экспорта есть несуществующие фрагменты')
        rows = [row for row in rows if row['position'] in wanted]
    rows = [row for row in rows if not row['excluded']]
    total = len(rows)
    def has_translation(row):
        return row['active_version'] is not None and bool((row['translated_text'] or '').strip())

    if kind == 'prefix':
        prefix = []
        for row in rows:
            if not has_translation(row):
                break
            prefix.append(row)
        rows = prefix
    elif not rows or any(not has_translation(row) for row in rows):
        raise ValueError('Выбранный состав ещё не переведён. Выберите готовые главы или непрерывное начало.')

    if kind == 'reviewed' and any(row['active_version'] != row['reviewed_version'] for row in rows):
        raise ValueError('Не все выбранные фрагменты приняты после вычитки.')
    text = ''.join(row['translated_text'].rstrip() + row['joiner_after'] for row in rows).rstrip()
    manifest = [{'position': row['position'], 'version': row['active_version'],
                 'reviewed': row['active_version'] == row['reviewed_version']} for row in rows]
    return TextExport(text + '\n' if text else '', manifest, total)


def export_text(conn, directory, kind='draft', positions=None):
    import book_translator as bt
    with conn:
        conn.execute('BEGIN')
        result = assemble_txt(conn, kind, positions)
    if not result.text:
        raise ValueError('У книги ещё нет непрерывного переведённого начала.')
    export_id = uuid.uuid4().hex
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    title = bt.get_meta(conn, 'display_name') or Path(directory).name
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title).strip(' .')[:100] or 'book'
    suffix = {'draft': 'ru', 'reviewed': 'ru_reviewed', 'prefix': 'ru_partial'}[kind]
    path = Path(directory) / f'{title}_{suffix}_{stamp}_{export_id[:6]}.txt'
    bt.atomic_write_text(path, result.text)
    try:
        with conn:
            digest = hashlib.sha256(result.text.encode('utf-8')).hexdigest()
            conn.execute('INSERT INTO exports VALUES(?,?,?,?,?,?)',
                         (export_id, now(), kind, json.dumps(result.manifest), digest, path.name))
            event(conn, 'export', f'Сохранён TXT ({len(result.manifest)} фрагм.)')
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return export_id, path


def make_portable(conn):
    """Retain book history without including runtime or filesystem locations."""
    import book_translator as bt
    profile = bt.get_meta(conn, 'profile_json')
    if profile:
        bt.set_meta(conn, 'profile_json', json.dumps(clean_profile(json.loads(profile)), ensure_ascii=False))
    for run_id, profile in conn.execute('SELECT id,profile FROM runs').fetchall():
        conn.execute('UPDATE runs SET profile=? WHERE id=?',
                     (json.dumps(clean_profile(json.loads(profile)), ensure_ascii=False), run_id))
    bt.set_meta(conn, 'source_path', 'source.txt')
    conn.execute("UPDATE exports SET path='' WHERE path<>''")


def backup(db_path, source_path, directory):
    """Create a self-contained archive; read backup back before returning it."""
    import book_translator as bt
    Path(directory).mkdir(parents=True, exist_ok=True)
    target = Path(directory) / f'book-backup-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.zip'
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro', uri=True)) as src, closing(sqlite3.connect(root/'translation.db')) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Проверка резервной копии не пройдена')
            dst.row_factory = sqlite3.Row
            bt.init_schema(dst)
            with dst:
                make_portable(dst)
        if source_path and Path(source_path).exists():
            shutil.copy2(source_path, root/'source.txt')
        else:
            with closing(sqlite3.connect(root/'translation.db')) as db:
                db.row_factory = sqlite3.Row
                if bt.get_meta(db, 'segmentation_mode') == 'manual':
                    raise ValueError('Исходник ручной разметки недоступен. Верните файл книги перед копированием.')
                text = ''.join(r[0]+r[1] for r in db.execute('SELECT source_text,joiner_after FROM chunks ORDER BY position'))
                if not text.strip():
                    raise ValueError('Исходный текст недоступен')
                (root/'source.txt').write_text(text, encoding='utf-8')
        hashes = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}
        (root/'manifest.json').write_text(json.dumps({'format':1,'files':hashes},indent=2),encoding='utf-8')
        staging = target.with_suffix('.tmp')
        try:
            with zipfile.ZipFile(staging, 'w', zipfile.ZIP_DEFLATED) as z:
                for p in root.iterdir():
                    z.write(p,p.name)
            with zipfile.ZipFile(staging) as z:
                for name,digest in hashes.items():
                    if hashlib.sha256(z.read(name)).hexdigest()!=digest:
                        raise ValueError('Архив не прошёл проверку')
            staging.replace(target)
        finally:
            staging.unlink(missing_ok=True)
    return target
