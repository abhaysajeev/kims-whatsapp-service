"""WHATSAPP_SEND_ENABLED: the master switch."""
import dataclasses

import pytest

from app.config import PROJECT_ROOT, ConfigError, load_settings
from app.worker import Services, Worker


def reload(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("WHATSAPP_SEND_ENABLED", raising=False)
    else:
        monkeypatch.setenv("WHATSAPP_SEND_ENABLED", value)
    return load_settings(env_file=None, mapping_file=PROJECT_ROOT / "table_mapping.yaml",
                         templates_file=PROJECT_ROOT / "whatsapp_templates.yaml")


@pytest.mark.parametrize("value, expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True),
    ("false", False), ("False", False), ("0", False), ("no", False),
])
def test_switch_values(settings, monkeypatch, value, expected):
    assert reload(monkeypatch, value).worker.send_enabled is expected


@pytest.mark.parametrize("value, message", [
    ("maybe", "must be true or false"),
    ("", "WHATSAPP_SEND_ENABLED is missing"),
    (None, "WHATSAPP_SEND_ENABLED is missing"),
])
def test_switch_must_be_explicit(settings, monkeypatch, value, message):
    with pytest.raises(ConfigError, match=message):
        reload(monkeypatch, value)


class FakeRepo:
    def __init__(self):
        self.claims = 0

    def reset_stuck(self, *_):
        return 0, 0

    def claim_next(self):
        self.claims += 1
        return None


def make_worker(settings, enabled):
    settings = dataclasses.replace(
        settings, worker=dataclasses.replace(settings.worker, send_enabled=enabled))
    worker = Worker(settings, Services(settings=settings, acl=None, bunny=None))
    repo = FakeRepo()
    worker._connect = lambda: repo
    return worker, repo


def test_disabled_worker_never_claims_a_job(settings):
    worker, repo = make_worker(settings, enabled=False)
    assert worker.run_cycle() == 0
    assert repo.claims == 0


def test_enabled_worker_claims(settings):
    worker, repo = make_worker(settings, enabled=True)
    worker.run_cycle()
    assert repo.claims == 1
