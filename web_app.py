from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import book_translator as bt
import workspace as ws


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
STATIC_DIR = BASE_DIR / "static"
SOURCES_DIR = BASE_DIR / "sources"


def config() -> dict:
    return bt.load_config(CONFIG_PATH)


def project_paths(name: str, source_path: Optional[Path] = None) -> dict:
    if name in ("", ".", "..") or any(ch in name for ch in ("/", "\\")):
        raise HTTPException(400, "Некорректное имя проекта")
    source = source_path or SOURCES_DIR / f"{name}.txt"
    return bt.resolve_paths(source, config(), name)


def open_project_connection(name: str) -> tuple[sqlite3.Connection, dict]:
    paths = project_paths(name)
    if not paths["db"].exists():
        raise HTTPException(404, "Проект не найден")
    conn = bt.connect_db(paths["db"])
    try:
        bt.init_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn, paths


def project_snapshot(name: str) -> dict:
    from web_features import snapshot
    return snapshot(name)


class TranslationManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._active_project: Optional[str] = None
        self._phase = "idle"
        self._message = ""

    def runtime_for(self, name: str) -> dict:
        with self._lock:
            active = self._active_project == name and self._thread is not None and self._thread.is_alive()
            return {
                "active": active,
                "phase": self._phase if active or self._active_project == name else "idle",
                "message": self._message if self._active_project == name else "",
                "stop_requested": active and self._stop.is_set(),
                "other_project": self._active_project if self._active_project and self._active_project != name and self._thread and self._thread.is_alive() else None,
            }

    def start(self, name: str, max_chunks: Optional[int] = None, positions=None, retranslate=False) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                if self._active_project == name:
                    raise HTTPException(409, "Перевод уже выполняется")
                raise HTTPException(409, f"Сейчас переводится другой проект: {self._active_project}")
            if ws.is_busy(Path(config()['_config_dir']) / '.translation-worker'):
                raise HTTPException(409, 'Перевод уже выполняется в другом окне или из командной строки')
            self._positions = positions
            self._retranslate = retranslate
            self._stop.clear()
            self._active_project = name
            self._phase = "starting"
            self._message = "Подключаемся к LM Studio"
            self._thread = threading.Thread(
                target=self._run,
                args=(name, max_chunks),
                daemon=True,
                name=f"translate-{name}",
            )
            self._thread.start()

    def request_stop(self, name: str) -> None:
        with self._lock:
            if not self._thread or not self._thread.is_alive() or self._active_project != name:
                raise HTTPException(409, "Этот проект сейчас не переводится")
            self._stop.set()
            self._phase = "stopping"
            self._message = "Завершаем текущий блок и сохраняем результат"

    def _set_state(self, phase: str, message: str) -> None:
        with self._lock:
            if self._stop.is_set() and phase in ('starting', 'translating', 'retry_wait'):
                return
            self._phase = phase
            self._message = message

    def _run(self, name, max_chunks):
        def update(phase, message, **detail):
            self._set_state(phase, message)
        try:
            result = ws.run(project_paths(name)['db'], config(), self._stop,
                            positions=self._positions, sample=max_chunks is not None,
                            retranslate=self._retranslate, progress=update)
            self._set_state(result.state, result.message)
        except Exception as exc:
            self._set_state('failed', str(exc))

    def shutdown(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


manager = TranslationManager()
app = FastAPI(title="Book Translator", docs_url=None, redoc_url=None)


@app.get("/api/model")
def model_status(name: Optional[str] = None) -> dict:
    cfg = config()
    if name:
        conn, _ = open_project_connection(name)
        try:
            cfg = ws.effective_config(conn, cfg)
        finally:
            conn.close()
    model = str(cfg["lmstudio"]["model"])
    client = bt.LMStudioClient(cfg, logging.getLogger("book_translator.ui.model_check"))
    client.connect_timeout = min(client.connect_timeout, 3)
    client.read_timeout = min(client.read_timeout, 4)

    def status(state, message):
        return {'ready': state == 'ready', 'state': state, 'model': model, 'message': message,
                'address': client.base_url}

    try:
        available = client.list_models()
        if model not in available:
            return status('missing', f'Загрузите модель «{model}» в LM Studio.')
        loaded = client.list_loaded_models()
        if loaded is None:
            return status('unknown_load', 'Модель найдена. Проверьте, что она загружена, и запустите пробу.')
        if model not in loaded:
            return status('unloaded', f'Загрузите модель «{model}» в LM Studio и проверьте связь.')
        return status('ready', 'LM Studio готова')
    except requests.RequestException as exc:
        return status('offline', ws.model_request_error(exc)[0])
    except (ValueError, AttributeError, TypeError):
        return status('unverified', 'Не удалось проверить модель. Проверьте адрес сервера LM Studio.')
    finally:
        client.session.close()


@app.get("/api/projects")
def list_projects() -> dict:
    cfg = config()
    root = (Path(cfg["_config_dir"]) / cfg["paths"]["projects_dir"]).resolve()
    projects = []
    errors = []
    if root.exists():
        for db in sorted(root.glob("*/translation.db"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                projects.append(project_snapshot(db.parent.name))
            except (sqlite3.Error, OSError, ValueError, HTTPException) as exc:
                errors.append({'name': db.parent.name, 'message': str(exc)})
    return {"projects": projects, 'errors': errors}


@app.post("/api/projects", status_code=201)
async def create_project(file: UploadFile = File(...), display_name: str = Form("")) -> dict:
    from web_features import import_upload
    return await import_upload(file, display_name)


@app.get("/api/projects/{name}")
def get_project(name: str) -> dict:
    return project_snapshot(name)


@app.post("/api/projects/{name}/start")
def start_translation(name: str, test: bool = False, body: dict | None = None) -> dict:
    snap = project_snapshot(name)
    if snap['archived']:
        raise HTTPException(409, 'Сначала верните книгу из архива')
    body = body or {}
    positions = body.get('positions')
    if positions is not None and (not isinstance(positions, list) or not positions or any(type(p) != int or p < 1 or p > snap['all_total'] for p in positions)):
        raise HTTPException(400, 'Выберите существующие фрагменты книги')
    manager.start(name, max_chunks=3 if test or body.get('sample') else None,
                  positions=body.get('positions'), retranslate=bool(body.get('retranslate')))
    return {"ok": True}


@app.post("/api/projects/{name}/stop")
def stop_translation(name: str) -> dict:
    manager.request_stop(name)
    return {"ok": True}


@app.post("/api/projects/{name}/retry")
def retry_failed(name: str) -> dict:
    with ws.project_lock(project_paths(name)['db']):
        conn, paths = open_project_connection(name)
        try:
            return {'ok': True, 'count': bt.reset_for_retry(conn)}
        finally:
            conn.close()


@app.get("/api/projects/{name}/partial")
def download_partial(name: str):
    from web_features import export_download
    return export_download(name, 'prefix')


@app.get("/api/projects/{name}/final")
def download_final(name: str):
    from web_features import export_download
    return export_download(name, 'draft')


@app.post("/api/projects/{name}/open-output")
def open_output_folder(name: str) -> dict:
    conn, paths = open_project_connection(name)
    conn.close()
    folder = paths['partial'].parent / name
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(folder)  # type: ignore[attr-defined]
        return {"ok": True}
    raise HTTPException(501, "Открытие папки поддерживается только в Windows")


from web_features import install
install(app)

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
