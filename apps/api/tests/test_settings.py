from __future__ import annotations

import pytest


def test_missing_data_directory_has_clear_error() -> None:
    from market_monitor_api.settings import SettingsError, load_settings

    with pytest.raises(SettingsError, match="MARKET_MONITOR_DATA_DIR"):
        load_settings({})
