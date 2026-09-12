import time
from pathlib import Path

import pytest

from dedalo import history


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_history.db"


def test_record_and_get_run(tmp_db: Path) -> None:
    # 1. Start run
    run_id = history.record_run_start(
        flow_id="test-flow",
        flow_name="Flujo de Prueba",
        trigger_type="manual",
        db_path=tmp_db,
    )
    assert run_id.startswith("run_")

    # Check running status
    runs = history.list_runs(flow_id="test-flow", db_path=tmp_db)
    assert len(runs) == 1
    assert runs[0]["status"] == "running"
    assert runs[0]["trigger_type"] == "manual"

    # 2. Finish run with steps
    results = {
        "nodo_1": {"status": "ok", "data": "Resultado exitoso", "error": None},
        "nodo_2": {"status": "skipped", "data": None, "error": None},
        "nodo_3": {"status": "error", "data": None, "error": "Fallo en conexión"},
    }
    history.record_run_finish(
        run_id=run_id,
        status="error",
        duration=0.123,
        error="Fallo en nodo_3",
        results=results,
        db_path=tmp_db,
    )

    # Check finished run
    run_detail = history.get_run(run_id, db_path=tmp_db)
    assert run_detail is not None
    assert run_detail["status"] == "error"
    assert run_detail["duration"] == 0.123
    assert run_detail["error"] == "Fallo en nodo_3"
    assert len(run_detail["steps"]) == 3

    steps = {s["node_id"]: s for s in run_detail["steps"]}
    assert steps["nodo_1"]["status"] == "ok"
    assert steps["nodo_1"]["data"] == "Resultado exitoso"
    assert steps["nodo_2"]["status"] == "skipped"
    assert steps["nodo_3"]["status"] == "error"
    assert steps["nodo_3"]["error"] == "Fallo en conexión"


def test_purge_older_than(tmp_db: Path) -> None:
    history.init_db(tmp_db)
    now = time.time()
    day_sec = 86400

    # Insert old run (>35 days old)
    with history.get_db(tmp_db) as conn:
        conn.execute(
            """
            INSERT INTO runs (id, flow_id, flow_name, status, trigger_type, started_at)
            VALUES ('run_old', 'flow1', 'Flow 1', 'ok', 'schedule', ?)
            """,
            (now - (35 * day_sec),),
        )
        conn.execute(
            """
            INSERT INTO run_steps (run_id, node_id, status, data)
            VALUES ('run_old', 'node1', 'ok', 'old data')
            """
        )
        # Insert recent run (2 days old)
        conn.execute(
            """
            INSERT INTO runs (id, flow_id, flow_name, status, trigger_type, started_at)
            VALUES ('run_recent', 'flow1', 'Flow 1', 'ok', 'schedule', ?)
            """,
            (now - (2 * day_sec),),
        )
        conn.execute(
            """
            INSERT INTO run_steps (run_id, node_id, status, data)
            VALUES ('run_recent', 'node1', 'ok', 'recent data')
            """
        )
        conn.commit()

    # Purge runs older than 30 days
    purged = history.purge_older_than(days=30, db_path=tmp_db)
    assert purged == 1

    # Old run should be gone, recent run should remain
    assert history.get_run("run_old", db_path=tmp_db) is None
    assert history.get_run("run_recent", db_path=tmp_db) is not None

    with history.get_db(tmp_db) as conn:
        steps = conn.execute("SELECT * FROM run_steps").fetchall()
        assert len(steps) == 1
        assert steps[0]["run_id"] == "run_recent"
