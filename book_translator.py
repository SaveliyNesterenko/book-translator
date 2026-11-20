#!/usr/bin/env python3
"""Resumable EN -> RU book translator for TranslateGemma in LM Studio.

Pipeline:
  TXT -> paragraph-aware chunks -> SQLite queue -> LM Studio /v1/chat/completions
      -> technical checks/retries -> partial/final TXT assembly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import requests
import yaml
from tqdm import tqdm

APP_VERSION = "3.1.0"
DEFAULT_CONFIG = Path(__file__).with_name("config.yaml")


# ---------------------------- Configuration ----------------------------


def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    cfg.setdefault("lmstudio", {})
    cfg.setdefault("translation", {})
    cfg.setdefault("chunking", {})
    cfg.setdefault("validation", {})
    cfg.setdefault("paths", {})
    cfg.setdefault("preprocessing", {})

    cfg["lmstudio"].setdefault("base_url", "http://localhost:1234/v1")
    cfg["lmstudio"].setdefault("api_key", "lm-studio")
    cfg["lmstudio"].setdefault("model", "translategemma-12b-it")
    cfg["lmstudio"].setdefault("source_lang_code", "en")
    cfg["lmstudio"].setdefault("target_lang_code", "ru")
    cfg["lmstudio"].setdefault("connect_timeout_seconds", 10)
    cfg["lmstudio"].setdefault("read_timeout_seconds", 600)

    cfg["translation"].setdefault("max_output_tokens", 4096)
    cfg["translation"].setdefault("max_retries", 5)
    cfg["translation"].setdefault("retry_backoff_seconds", [5, 15, 30, 60, 120])

    cfg["chunking"].setdefault("target_tokens", 1500)
    cfg["chunking"].setdefault("chars_per_token", 3.7)
    cfg["chunking"].setdefault("hard_max_tokens", 1900)

    cfg["validation"].setdefault("min_length_ratio", 0.45)
    cfg["validation"].setdefault("max_length_ratio", 2.2)

    cfg["paths"].setdefault("projects_dir", "projects")
    cfg["paths"].setdefault("output_dir", "output")
    cfg["preprocessing"].setdefault("strip_gutenberg_wrapper", True)

    # Relative project/output paths must not depend on the shell's current
    # directory. Keep them anchored next to the selected configuration file.
    cfg["_config_dir"] = str(path.resolve().parent)

    validate_config(cfg)
    return cfg


def validate_config(cfg: dict) -> None:
    try:
        translation, lmstudio = cfg['translation'], cfg['lmstudio']
        chunking, validation = cfg['chunking'], cfg['validation']
        if not 1 <= int(translation['max_retries']) <= 10:
            raise ValueError('Количество попыток должно быть от 1 до 10')
        if not isinstance(translation['retry_backoff_seconds'], list) or any(
            not 0 <= float(delay) <= 600 for delay in translation['retry_backoff_seconds']
        ):
            raise ValueError('Некорректные интервалы повторных попыток')
        if not 1 <= float(lmstudio['read_timeout_seconds']) <= 600:
            raise ValueError('Ожидание ответа должно быть от 1 до 600 секунд')
        if not 1 <= int(translation['max_output_tokens']) <= 32768:
            raise ValueError('Некорректный лимит выходных токенов')
        if not 0 < int(chunking['target_tokens']) <= int(chunking['hard_max_tokens']):
            raise ValueError('Некорректный размер фрагмента')
        chars_per_token = float(chunking['chars_per_token'])
        if not math.isfinite(chars_per_token) or chars_per_token <= 0:
            raise ValueError('Некорректная оценка размера фрагмента')
        low, high = float(validation['min_length_ratio']), float(validation['max_length_ratio'])
        if not 0 < low <= high or not math.isfinite(high):
            raise ValueError('Некорректное соотношение длины текста')
        if any(not isinstance(lmstudio[key], str) or not lmstudio[key].strip()
               for key in ('model', 'source_lang_code', 'target_lang_code')):
            raise ValueError('Укажите модель и языки перевода')
        if type(cfg['preprocessing']['strip_gutenberg_wrapper']) is not bool:
            raise ValueError('Некорректная настройка очистки исходника')
    except (TypeError, OverflowError, KeyError):
        raise ValueError('Некорректные параметры профиля перевода') from None


# ---------------------------- Text parsing ----------------------------


@dataclass
class TextUnit:
    text: str
    separator_after: str
    section: str = ""
    is_heading: bool = False


@dataclass
class Chunk:
    position: int
    source_text: str
    joiner_after: str
    section: str
    estimated_tokens: int


def read_text_file(path: Path) -> str:
    # utf-8-sig transparently removes a possible BOM.
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{path} is not UTF-8. Convert the book to UTF-8 TXT first. Original error: {exc}"
        ) from exc


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Keep paragraph semantics but normalize trailing whitespace.
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def preprocess_text(text: str, cfg: dict) -> tuple[str, list[str]]:
    """Normalize source text and remove a complete Project Gutenberg wrapper."""
    text = normalize_text(text)
    notes: list[str] = []
    if not cfg.get("preprocessing", {}).get("strip_gutenberg_wrapper", True):
        return text, notes

    start = text.find("*** START OF THE PROJECT GUTENBERG")
    end = text.find("*** END OF THE PROJECT GUTENBERG")
    if start >= 0 and end > start:
        content_start = text.find("\n", start)
        if content_start >= 0:
            original_length = len(text)
            text = text[content_start + 1 : end].strip()
            notes.append(
                f"Removed Project Gutenberg wrapper ({original_length - len(text)} characters)."
            )
    return text, notes


def estimate_tokens(text: str, chars_per_token: float) -> int:
    if not text:
        return 0
    return max(1, round(len(text) / max(chars_per_token, 0.5)))


def looks_like_heading(paragraph: str) -> bool:
    p = paragraph.strip()
    if not p or len(p) > 240:
        return False
    one_line = re.sub(r"\s+", " ", p)
    if re.match(
        r"^(chapter|part|book|prologue|epilogue|introduction|preface|interlude|appendix)\b",
        one_line,
        flags=re.IGNORECASE,
    ):
        return True
    # Short all-caps headings are common in public-domain TXT books.
    letters = [c for c in one_line if c.isalpha()]
    return len(letters) >= 3 and one_line.upper() == one_line


SENTENCE_BREAK = re.compile(r'''[.!?…]+["'”’»)\]]*(?=\s)\s*''')
PARAGRAPH_BREAK = re.compile(r"(\n(?:[^\S\n]*\n)+)")


def split_oversized_paragraph(paragraph: str, max_chars: int) -> list[str]:
    """Prefer sentence and word boundaries while preserving the original text."""
    if max_chars < 1:
        raise ValueError("Размер фрагмента должен быть положительным")
    pieces = []
    start = 0
    while len(paragraph) - start > max_chars:
        window = paragraph[start:start + max_chars]
        sentence_end = max((m.end() for m in SENTENCE_BREAK.finditer(window)), default=0)
        if sentence_end and sentence_end >= max_chars // 2:
            cut = sentence_end
        else:
            cut = max((m.end() for m in re.finditer(r"\s+", window)), default=max_chars)
        pieces.append(paragraph[start:start + cut])
        start += cut
    if start < len(paragraph):
        pieces.append(paragraph[start:])
    return pieces


def text_to_units(text: str, max_chars: int) -> list[TextUnit]:
    blocks = PARAGRAPH_BREAK.split(text)
    units: list[TextUnit] = []
    current_section = ""

    for index in range(0, len(blocks), 2):
        paragraph = blocks[index]
        separator = blocks[index + 1] if index + 1 < len(blocks) else ""
        heading = looks_like_heading(paragraph)
        if heading:
            current_section = re.sub(r"\s+", " ", paragraph.strip())

        pieces = split_oversized_paragraph(paragraph, max_chars)
        for piece_index, piece in enumerate(pieces):
            body = piece.rstrip()
            joiner = piece[len(body):]
            if piece_index == len(pieces) - 1:
                joiner += separator
            if body:
                units.append(TextUnit(body, joiner, current_section, heading and piece_index == 0))
            elif units:
                units[-1].separator_after += joiner
    return units


def build_chunks(text: str, cfg: dict) -> list[Chunk]:
    c = cfg["chunking"]
    chars_per_token = float(c["chars_per_token"])
    target_tokens = int(c["target_tokens"])
    hard_max_tokens = int(c["hard_max_tokens"])
    if target_tokens <= 0 or hard_max_tokens < target_tokens:
        raise ValueError("chunking.target_tokens must be > 0 and <= hard_max_tokens")

    target_chars = max(200, int(target_tokens * chars_per_token))
    hard_max_chars = max(target_chars, int(hard_max_tokens * chars_per_token))
    units = text_to_units(text, hard_max_chars)

    chunks: list[Chunk] = []
    bucket: list[TextUnit] = []
    bucket_chars = 0

    def flush() -> None:
        nonlocal bucket_chars
        if not bucket:
            return
        last = bucket[-1]
        body = "".join(unit.text + unit.separator_after for unit in bucket[:-1]) + last.text
        chunks.append(
            Chunk(
                position=len(chunks) + 1,
                source_text=body,
                joiner_after=last.separator_after,
                section=last.section,
                estimated_tokens=estimate_tokens(body, chars_per_token),
            )
        )
        bucket.clear()
        bucket_chars = 0

    for index, unit in enumerate(units):
        # A real section heading is normally followed by prose. Consecutive
        # headings are usually a table of contents and should not create dozens
        # of tiny chunks.
        next_is_heading = index + 1 < len(units) and units[index + 1].is_heading
        if bucket:
            candidate_chars = bucket_chars + len(bucket[-1].separator_after) + len(unit.text)
            if (unit.is_heading and not next_is_heading) or candidate_chars > target_chars:
                flush()
        bucket_chars += (len(bucket[-1].separator_after) if bucket else 0) + len(unit.text)
        bucket.append(unit)

    flush()
    return chunks


# ---------------------------- Project / SQLite ----------------------------


def safe_project_name(book_path: Path) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", book_path.stem).strip("._")
    return name or "book"


def resolve_paths(book_path: Path, cfg: dict, project_name: Optional[str] = None) -> dict:
    base = Path(cfg.get("_config_dir", Path.cwd()))
    projects_dir = (base / cfg["paths"]["projects_dir"]).resolve()
    output_dir = (base / cfg["paths"]["output_dir"]).resolve()
    name = project_name or safe_project_name(book_path)
    if name in ("", ".", "..") or any(ch in name for ch in ("/", "\\")):
        raise ValueError("project-name must be a single safe directory name")
    project_dir = projects_dir / name
    return {
        "project_name": name,
        "project_dir": project_dir,
        "db": project_dir / "translation.db",
        "log": project_dir / "translation.log",
        "partial": output_dir / f"{name}_ru.partial.txt",
        "final": output_dir / f"{name}_ru.txt",
    }


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    from workspace import SCHEMA_VERSION
    version = conn.execute('PRAGMA user_version').fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError('Книга создана более новой версией приложения')
    if version == SCHEMA_VERSION:
        return
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position INTEGER NOT NULL UNIQUE,
            section TEXT NOT NULL DEFAULT '',
            source_text TEXT NOT NULL,
            joiner_after TEXT NOT NULL DEFAULT '\n\n',
            estimated_tokens INTEGER NOT NULL DEFAULT 0,
            translated_text TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            model TEXT,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            duration_seconds REAL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            translated_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_status ON chunks(status);
        """
    )
    conn.commit()
    from workspace import migrate
    migrate(conn)


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def prepare_project(
    book_path: Path,
    cfg: dict,
    paths: dict,
    rebuild: bool = False,
) -> sqlite3.Connection:
    if not rebuild and paths['db'].is_file():
        from workspace import effective_config
        with closing(connect_db(paths['db'])) as existing:
            init_schema(existing)
            cfg = effective_config(existing, cfg)
    text, preprocessing_notes = preprocess_text(read_text_file(book_path), cfg)
    if not text:
        raise ValueError("The source TXT is empty.")
    source_hash = sha256_text(text)

    if rebuild:
        if paths['db'].exists():
            from workspace import backup
            backup(paths['db'], book_path, Path(cfg['_config_dir']) / 'backups')
        for db_file in (
            paths["db"],
            Path(str(paths["db"]) + "-wal"),
            Path(str(paths["db"]) + "-shm"),
        ):
            if db_file.exists():
                db_file.unlink()

    conn = connect_db(paths["db"])
    init_schema(conn)

    old_hash = get_meta(conn, "source_sha256")
    if old_hash:
        if old_hash != source_hash:
            conn.close()
            raise RuntimeError(
                "This project already exists, but the source TXT has changed. "
                "Use --rebuild to recreate its chunks/database, or --project-name for a new project."
            )
    else:
        chunks = build_chunks(text, cfg)
        if not chunks:
            conn.close()
            raise ValueError("Could not create any chunks from the source text.")
        conn.executemany(
            """
            INSERT INTO chunks(position,section,source_text,joiner_after,estimated_tokens)
            VALUES(?,?,?,?,?)
            """,
            [
                (c.position, c.section, c.source_text, c.joiner_after, c.estimated_tokens)
                for c in chunks
            ],
        )
        set_meta(conn, "source_sha256", source_hash)
        set_meta(conn, "source_path", str(book_path.resolve()))
        set_meta(conn, "app_version", APP_VERSION)
        set_meta(conn, "chunk_count", str(len(chunks)))
        set_meta(conn, "preprocessing_notes", json.dumps(preprocessing_notes, ensure_ascii=False))
        conn.commit()

    return conn


def setup_logging(log_path: Path, verbose: bool = False) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("book_translator")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)

    # Console output is intentionally sparse because tqdm owns the terminal line.
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(sh)
    return logger


# ---------------------------- LM Studio ----------------------------


class NonRetryableTranslationError(RuntimeError):
    """A repeated identical request cannot fix this response failure."""


class LMStudioClient:
    def __init__(self, cfg: dict, logger: logging.Logger):
        self.cfg = cfg
        self.base_url = str(cfg["lmstudio"]["base_url"]).rstrip("/")
        self.api_key = str(cfg["lmstudio"].get("api_key") or "lm-studio")
        self.connect_timeout = float(cfg["lmstudio"]["connect_timeout_seconds"])
        self.read_timeout = float(cfg["lmstudio"]["read_timeout_seconds"])
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
        )

    def list_models(self) -> list[str]:
        r = self.session.get(
            f"{self.base_url}/models",
            timeout=(self.connect_timeout, min(self.read_timeout, 60)),
        )
        r.raise_for_status()
        data = r.json()
        return [str(item.get("id")) for item in data.get("data", []) if item.get("id")]

    def list_loaded_models(self) -> Optional[list[str]]:
        """Return loaded chat-capable models when LM Studio's native API is available."""
        server_root = re.sub(r"/v1$", "", self.base_url)
        try:
            r = self.session.get(
                f"{server_root}/api/v0/models",
                timeout=(self.connect_timeout, min(self.read_timeout, 30)),
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError):
            return None
        return [
            str(item["id"])
            for item in data.get("data", [])
            if item.get("id")
            and item.get("state") == "loaded"
            and item.get("type") in ("llm", "vlm")
        ]

    def resolve_model(self, configured: str) -> str:
        models = self.list_models()
        if not configured or configured.lower() == "auto":
            raise RuntimeError("Укажите идентификатор модели TranslateGemma в config.yaml.")
        if configured not in models:
            raise RuntimeError(
                f"Модель «{configured}» недоступна. Загрузите её в LM Studio и повторите перевод."
            )
        return configured

    def translate(
        self, model: str, source_text: str
    ) -> tuple[str, Optional[int], Optional[int]]:
        """Translate one block with TranslateGemma's required chat template."""
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "source_lang_code": str(
                                self.cfg["lmstudio"].get("source_lang_code", "en")
                            ),
                            "target_lang_code": str(
                                self.cfg["lmstudio"].get("target_lang_code", "ru")
                            ),
                            "text": source_text,
                        }
                    ],
                }
            ],
            "temperature": 0.0,
            "max_tokens": int(self.cfg["translation"]["max_output_tokens"]),
            "stream": False,
        }
        r = self.session.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            timeout=(self.connect_timeout, self.read_timeout),
        )
        r.raise_for_status()
        try:
            data = r.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
        except (ValueError, AttributeError, KeyError, IndexError, TypeError):
            raise NonRetryableTranslationError("LM Studio вернула ответ в неизвестном формате.") from None
        if finish_reason == "length":
            exc = NonRetryableTranslationError(
                "Модель обрезала перевод. Попробуйте уменьшить размер фрагмента."
            )
            exc.response_text = content if isinstance(content, str) else None
            raise exc
        if not isinstance(content, str):
            raise NonRetryableTranslationError("LM Studio вернула ответ без текста.")
        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        return (content.strip(), prompt if type(prompt) is int else None,
                completion if type(completion) is int else None)


# ---------------------------- Translation logic ----------------------------


def validate_translation(
    source: str,
    translation: str,
    cfg: dict,
) -> list[str]:
    """Catch only unusable output, not literary or linguistic imperfections."""
    v = cfg["validation"]
    if not translation.strip():
        return ["Модель вернула пустой перевод."]
    issues: list[str] = []
    ratio = len(translation) / max(len(source), 1)
    if ratio < float(v["min_length_ratio"]):
        issues.append("Ответ модели слишком короткий. Проверьте, не пропущен ли текст.")
    if ratio > float(v["max_length_ratio"]):
        issues.append("Ответ модели слишком длинный. Проверьте исходный фрагмент.")

    return issues


def translation_profile_hash(cfg: dict, model: str) -> str:
    """Fingerprint settings that must stay stable within a resumed project."""
    profile = {
        "model": model,
        "source_lang_code": cfg["lmstudio"].get("source_lang_code", "en"),
        "target_lang_code": cfg["lmstudio"].get("target_lang_code", "ru"),
        "chunking": cfg["chunking"],
        "validation": cfg["validation"],
        "preprocessing": cfg.get("preprocessing", {}),
        "max_output_tokens": cfg["translation"]["max_output_tokens"],
    }
    encoded = json.dumps(profile, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ensure_translation_profile(conn: sqlite3.Connection, cfg: dict, model: str) -> None:
    current = translation_profile_hash(cfg, model)
    previous = get_meta(conn, "translation_profile_sha256")
    processed = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE status != 'pending' OR attempts > 0"
    ).fetchone()[0]
    if processed and previous != current:
        raise RuntimeError(
            "The model or translation settings changed after work began. "
            "Use --rebuild or a new --project-name to avoid mixing incompatible translations."
        )
    if previous != current:
        set_meta(conn, "translation_profile_sha256", current)
        conn.commit()


def retry_delay(cfg: dict, attempt_number: int) -> float:
    values = cfg["translation"].get("retry_backoff_seconds", [5, 15, 30, 60, 120])
    if not isinstance(values, list) or not values:
        return 5.0
    idx = min(max(attempt_number - 1, 0), len(values) - 1)
    return float(values[idx])


def translate_one_chunk(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    client: LMStudioClient,
    model: str,
    cfg: dict,
    logger: logging.Logger,
) -> tuple[str, str]:
    from workspace import translate_one
    return translate_one(conn, row, client, model, cfg, logger)


# ---------------------------- Assembly / reports ----------------------------


def assembled_text(conn: sqlite3.Connection, complete_only: bool) -> tuple[str, int, int]:
    from workspace import assemble_txt
    result = assemble_txt(conn, 'prefix' if complete_only else 'draft')
    return result.text, len(result.manifest), result.total


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_partial(conn: sqlite3.Connection, path: Path) -> tuple[int, int]:
    text, included, total = assembled_text(conn, complete_only=True)
    if included:
        atomic_write_text(path, text)
    return included, total


def write_final(conn: sqlite3.Connection, path: Path) -> None:
    try:
        text, _, _ = assembled_text(conn, complete_only=False)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    atomic_write_text(path, text)


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) n FROM chunks WHERE excluded=0 GROUP BY status").fetchall()
    counts = {row["status"]: int(row["n"]) for row in rows}
    for status in ("pending", "processing", "done", "failed"):
        counts.setdefault(status, 0)
    return counts


def print_status(conn: sqlite3.Connection, paths: dict) -> None:
    counts = status_counts(conn)
    total = sum(counts.values())
    processed = counts["done"]
    pct = processed / total * 100 if total else 0.0
    row = conn.execute(
        "SELECT SUM(prompt_tokens) p, SUM(completion_tokens) c, SUM(duration_seconds) d FROM chunks"
    ).fetchone()
    print(f"Project:    {paths['project_name']}")
    print(f"Database:   {paths['db']}")
    print(f"Progress:   {processed}/{total} ({pct:.1f}%)")
    print(f"Done:       {counts['done']}")
    print(f"Failed:     {counts['failed']}")
    print(f"Pending:    {counts['pending']}")
    if row and row["d"]:
        print(f"Model time: {float(row['d'])/60:.1f} min")
    if row and (row["p"] or row["c"]):
        print(f"Tokens:     prompt={row['p'] or 0}, completion={row['c'] or 0}")
    if paths["partial"].exists():
        print(f"Saved partial snapshot: {paths['partial']}")
    if paths["final"].exists():
        print(f"Saved final snapshot:   {paths['final']}")


def print_inspect(conn: sqlite3.Connection, show_text: bool = False) -> None:
    rows = conn.execute(
        """
        SELECT position, section, status, attempts, error,
               LENGTH(source_text) source_chars,
               LENGTH(COALESCE(translated_text,'')) translated_chars
        FROM chunks
        WHERE status='failed'
        ORDER BY position
        """
    ).fetchall()
    if not rows:
        print("No failed chunks.")
        return
    for row in rows:
        print("-" * 72)
        print(f"Chunk #{row['position']} | {row['status']} | attempts={row['attempts']}")
        if row["section"]:
            print(f"Section: {row['section']}")
        print(f"Chars: source={row['source_chars']} translation={row['translated_chars']}")
        if row["error"]:
            print(f"Error: {row['error']}")
        if show_text:
            detail = conn.execute(
                "SELECT source_text, translated_text FROM chunks WHERE position=?",
                (row["position"],),
            ).fetchone()
            print("SOURCE:")
            print((detail["source_text"] or "")[:1200])
            print("TRANSLATION:")
            print((detail["translated_text"] or "")[:1200])


def reset_for_retry(conn: sqlite3.Connection) -> int:
    from workspace import migrate, event
    migrate(conn)
    cur = conn.execute("UPDATE chunks SET status='pending' WHERE status='failed'")
    event(conn, 'retry', f'Returned to queue: {cur.rowcount}')
    conn.commit()
    return cur.rowcount


# ---------------------------- Commands ----------------------------


def open_existing_project(book: Path, cfg: dict, paths: dict) -> sqlite3.Connection:
    if not paths["db"].exists():
        raise FileNotFoundError(
            f"Project database does not exist: {paths['db']}\nRun translate first to create it."
        )
    conn = connect_db(paths["db"])
    init_schema(conn)
    return conn


def exclusive_command(function):
    def wrapped(args, cfg):
        from workspace import project_lock
        paths = resolve_paths(Path(args.book), cfg, args.project_name)
        with project_lock(paths['db']):
            return function(args, cfg)
    return wrapped


@exclusive_command
def cmd_prepare(args: argparse.Namespace, cfg: dict) -> int:
    book = Path(args.book).expanduser().resolve()
    if not book.exists():
        raise FileNotFoundError(book)
    paths = resolve_paths(book, cfg, args.project_name)
    conn = prepare_project(book, cfg, paths, rebuild=args.rebuild)
    total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    notes = json.loads(get_meta(conn, "preprocessing_notes") or "[]")
    print(f"Project prepared: {paths['project_name']}")
    print(f"Chunks:   {total}")
    for note in notes:
        print(f"Source:   {note}")
    print("Run translate to start or resume translation.")
    conn.close()
    return 0


def cmd_models(args: argparse.Namespace, cfg: dict) -> int:
    dummy_log = setup_logging(Path("lmstudio_models.log"), verbose=False)
    client = LMStudioClient(cfg, dummy_log)
    try:
        models = client.list_models()
        loaded = set(client.list_loaded_models() or [])
    finally:
        # Avoid keeping a throwaway log file around if empty/small.
        for h in dummy_log.handlers:
            h.close()
        p = Path("lmstudio_models.log")
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    configured = str(cfg["lmstudio"]["model"])
    if configured not in models:
        print(f"Configured TranslateGemma model is not available: {configured}")
        return 1
    if configured not in loaded:
        print(f"TranslateGemma is visible but not loaded: {configured}")
        return 1
    print(f"TranslateGemma is ready: {configured}")
    return 0


def cmd_translate(args: argparse.Namespace, cfg: dict) -> int:
    import workspace
    book = Path(args.book).expanduser().resolve()
    paths = resolve_paths(book, cfg, args.project_name)
    if args.max_chunks is not None and args.max_chunks <= 0:
        raise ValueError('max-chunks must be positive')
    if args.start_chunk is not None and args.start_chunk <= 0:
        raise ValueError('start-chunk must be positive')
    with workspace.project_lock(paths['db']):
        conn = prepare_project(book, cfg, paths, rebuild=args.rebuild)
        try:
            workspace.recover(conn)
            positions = [r[0] for r in conn.execute(
                "SELECT position FROM chunks WHERE excluded=0 AND status IN ('pending','failed') "
                "AND position>=? ORDER BY position", (args.start_chunk or 1,))]
        finally:
            conn.close()
    if args.max_chunks is not None:
        positions = positions[:args.max_chunks]
    result = workspace.run(paths['db'], cfg, positions=positions)
    with workspace.project_lock(paths['db']):
        conn = connect_db(paths['db'])
        try:
            write_partial(conn, paths['partial'])
            try:
                full, _, _ = assembled_text(conn, complete_only=False)
            except ValueError:
                full = None
            if full is not None:
                atomic_write_text(paths['final'], full)
            print_status(conn, paths)
        finally:
            conn.close()
    if result.state == 'failed':
        print(result.message, file=sys.stderr)
        return 1
    if result.state == 'no_work' and full is None:
        print(result.message, file=sys.stderr)
        return 1
    return 0


def cmd_status(args: argparse.Namespace, cfg: dict) -> int:
    from workspace import recover_if_idle
    book = Path(args.book).expanduser().resolve()
    paths = resolve_paths(book, cfg, args.project_name)
    conn = open_existing_project(book, cfg, paths)
    try:
        recover_if_idle(conn, paths['db'])
        print_status(conn, paths)
    finally:
        conn.close()
    return 0


def cmd_inspect(args: argparse.Namespace, cfg: dict) -> int:
    book = Path(args.book).expanduser().resolve()
    paths = resolve_paths(book, cfg, args.project_name)
    conn = open_existing_project(book, cfg, paths)
    print_inspect(conn, show_text=args.show_text)
    conn.close()
    return 0


@exclusive_command
def cmd_retry(args: argparse.Namespace, cfg: dict) -> int:
    book = Path(args.book).expanduser().resolve()
    paths = resolve_paths(book, cfg, args.project_name)
    conn = open_existing_project(book, cfg, paths)
    count = reset_for_retry(conn)
    write_partial(conn, paths["partial"])
    print(f"Reset {count} chunk(s) to pending. Run translate again.")
    if paths["final"].exists() and count:
        print(f"Warning: existing final file is stale until translation completes: {paths['final']}")
    conn.close()
    return 0


@exclusive_command
def cmd_assemble(args: argparse.Namespace, cfg: dict) -> int:
    book = Path(args.book).expanduser().resolve()
    paths = resolve_paths(book, cfg, args.project_name)
    conn = open_existing_project(book, cfg, paths)
    if args.partial:
        included, total = write_partial(conn, paths["partial"])
        if not included:
            conn.close()
            raise ValueError('У книги ещё нет непрерывного переведённого начала.')
        print(f"Wrote partial prefix: {included}/{total} chunks -> {paths['partial']}")
    else:
        write_final(conn, paths["final"])
        print(f"Wrote final translation -> {paths['final']}")
    conn.close()
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resumable EN->RU TXT book translator for TranslateGemma in LM Studio."
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help=f"Path to YAML config (default: {DEFAULT_CONFIG})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_models = sub.add_parser("models", help="Check that the configured TranslateGemma is loaded")
    p_models.set_defaults(func=cmd_models)

    def add_book_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("book", help="Path to source English UTF-8 TXT")
        p.add_argument("--project-name", help="Override project/output name")

    p_prepare = sub.add_parser("prepare", help="Create chunks without translating")
    add_book_args(p_prepare)
    p_prepare.add_argument("--rebuild", action="store_true", help="Recreate project chunks/database")
    p_prepare.set_defaults(func=cmd_prepare)

    p_translate = sub.add_parser("translate", help="Create/resume translation")
    add_book_args(p_translate)
    p_translate.add_argument(
        "--rebuild",
        action="store_true",
        help="Delete/recreate this project's translation database and chunks",
    )
    p_translate.add_argument("--verbose", action="store_true")
    p_translate.add_argument(
        "--max-chunks",
        type=int,
        help="Translate at most N pending chunks in this run (useful for a smoke test)",
    )
    p_translate.add_argument(
        "--start-chunk",
        type=int,
        help="Start this run at chunk N (use with --max-chunks for representative tests)",
    )
    p_translate.set_defaults(func=cmd_translate)

    p_status = sub.add_parser("status", help="Show project progress")
    add_book_args(p_status)
    p_status.set_defaults(func=cmd_status)

    p_inspect = sub.add_parser("inspect", help="Show failed chunks")
    add_book_args(p_inspect)
    p_inspect.add_argument("--show-text", action="store_true", help="Show source/translation excerpts")
    p_inspect.set_defaults(func=cmd_inspect)

    p_retry = sub.add_parser("retry", help="Reset failed chunks to pending")
    add_book_args(p_retry)
    p_retry.set_defaults(func=cmd_retry)

    p_assemble = sub.add_parser("assemble", help="Rebuild output TXT from SQLite")
    add_book_args(p_assemble)
    p_assemble.add_argument(
        "--partial",
        action="store_true",
        help="Write only the contiguous translated prefix instead of requiring completion",
    )
    p_assemble.set_defaults(func=cmd_assemble)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        cfg = load_config(Path(args.config).expanduser().resolve())
        return int(args.func(args, cfg))
    except requests.ConnectionError:
        print(
            "ERROR: Cannot connect to LM Studio. Start the local server (Developer -> Server) "
            "and check lmstudio.base_url in config.yaml.",
            file=sys.stderr,
        )
        return 2
    except requests.HTTPError as exc:
        body = ""
        if exc.response is not None:
            body = exc.response.text[:1000]
        print(f"ERROR: LM Studio HTTP error: {exc}\n{body}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
