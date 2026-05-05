from __future__ import annotations

import pytest

from monitoring_board.services.fusionsolar import build_provider_url, map_fusionsolar_status, normalize_sync_hours


def test_normalize_sync_hours_keeps_two_valid_times() -> None:
    assert normalize_sync_hours("07:30, 18:45") == "07:30,18:45"


def test_normalize_sync_hours_falls_back_when_invalid() -> None:
    assert normalize_sync_hours("99:99") == "08:00,14:00"


def test_build_provider_url_requires_complete_config() -> None:
    assert build_provider_url("https://example.test/", "/thirdData/login") == "https://example.test/thirdData/login"
    with pytest.raises(ValueError, match="base URL"):
        build_provider_url("", "/thirdData/login")


def test_map_fusionsolar_status_codes() -> None:
    assert map_fusionsolar_status("1") == "Desconectada"
    assert map_fusionsolar_status("2") == "Erro"
    assert map_fusionsolar_status("3") == "Operacional"
