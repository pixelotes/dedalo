"""Tests de i18n: catálogo es.json, idioma por petición y mensajes traducidos."""

import json
import string

import pytest
from fastapi.testclient import TestClient

from dedalo import i18n, runtime
from dedalo.app import app

client = TestClient(app)


def _fields(text: str) -> set[str]:
    return {name.split("!")[0].split(":")[0] for _, name, _, _ in string.Formatter().parse(text) if name}


def test_catalog_placeholders_match_between_languages():
    catalog = json.loads((i18n.LOCALES_DIR / "es.json").read_text(encoding="utf-8"))
    assert len(catalog) > 500
    for en, es in catalog.items():
        if en.startswith("Receives HTTP requests at"):
            continue  # texto de ayuda con un ejemplo de URL: se muestra tal cual, nunca se formatea
        assert _fields(en) == _fields(es), (en, es)  # evita KeyError al formatear en español


def test_default_language_is_english_and_needs_no_catalog(monkeypatch):
    monkeypatch.delenv("DEDALO_LANG", raising=False)
    assert i18n.current() == "en"
    assert i18n.tr("Empty command.") == "Empty command."
    assert i18n.tr("Wait completed ({wait_time:.1f}s)", wait_time=2) == "Wait completed (2.0s)"


def test_tr_translates_and_formats(monkeypatch):
    token = i18n.set_lang("es")
    try:
        assert i18n.tr("Wait completed ({wait_time:.1f}s)", wait_time=2) == "Espera completada (2.0s)"
        assert i18n.tr("some text that is not in the catalog") == "some text that is not in the catalog"
    finally:
        i18n.reset(token)


def test_tr_without_params_keeps_literal_braces():
    assert i18n.tr("Supports {fecha}, {hora}, {timestamp}.") == "Supports {fecha}, {hora}, {timestamp}."


def test_env_default_language(monkeypatch):
    monkeypatch.setenv("DEDALO_LANG", "es")
    assert i18n.current() == "es"
    monkeypatch.setenv("DEDALO_LANG", "xx")  # idioma sin catálogo: inglés
    assert i18n.current() == "en"


def test_node_error_messages_follow_the_language():
    r_en = runtime.command_input(None, [], cmd="")
    assert r_en.error == "Missing command to run"
    token = i18n.set_lang("es")
    try:
        assert runtime.command_input(None, [], cmd="").error == "Falta el comando a ejecutar"
    finally:
        i18n.reset(token)


def test_catalog_endpoint_translates_per_request_header():
    en = client.get("/api/catalog").json()["node_types"]["input.command"]
    es = client.get("/api/catalog", headers={"X-Dedalo-Lang": "es"}).json()["node_types"]["input.command"]
    assert en["label"] == "System Command (Shell)"
    assert es["label"] == "Comando del Sistema (Shell)"
    assert en["help"]["cmd"] != es["help"]["cmd"]
    assert en["optional"] == es["optional"] and sorted(en["required"]) == sorted(es["required"])  # los valores no cambian


def test_catalog_choice_values_are_never_translated():
    en = client.get("/api/catalog").json()["node_types"]
    es = client.get("/api/catalog", headers={"X-Dedalo-Lang": "es"}).json()["node_types"]
    for ntype, spec in en.items():
        for field, choices in spec["choices"].items():
            en_vals = [c["value"] if isinstance(c, dict) else c for c in choices]
            es_vals = [c["value"] if isinstance(c, dict) else c for c in es[ntype]["choices"][field]]
            assert en_vals == es_vals, (ntype, field)


def test_templates_endpoint_translates_per_request_header():
    en = {t["name"]: t for t in client.get("/api/templates").json()}
    es = {t["name"]: t for t in client.get("/api/templates", headers={"X-Dedalo-Lang": "es"}).json()}
    assert set(en) == set(es) and "haiku-diario" in en  # los identificadores no cambian
    assert any(en[n]["title"] != es[n]["title"] for n in en)


def test_unknown_language_header_falls_back_to_english():
    res = client.get("/api/catalog", headers={"X-Dedalo-Lang": "klingon"}).json()
    assert res["node_types"]["input.command"]["label"] == "System Command (Shell)"


@pytest.mark.parametrize("path", ["/api/template?name=haiku-diario"])
def test_template_endpoint_still_works(path):
    assert client.get(path).status_code == 200
