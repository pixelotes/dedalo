from datetime import datetime
import pytest

from dedalo import cron


def test_parse_cron_valid():
    m, h, dm, mo, dw = cron.parse_cron("*/15 8-10 1,15 * 1-5")
    assert 0 in m and 15 in m and 30 in m and 45 in m
    assert 8 in h and 9 in h and 10 in h and 11 not in h
    assert 1 in dm and 15 in dm and 2 not in dm
    assert len(mo) == 12
    assert 1 in dw and 5 in dw and 0 not in dw


def test_parse_cron_invalid():
    with pytest.raises(ValueError):
        cron.parse_cron("invalid cron expression")


def test_shortcuts():
    m, h, dm, mo, dw = cron.parse_cron("@hourly")
    assert m == {0}
    assert len(h) == 24


def test_matches_cron():
    dt = datetime(2026, 9, 12, 8, 30)  # Sábado (dw=6)
    assert cron.matches_cron("30 8 * * *", dt) is True
    assert cron.matches_cron("0 8 * * *", dt) is False
    assert cron.matches_cron("30 8 * * 6", dt) is True
    assert cron.matches_cron("30 8 * * 1-5", dt) is False


def test_should_run_interval():
    # Sin last_run debe correr
    assert cron.should_run("@every 10m", last_run=None, now_ts=1000.0) is True
    # Con last_run reciente no debe correr
    assert cron.should_run("@every 10m", last_run=900.0, now_ts=1000.0) is False
    # Pasado el tiempo debe correr
    assert cron.should_run("@every 10m", last_run=300.0, now_ts=1000.0) is True


def test_get_next_run():
    base = datetime(2026, 9, 12, 8, 0, 0)
    nxt = cron.get_next_run("15 8 * * *", base_dt=base)
    assert nxt == "2026-09-12 08:15:00"

    nxt_int = cron.get_next_run("@every 30m", base_dt=base)
    assert nxt_int is not None
