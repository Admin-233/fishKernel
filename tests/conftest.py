from __future__ import annotations

import pytest

from fishkernel.archive import Archive, create_archive
from fishkernel.config import load_config
from fishkernel.events import EventLog


@pytest.fixture
def archive(tmp_path) -> Archive:
    return create_archive(tmp_path / "存档", name="测试体", api_key="sk-test", turn_delay=0)


@pytest.fixture
def config(archive):
    return load_config(archive.root)


@pytest.fixture
def log(archive):
    event_log = EventLog(archive.events_path).open()
    yield event_log
    event_log.close()


def open_log(archive: Archive) -> EventLog:
    return EventLog(archive.events_path).open()
