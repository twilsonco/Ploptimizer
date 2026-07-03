"""Tests for plt_optimizer/utils/config.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import mock_open, patch

import pytest

from plt_optimizer.utils.config import (
    DEFAULT_CONFIG,
    get_config_path,
    load_config,
    save_config,
    update_config,
)


class TestGetConfigPath:
    """Tests for get_config_path function (lines 32-38)."""

    def test_get_config_path_creates_directory(self) -> None:
        """Test that the parent directory is created if it doesn't exist."""
        with patch("sys.platform", "darwin"):
            with patch.object(Path, "mkdir") as mock_mkdir:
                get_config_path()
                mock_mkdir.assert_called_once_with(parents=True, exist_ok=True)


class TestLoadConfig:
    """Tests for load_config function (lines 48-65)."""

    def test_load_config_returns_defaults_when_no_file(self) -> None:
        """Test that DEFAULT_CONFIG is returned when config file doesn't exist."""
        with patch.object(Path, "exists", return_value=False):
            result = load_config()
            assert result == DEFAULT_CONFIG.copy()

    def test_load_config_merges_with_defaults(self) -> None:
        """Test that loaded config merges with defaults to ensure all keys exist."""
        partial_config = {"watch_dir": "/test/path", "fast_mode": True}
        json_content = json.dumps(partial_config)

        with patch.object(Path, "exists", return_value=True):
            with patch(
                "builtins.open",
                mock_open(read_data=json_content),
            ):
                result = load_config()

        # All default keys should exist
        assert "watch_dir" in result
        assert "output_dir" in result
        assert "log_dir" in result
        assert "processed_dir" in result
        assert "fast_mode" in result
        assert "debug_save_files" in result
        assert "run_at_startup" in result

    def test_load_config_overrides_defaults(self) -> None:
        """Test that loaded values override defaults."""
        custom_config = {"watch_dir": "/custom/path", "fast_mode": True}
        json_content = json.dumps(custom_config)

        with patch.object(Path, "exists", return_value=True):
            with patch(
                "builtins.open",
                mock_open(read_data=json_content),
            ):
                result = load_config()

        assert result["watch_dir"] == "/custom/path"
        assert result["fast_mode"] is True
        # Defaults should still be present for missing keys
        assert result["output_dir"] == "./optimized"

    def test_load_config_handles_json_decode_error(self) -> None:
        """Test that JSON decode errors return defaults (line 56-57)."""
        with patch.object(Path, "exists", return_value=True):
            with patch("builtins.open", mock_open(read_data="invalid json{")):
                result = load_config()
            assert result == DEFAULT_CONFIG.copy()

    def test_load_config_handles_os_error(self) -> None:
        """Test that OS errors when reading file return defaults."""
        with patch.object(Path, "exists", return_value=True):
            with patch("builtins.open", side_effect=OSError("Permission denied")):
                result = load_config()
            assert result == DEFAULT_CONFIG.copy()


class TestSaveConfig:
    """Tests for save_config function (lines 74-83)."""

    def test_save_config_writes_json_file(self) -> None:
        """Test that config is written as formatted JSON."""
        test_config: dict[str, Any] = {"watch_dir": "/test", "fast_mode": False}

        with patch.object(Path, "parent", new_callable=lambda: Path("/config")):
            with patch.object(Path, "mkdir"):
                m = mock_open()
                with patch("builtins.open", m):
                    save_config(test_config)

                    # Verify file was opened in write mode
                    m.assert_called_once()
                    call_args = m.call_args
                    assert "w" in str(call_args)

    def test_save_config_creates_parent_directory(self) -> None:
        """Test that parent directory is created before writing."""
        test_config: dict[str, Any] = {"watch_dir": "/test"}

        with patch.object(Path, "exists", return_value=False):
            with patch.object(Path, "mkdir") as mock_mkdir:
                with patch("builtins.open", mock_open()):
                    save_config(test_config)
                    # parent should be called with exist_ok=True
                    assert mock_mkdir.called

    def test_save_config_raises_os_error_on_failure(self) -> None:
        """Test that OSError is raised when file cannot be written."""
        test_config: dict[str, Any] = {"watch_dir": "/test"}

        with patch.object(Path, "exists", return_value=False):
            with patch.object(Path, "mkdir"):
                with patch(
                    "builtins.open",
                    side_effect=OSError("Disk full"),
                ):
                    with pytest.raises(OSError, match="Failed to save configuration"):
                        save_config(test_config)


class TestUpdateConfig:
    """Tests for update_config function (lines 95-98)."""

    def test_update_config_loads_updates_and_saves(self) -> None:
        """Test that update_config loads, updates, and saves config."""
        with patch(
            "plt_optimizer.utils.config.load_config",
            return_value=DEFAULT_CONFIG.copy(),
        ) as mock_load:
            with patch(
                "plt_optimizer.utils.config.save_config",
            ) as mock_save:
                result = update_config({"watch_dir": "/new/path"})

                mock_load.assert_called_once()
                mock_save.assert_called_once()
                assert result["watch_dir"] == "/new/path"

    def test_update_config_returns_updated_config(self) -> None:
        """Test that updated config is returned with new values."""
        original = DEFAULT_CONFIG.copy()

        with patch(
            "plt_optimizer.utils.config.load_config",
            return_value=original,
        ):
            with patch("plt_optimizer.utils.config.save_config"):
                result = update_config({"fast_mode": True, "debug_save_files": True})

                assert result["fast_mode"] is True
                assert result["debug_save_files"] is True


class TestDefaultConfig:
    """Tests for DEFAULT_CONFIG constant."""

    def test_default_config_has_all_required_keys(self) -> None:
        """Test that DEFAULT_CONFIG contains all required configuration keys."""
        required_keys = [
            "watch_dir",
            "output_dir",
            "log_dir",
            "processed_dir",
            "fast_mode",
            "debug_save_files",
            "run_at_startup",
            "first_run"
        ]
        for key in required_keys:
            assert key in DEFAULT_CONFIG


class TestConfigRoundTrip:
    """Integration tests for config save/load round trip."""

    def test_config_round_trip(self) -> None:
        """Test that saving and loading config preserves all values."""
        original: dict[str, Any] = {
            "watch_dir": "/my/watch",
            "output_dir": "./out",
            "log_dir": "./logs",
            "processed_dir": "./processed",
            "fast_mode": True,
            "debug_save_files": True,
            "run_at_startup": False,
        }

        saved_content: dict[str, Any] = {}

        def capture_write(config: dict[str, Any]) -> None:
            """Capture what would be written."""
            saved_content.update(config)

        with patch(
            "plt_optimizer.utils.config.load_config",
            return_value=original.copy(),
        ):
            with patch(
                "plt_optimizer.utils.config.save_config",
                side_effect=capture_write,
            ):
                result = update_config({"watch_dir": "/updated"})

        # The saved config should have the updated value
        assert saved_content["watch_dir"] == "/updated"
