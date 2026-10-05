"""HTTP-клиент: ретраи, auth, маскировка ключа — на подменённом urlopen и без реальных пауз."""
import io
import json
import urllib.error

import pytest

from adops.tracker import api

SECRET = "SuperSecretValue123"


class Resp:
    def __init__(self, status, body):
        self.status = status
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body=None, headers=None):
    return urllib.error.HTTPError("http://x", code, "err", headers or {},
                                  io.BytesIO(json.dumps(body or {}).encode()))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    return api.TrackerClient(base_url="https://tracker.example", api_key=SECRET, delay=0)


def script(monkeypatch, seq):
    """urlopen отдаёт элементы seq по очереди (исключение — поднимается)."""
    calls = []

    def fake(req, timeout=None):
        calls.append(req)
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(api.urllib.request, "urlopen", fake)
    return calls


def test_requires_url_and_key(monkeypatch):
    monkeypatch.delenv("TRACKER_API_KEY", raising=False)
    monkeypatch.delenv("TRACKER_BASE_URL", raising=False)
    with pytest.raises(RuntimeError):
        api.TrackerClient(base_url="https://t.example")
    with pytest.raises(RuntimeError):
        api.TrackerClient(api_key="k-123456")


def test_retries_transient_then_succeeds(client, monkeypatch):
    calls = script(monkeypatch, [http_error(503), http_error(429), Resp(200, [{"id": 1, "name": "a"}])])
    assert client.list_campaigns() == [{"id": 1, "name": "a"}]
    assert len(calls) == 3


def test_auth_failure_is_not_retried(client, monkeypatch):
    calls = script(monkeypatch, [http_error(401), Resp(200, [])])
    with pytest.raises(api.TrackerAuthError):
        client.list_campaigns()
    assert len(calls) == 1


def test_network_exhaustion_raises_tracker_error(client, monkeypatch):
    script(monkeypatch, [urllib.error.URLError("down")] * 4)
    with pytest.raises(api.TrackerError):
        client.list_campaigns()


def test_non_retryable_error_returned_to_caller(client, monkeypatch):
    script(monkeypatch, [http_error(400, {"errors": {"message": "Clicks not found"}})])
    ok, status, msg = client.update_cost(5, {"cost": 1})
    assert (ok, status, msg) == (False, 400, "Clicks not found")


def test_update_uses_put_and_path(client, monkeypatch):
    calls = script(monkeypatch, [Resp(200, {})])
    assert client.update_cost(7, {"cost": 1})[0] is True
    assert calls[0].get_method() == "PUT" and calls[0].full_url.endswith("/campaigns/7/costs")


def test_list_campaigns_filters_totals_and_deleted(client, monkeypatch):
    script(monkeypatch, [Resp(200, [{"id": "totals"}, {"id": 2, "is_deleted": True}, {"id": 3, "name": "ok"}, {"id": 0}])])
    assert [c["id"] for c in client.list_campaigns()] == [3]


def test_tag_report_skips_totals_row(client, monkeypatch):
    rep = {"report": [{"level": "0", "name": "", "clicks": "9", "cost": "1"},
                      {"level": "1", "name": "", "clicks": "9", "cost": "1"},
                      {"level": "1", "name": "t-1/a", "clicks": "12", "cost": "3.5"}]}
    script(monkeypatch, [Resp(200, rep)])
    assert client.tag_report(1) == [("t-1/a", 12, 3.5)]


def test_secret_is_masked_in_error_text():
    assert SECRET not in api.mask(f"request failed: key={SECRET}")
    assert SECRET not in api._err_message({"message": f"bad key: {SECRET}"})


def test_key_not_in_repr_of_errors(client, monkeypatch):
    script(monkeypatch, [http_error(401)])
    with pytest.raises(api.TrackerAuthError) as e:
        client.list_campaigns()
    assert SECRET not in str(e.value)


def test_config_file_roundtrip(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"base_url": "https://t.example/", "api_key": "k-123456"}))
    c = api.TrackerClient.from_config(p)
    assert c.base_url == "https://t.example"
    with pytest.raises(RuntimeError):
        api.load_config(tmp_path / "missing.json")
