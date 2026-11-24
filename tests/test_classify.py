"""Fast classifier (transform.classify) and its teaching tool (tool.classifier)."""

import hashlib
import math
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from dedalo import compiler, runtime
from dedalo.runtime import NodeResult, classify_items, tool_classifier


class Ctx:
    flow_name = "triage"


@pytest.fixture(autouse=True)
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _fake_vec(text: str, dims: int = 64) -> list[float]:
    """Bag of words hashed into a vector: texts that share words are similar."""
    v = [0.0] * dims
    for word in text.lower().replace("|", " ").split():
        v[int(hashlib.md5(word.encode()).hexdigest(), 16) % dims] += 1.0
    return v


def _embeddings(calls: list):
    def post(self, url, headers=None, json=None, **kw):
        calls.append({"url": url, "json": json})
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": [{"index": i, "embedding": _fake_vec(t)} for i, t in enumerate(json["input"])]}
        return resp
    return post


MAIL = [
    {"id": "1", "from": "newsletter@shop.com", "subject": "weekly offers and discounts"},
    {"id": "2", "from": "bank@santander.es", "subject": "your loan payment is due"},
]
KW = dict(text="{from} | {subject}", model="emb", url="http://llm.test/v1", min_examples=2, k=3, min_agreement=0.7)


def _learn(item_id, label, **cfg):
    tools = runtime._build_classifier_tools({"labels": "important,subscription", **cfg}, Ctx())
    return tools[0].handler({"id": item_id, "label": label})


def test_cold_start_sends_everything_to_unsure_and_learning_warms_it_up():
    calls: list = []
    with patch("httpx.Client.post", _embeddings(calls)):
        res = classify_items(Ctx(), [NodeResult("ok", data=MAIL)], **KW)
        assert res.ok and res.data["sure"] == [] and len(res.data["unsure"]) == 2
        assert calls[0]["url"] == "http://llm.test/v1/embeddings" and calls[0]["json"]["model"] == "emb"
        assert calls[0]["json"]["input"][0] == "newsletter@shop.com | weekly offers and discounts"

        # the agent teaches the classifier by _id; no new embedding call is needed for that
        assert "Learned" in _learn("1", "subscription")
        assert "Learned" in _learn("2", "important")
        assert len(calls) == 1
        assert "no item" in _learn("1", "important")  # already learned

        new = [
            {"id": "3", "from": "newsletter@shop.com", "subject": "new weekly offers"},
            {"id": "4", "from": "bank@santander.es", "subject": "loan payment receipt"},
        ]
        res = classify_items(Ctx(), [NodeResult("ok", data=new)], **KW)
    labels = {it["id"]: it["_label"] for it in res.data["sure"]}
    assert labels == {"3": "subscription", "4": "important"}
    assert res.data["counts"] == {"subscription": 1, "important": 1} and res.data["examples"] == 2
    assert all(0.7 <= it["_confidence"] <= 1 for it in res.data["sure"])


def test_split_vote_goes_to_unsure_with_its_neighbours():
    with patch("httpx.Client.post", _embeddings([])):
        classify_items(Ctx(), [NodeResult("ok", data=MAIL)], **KW)
        _learn("1", "subscription")
        _learn("2", "important")
        mixed = [{"id": "5", "from": "x@y.com", "subject": "weekly loan"}]
        res = classify_items(Ctx(), [NodeResult("ok", data=mixed)], **{**KW, "min_agreement": 0.9})
    assert res.data["sure"] == []
    item = res.data["unsure"][0]
    assert item["_id"] == "5" and item["_neighbours"]


def test_learning_enforces_allowed_labels_and_cap():
    with patch("httpx.Client.post", _embeddings([])):
        classify_items(Ctx(), [NodeResult("ok", data=MAIL)], **KW)
    assert "not one of" in _learn("1", "spam")
    assert "Learned" in _learn("1", "subscription", max_examples=1)
    assert "Learned" in _learn("2", "important", max_examples=1)
    conn = sqlite3.connect("data/state/classifier/triage.sqlite")
    rows = conn.execute("SELECT label, vec FROM examples").fetchall()
    assert [r[0] for r in rows] == ["important"]  # oldest forgotten
    # vectors are stored as float16 bytes (2 bytes per dimension), unit length
    assert len(rows[0][1]) == 2 * 64
    assert math.isclose(sum(x * x for x in runtime._vec_unpack(rows[0][1])), 1.0, rel_tol=1e-2)


def test_embedding_errors_fail_the_node():
    bad = MagicMock(status_code=401, text="unauthorized")
    with patch("httpx.Client.post", return_value=bad):
        res = classify_items(Ctx(), [NodeResult("ok", data=MAIL)], **KW)
    assert not res.ok and "401" in res.error


def test_items_without_id_get_a_stable_one_and_empty_input_is_fine():
    with patch("httpx.Client.post", _embeddings([])):
        res = classify_items(Ctx(), [NodeResult("ok", data=["plain text item"])], **KW)
        again = classify_items(Ctx(), [NodeResult("ok", data=["plain text item"])], **KW)
        empty = classify_items(Ctx(), [NodeResult("ok", data=[])], **KW)
    assert res.data["unsure"][0]["_id"] == again.data["unsure"][0]["_id"]
    assert res.data["unsure"][0]["value"] == "plain text item"
    assert empty.ok and empty.data["unsure"] == []


def test_classifier_tool_wires_into_the_harness():
    desc = tool_classifier(Ctx(), [], labels="important,subscription")
    assert desc.ok and desc.data["__dedalo_tool__"] == "classifier"
    graph = compiler.Graph.from_dict({"version": 1, "name": "t", "nodes": [
        {"id": "mail", "type": "input.imap", "config": {"host": "h", "username": "u"}},
        {"id": "fast", "type": "transform.classify", "config": {"model": "emb"}},
        {"id": "teach", "type": "tool.classifier", "config": {}},
        {"id": "agent", "type": "transform.harness", "config": {"prompt": "p"}},
    ], "edges": [
        {"source": "mail", "target": "fast", "port": "ok"},
        {"source": "fast", "target": "agent", "port": "ok"},
        {"source": "teach", "target": "agent", "port": "ok"},
    ]})
    assert compiler.validate(graph) == []
    assert "runtime.classify_items" in compiler.compile(graph)
    tools = runtime._build_classifier_tools({"labels": "a,b"}, Ctx())
    assert tools[0].name == "learn_label" and tools[0].parameters["properties"]["label"]["enum"] == ["a", "b"]
