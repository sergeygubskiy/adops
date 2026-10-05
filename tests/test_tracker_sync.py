"""Оркестрация переноса расходов — end-to-end на фейковом клиенте, без сети.

Проверяется то, что чистыми функциями не проверить: сколько запросов записи уходит, с какими
телами, что попадает в бэкап и как выглядит статус при ЧАСТИЧНОМ сбое. Каталог бэкапа подменяется
на временный (tmp_path) — домашний каталог тесты не засоряют.
"""
import json

import pytest

from adops.tracker import cost, sync


class FakeClient:
    """Отдаёт заданную карту и копит запросы записи. fail_on: {campaign_id: сообщение}."""

    def __init__(self, reports, fail_on=None):
        self._reports = reports                # {cid: [(tag, clicks, cost), ...]}
        self.fail_on = fail_on or {}
        self.writes = []

    def list_campaigns(self):
        return [{"id": cid, "name": f"camp{cid}"} for cid in sorted(self._reports)]

    def tag_report(self, campaign_id):
        return self._reports.get(campaign_id, [])

    def update_cost(self, campaign_id, body):
        self.writes.append((campaign_id, dict(body)))
        msg = self.fail_on.get(campaign_id)
        return (False, 400, msg) if msg else (True, 200, "ok")


TAG = "t-1/tok"
ROW = [{"tag": TAG, "cost": 200.0, "key": cost.norm_tag(TAG)}]
TWO = {11: [(TAG, 500, 7.0)], 22: [(TAG, 500, 3.0)]}      # метка набрала клики в двух кампаниях
ONE = {11: [(TAG, 1000, 7.0)], 22: [("t-other/x", 900, 1.0)]}


@pytest.fixture(autouse=True)
def backup_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "BACKUP_DIR", tmp_path)
    return tmp_path


def run(client, dry_run=False):
    return sync.run_sync(ROW, emit=lambda *a, **k: None, client=client, dry_run=dry_run)


def dumps(path):
    return [json.loads(p.read_text(encoding="utf-8")) for p in path.glob("*.json")]


def test_dry_run_writes_nothing_and_shows_split(backup_dir):
    c = FakeClient(TWO)
    st = run(c, dry_run=True)[0][2]
    assert c.writes == [] and dumps(backup_dir) == []
    assert st.startswith("делю на 2 кампании") and "$100" in st and "camp11" in st


def test_real_write_two_requests_sum_matches(backup_dir):
    c = FakeClient(TWO)
    res = run(c)
    assert {cid for cid, _ in c.writes} == {11, 22}
    costs = sorted(b["cost"] for _, b in c.writes)
    assert costs == [100.0, 100.0] and sum(costs) == 200.0
    for _cid, b in c.writes:                    # контракт тела не поехал
        assert b["tagSlot"] == cost.TAG_SLOT and b["model"] == cost.MODEL_TOTAL and b["tagValue"] == TAG
    assert res[0][2].startswith("разделено на 2")


def test_backup_has_both_writes_with_old_values(backup_dir):
    run(FakeClient(TWO))
    (rec,) = dumps(backup_dir)
    assert sorted(x["old_cost"] for x in rec) == [3.0, 7.0]
    assert sorted(x["new_cost"] for x in rec) == [100.0, 100.0]


def test_single_campaign_unchanged_behaviour():
    c = FakeClient(ONE)
    res = run(c)
    assert len(c.writes) == 1 and c.writes[0][0] == 11 and c.writes[0][1]["cost"] == 200.0
    assert res[0][2].startswith("обновлено $200")


def test_partial_failure_is_visible(backup_dir):
    c = FakeClient(TWO, fail_on={22: "Clicks not found"})
    st = run(c)[0][2]
    assert st.startswith("ЧАСТИЧНО") and "camp11 $100" in st and "camp22" in st
    assert sync.bucket(st) == "частично"
    (rec,) = dumps(backup_dir)
    assert len(rec) == 1, "в бэкап попадает ТОЛЬКО реально записанная кампания"


def test_all_failed_is_error_without_backup(backup_dir):
    st = run(FakeClient(TWO, fail_on={11: "boom", 22: "boom"}))[0][2]
    assert st.startswith("ошибка:") and sync.bucket(st) == "ошибка"
    assert dumps(backup_dir) == []


def test_unknown_tag():
    st = run(FakeClient({11: [("t-other/x", 5, 1.0)]}))[0][2]
    assert st == "метка не найдена"


def test_missing_config_gives_status_not_crash(monkeypatch, tmp_path):
    from adops.tracker import api
    monkeypatch.setattr(api, "CONFIG_PATH", tmp_path / "nope.json")
    monkeypatch.delenv("TRACKER_API_KEY", raising=False)
    monkeypatch.delenv("TRACKER_BASE_URL", raising=False)
    res = sync.run_sync(ROW, emit=lambda *a, **k: None, client=None)
    assert res[0][2] == "нет ключа"


def test_buckets():
    assert sync.bucket("разделено на 2: camp11 $100; camp22 $100") == "обновлено"
    assert sync.bucket("делю на 2 кампании по 1000 кл. — …") == "готово к записи"
    assert sync.bucket("обновлено $200 (было $7)") == "обновлено"
