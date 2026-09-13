"""Tests para la exportación e importación de flujos en formato ZIP en Dédalo."""

import io
import json
import zipfile

from dedalo import store


def test_export_and_import_flow_zip():
    # 1. Guardar un flujo de prueba
    flow_id = "test-export-flow"
    graph = {
        "version": 1,
        "name": flow_id,
        "nodes": [
            {"id": "ping", "type": "input.ping", "config": {"host": "1.1.1.1"}},
            {"id": "aviso", "type": "output.alert", "config": {"text": "ok", "secret": "mi_clave"}},
        ],
        "edges": [
            {"source": "ping", "target": "aviso", "port": "ok"},
        ],
    }
    meta = {"name": "Test Export", "description": "Flujo para probar exportación", "enabled": True}
    store.save_flow(flow_id, graph, meta, force=True)

    # 2. Exportar a ZIP
    zip_bytes = store.export_flow_zip(flow_id)
    assert zip_bytes
    assert zipfile.is_zipfile(io.BytesIO(zip_bytes))

    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        assert f"{flow_id}.json" in names
        assert f"{flow_id}.meta.json" in names

        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["format"] == "dedalo_flow_bundle"
        assert manifest["flows"][0]["id"] == flow_id
        assert manifest["flows"][0]["required_secrets"] == ["mi_clave"]

    # 3. Eliminar flujo
    store.delete_flow(flow_id)
    assert store.load_flow(flow_id) is None

    # 4. Importar desde el ZIP
    res = store.import_flows_from_zip(zip_bytes, overwrite=True)
    assert res["status"] == "ok"
    assert res["imported_count"] == 1
    assert res["imported"][0]["id"] == flow_id
    assert "mi_clave" in res["required_secrets"]

    # 5. Comprobar que el flujo se cargó y compiló correctamente
    reloaded = store.load_flow(flow_id)
    assert reloaded is not None
    assert len(reloaded["graph"]["nodes"]) == 2
    assert "def run(ctx):" in reloaded["code"]

    # Limpiar
    store.delete_flow(flow_id)


def test_export_all_flows_zip():
    backup_bytes = store.export_all_flows_zip()
    assert backup_bytes
    assert zipfile.is_zipfile(io.BytesIO(backup_bytes))

    with zipfile.ZipFile(io.BytesIO(backup_bytes), "r") as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["format"] == "dedalo_flows_backup"
        assert manifest["total_flows"] >= 1


def test_import_zip_slip_security():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../etc/evil.json", json.dumps({"nodes": []}))

    res = store.import_flows_from_zip(buf.getvalue())
    assert res["status"] == "error"
    assert "sospechoso" in res["error"].lower() or "zip" in res["error"].lower()


def test_import_direct_json():
    raw_graph = {
        "version": 1,
        "name": "flujo-directo-json",
        "nodes": [
            {"id": "n1", "type": "input.disk_free", "config": {}},
        ],
        "edges": [],
    }
    json_bytes = json.dumps(raw_graph).encode("utf-8")
    res = store.import_flows_from_zip(json_bytes, overwrite=True)
    assert res["status"] == "ok"
    assert res["imported_count"] == 1
    assert res["imported"][0]["id"] == "flujo-directo-json"

    assert store.load_flow("flujo-directo-json") is not None
    store.delete_flow("flujo-directo-json")
