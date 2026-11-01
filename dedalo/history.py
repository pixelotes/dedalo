"""Historial de ejecuciones de Dédalo en SQLite local.

Diseñado para mínimo consumo de memoria y almacenamiento en Raspberry Pi.
Almacena el estado general de cada ejecución (runs) y el desglose de resultados
paso a paso por nodo (run_steps).
Permite purgar de forma automática registros con más de 30 días de antigüedad.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("DEDALO_HISTORY_DB", "data/history.db"))
MAX_DATA_PREVIEW_LEN = 4000


def get_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Obtiene una conexión configurada a SQLite con WAL y foreign keys."""
    target = Path(db_path) if db_path else DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA cache_size = -2000;")  # Limita la caché en RAM a 2 MB
    conn.execute("PRAGMA mmap_size = 0;")       # Evita mmap masivo de memoria virtual
    conn.execute("PRAGMA temp_store = FILE;")   # No usar RAM para operaciones temporales
    return conn


_initialized: set[str] = set()


def init_db(db_path: Path | str | None = None) -> None:
    """Inicializa las tablas e índices si no existen y migra columnas nuevas (una vez por proceso y base)."""
    target = Path(db_path) if db_path else DB_PATH
    key = str(target)
    if key in _initialized and target.exists():
        return
    _init_schema(db_path)
    _initialized.add(key)


def _init_schema(db_path: Path | str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            flow_id TEXT NOT NULL,
            flow_name TEXT,
            status TEXT NOT NULL,          -- 'ok', 'error', 'running'
            trigger_type TEXT NOT NULL,    -- 'manual', 'schedule', 'preview', 'debug'
            started_at REAL NOT NULL,
            finished_at REAL,
            duration REAL,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_runs_flow_started ON runs(flow_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);

        CREATE TABLE IF NOT EXISTS run_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            status TEXT NOT NULL,          -- 'ok', 'skipped', 'error'
            data TEXT,
            error TEXT,
            inputs TEXT,
            duration REAL,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_run_steps_run_id ON run_steps(run_id);
        """)

        # Migración no destructiva de columnas añadidas posteriormente
        for col_name, col_type in [("inputs", "TEXT"), ("duration", "REAL")]:
            try:
                conn.execute(f"ALTER TABLE run_steps ADD COLUMN {col_name} {col_type};")
            except sqlite3.OperationalError:
                pass


def _serialize_data(val: Any, truncate: bool = True, max_len: int = 200_000) -> str | None:
    """Convierte de forma segura y estructurada los datos a texto para almacenamiento."""
    if val is None:
        return None
    if isinstance(val, str):
        text = val
    else:
        try:
            text = json.dumps(val, ensure_ascii=False)
        except Exception:
            text = str(val)

    if truncate and len(text) > max_len:
        return text[:max_len] + f" ...[truncado a {max_len} caracteres]"
    return text


def record_run_start(
    flow_id: str,
    flow_name: str = "",
    trigger_type: str = "manual",
    db_path: Path | str | None = None,
) -> str:
    """Registra el comienzo de una ejecución y devuelve el ID generado."""
    init_db(db_path)
    run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    now = time.time()
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO runs (id, flow_id, flow_name, status, trigger_type, started_at)
            VALUES (?, ?, ?, 'running', ?, ?)
            """,
            (run_id, flow_id, flow_name or flow_id, trigger_type, now),
        )
        conn.commit()
    return run_id


def record_run_finish(
    run_id: str,
    status: str,
    duration: float,
    error: str | None = None,
    results: dict[str, Any] | None = None,
    db_path: Path | str | None = None,
    truncate: bool = True,
) -> None:
    """Actualiza el estado final del run e inserta los pasos de ejecución con entradas y salidas."""
    init_db(db_path)
    now = time.time()
    with get_db(db_path) as conn:
        conn.execute(
            """
            UPDATE runs
            SET status = ?, finished_at = ?, duration = ?, error = ?
            WHERE id = ?
            """,
            (status, now, round(duration, 4), error, run_id),
        )

        if results:
            steps_data = []
            for node_id, res in results.items():
                if hasattr(res, "status"):
                    st = res.status
                    d = getattr(res, "data", None) if st == "ok" else None
                    err = getattr(res, "error", "") if st == "error" else None
                    inp = getattr(res, "inputs", None)
                    dur = getattr(res, "duration", 0.0)
                elif isinstance(res, dict):
                    st = res.get("status", "ok")
                    d = res.get("data")
                    err = res.get("error")
                    inp = res.get("inputs")
                    dur = res.get("duration_ms", 0.0)
                else:
                    st = "ok"
                    d = res
                    err = None
                    inp = None
                    dur = 0.0

                steps_data.append((
                    run_id,
                    node_id,
                    st,
                    _serialize_data(d, truncate=truncate),
                    err,
                    _serialize_data(inp, truncate=truncate),
                    dur,
                ))

            conn.executemany(
                """
                INSERT INTO run_steps (run_id, node_id, status, data, error, inputs, duration)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                steps_data,
            )
        conn.commit()


def list_runs(
    flow_id: str | None = None,
    limit: int = 50,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Lista las ejecuciones más recientes, opcionalmente filtradas por flow_id."""
    init_db(db_path)
    limit = max(1, min(limit, 200))
    with get_db(db_path) as conn:
        if flow_id:
            rows = conn.execute(
                """
                SELECT r.id, r.flow_id, r.flow_name, r.status, r.trigger_type,
                       r.started_at, r.finished_at, r.duration, r.error,
                       COUNT(s.id) AS step_count
                FROM runs r
                LEFT JOIN run_steps s ON s.run_id = r.id
                WHERE r.flow_id = ?
                GROUP BY r.id
                ORDER BY r.started_at DESC
                LIMIT ?
                """,
                (flow_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT r.id, r.flow_id, r.flow_name, r.status, r.trigger_type,
                       r.started_at, r.finished_at, r.duration, r.error,
                       COUNT(s.id) AS step_count
                FROM runs r
                LEFT JOIN run_steps s ON s.run_id = r.id
                GROUP BY r.id
                ORDER BY r.started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    return [dict(row) for row in rows]


def get_run(run_id: str, db_path: Path | str | None = None) -> dict[str, Any] | None:
    """Obtiene el detalle completo de una ejecución con todos sus pasos ordenados."""
    init_db(db_path)
    with get_db(db_path) as conn:
        run_row = conn.execute(
            """
            SELECT id, flow_id, flow_name, status, trigger_type, started_at, finished_at, duration, error
            FROM runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()

        if not run_row:
            return None

        steps = conn.execute(
            """
            SELECT id, node_id, status, data, error, inputs, duration
            FROM run_steps
            WHERE run_id = ?
            ORDER BY id ASC
            """,
            (run_id,),
        ).fetchall()

    run_dict = dict(run_row)
    run_dict["steps"] = [dict(s) for s in steps]
    return run_dict


def purge_older_than(days: int = 30, db_path: Path | str | None = None) -> int:
    """Elimina ejecuciones y pasos con antigüedad superior a `days` días (por defecto 30)."""
    init_db(db_path)
    cutoff = time.time() - (days * 86400)
    with get_db(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM run_steps WHERE run_id IN (SELECT id FROM runs WHERE started_at < ?)",
            (cutoff,),
        )
        cursor.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
        deleted = cursor.rowcount
        conn.commit()
        return deleted
