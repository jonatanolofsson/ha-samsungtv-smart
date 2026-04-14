"""Tests for client-side art rotation on Samsung Frame TVs.

Tests the rotation logic in isolation by importing only const.py
(which has no HA dependencies) and testing the rotation function
as a standalone coroutine.
"""

from __future__ import annotations

import asyncio
import random
from unittest.mock import AsyncMock, MagicMock

import pytest

# Import constants directly from const.py, bypassing __init__.py
# (which imports homeassistant and can't be loaded without HA installed)
import importlib.util
import os
_const_path = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "samsungtv_smart", "const.py"
)
_spec = importlib.util.spec_from_file_location("samsungtv_const", _const_path)
_const = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_const)

ART_ROTATION_MINUTES = _const.ART_ROTATION_MINUTES
ART_ROTATION_OPTIONS = _const.ART_ROTATION_OPTIONS
CONF_ART_ROTATION_FULLSCREEN = _const.CONF_ART_ROTATION_FULLSCREEN
CONF_ART_ROTATION_INTERVAL = _const.CONF_ART_ROTATION_INTERVAL
DEFAULT_ART_ROTATION_FULLSCREEN = _const.DEFAULT_ART_ROTATION_FULLSCREEN
DEFAULT_ART_ROTATION_INTERVAL = _const.DEFAULT_ART_ROTATION_INTERVAL
FRAME_HEIGHT = _const.FRAME_HEIGHT
FRAME_WIDTH = _const.FRAME_WIDTH


def _make_image(content_id, width=FRAME_WIDTH, height=FRAME_HEIGHT):
    return {
        "content_id": content_id,
        "category_id": "MY-C0002",
        "width": width,
        "height": height,
    }


IMAGES = [
    _make_image("F001"),
    _make_image("F002"),
    _make_image("F003"),
    _make_image("F004", width=1626, height=2160),  # portrait/wrong size
]


async def rotate_image(art_api, current_id, only_fullscreen=True):
    """Extracted rotation logic for standalone testing.

    This mirrors the logic in SamsungTVArtRotationSelect._async_rotate_image()
    """
    artmode = await art_api.get_artmode()
    if artmode != "on":
        return None

    images = await art_api.available(category="MY-C0002")
    if not images or len(images) < 2:
        return None

    current = await art_api.get_current()
    cur_id = current.get("content_id") if current else None

    if only_fullscreen:
        candidates = [
            img for img in images
            if img.get("width") == FRAME_WIDTH
            and img.get("height") == FRAME_HEIGHT
            and img.get("content_id") != cur_id
        ]
    else:
        candidates = [
            img for img in images
            if img.get("content_id") != cur_id
        ]

    if not candidates:
        return None

    selected = random.choice(candidates)
    await art_api.select_image(selected["content_id"])
    return selected["content_id"]


@pytest.fixture
def art_api():
    api = AsyncMock()
    api.get_artmode = AsyncMock(return_value="on")
    api.get_current = AsyncMock(return_value={"content_id": "F001"})
    api.available = AsyncMock(return_value=list(IMAGES))
    api.select_image = AsyncMock()
    return api


# ── Constants tests ──────────────────────────────────────────────────────

class TestConstants:
    def test_rotation_options(self):
        assert "off" in ART_ROTATION_OPTIONS
        assert "1h" in ART_ROTATION_OPTIONS
        assert "3min" in ART_ROTATION_OPTIONS

    def test_rotation_minutes_mapping(self):
        assert ART_ROTATION_MINUTES["off"] == 0
        assert ART_ROTATION_MINUTES["3min"] == 3
        assert ART_ROTATION_MINUTES["1h"] == 60
        assert ART_ROTATION_MINUTES["1d"] == 1440

    def test_all_options_have_minutes(self):
        for opt in ART_ROTATION_OPTIONS:
            assert opt in ART_ROTATION_MINUTES

    def test_defaults(self):
        assert DEFAULT_ART_ROTATION_INTERVAL == "off"
        assert DEFAULT_ART_ROTATION_FULLSCREEN is True

    def test_frame_dimensions(self):
        assert FRAME_WIDTH == 3840
        assert FRAME_HEIGHT == 2160


# ── Rotation logic tests ────────────────────────────────────────────────

class TestRotateImage:
    @pytest.mark.asyncio
    async def test_selects_different_image(self, art_api):
        result = await rotate_image(art_api, "F001")
        assert result is not None
        assert result != "F001"
        art_api.select_image.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_when_not_art_mode(self, art_api):
        art_api.get_artmode.return_value = "off"
        result = await rotate_image(art_api, "F001")
        assert result is None
        art_api.select_image.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_fewer_than_2(self, art_api):
        art_api.available.return_value = [_make_image("F001")]
        result = await rotate_image(art_api, "F001")
        assert result is None

    @pytest.mark.asyncio
    async def test_fullscreen_excludes_wrong_size(self, art_api):
        """With fullscreen=True, never selects the portrait image."""
        selected = set()
        for _ in range(50):
            art_api.select_image.reset_mock()
            result = await rotate_image(art_api, "F001", only_fullscreen=True)
            if result:
                selected.add(result)
        assert "F004" not in selected
        assert selected.issubset({"F002", "F003"})

    @pytest.mark.asyncio
    async def test_no_filter_includes_all(self, art_api):
        """With fullscreen=False, can select any non-current image."""
        selected = set()
        for _ in range(100):
            art_api.select_image.reset_mock()
            result = await rotate_image(art_api, "F001", only_fullscreen=False)
            if result:
                selected.add(result)
        assert "F004" in selected

    @pytest.mark.asyncio
    async def test_no_candidates(self, art_api):
        """All images are either current or wrong size."""
        art_api.available.return_value = [
            _make_image("F001"),  # current
            _make_image("F004", width=1626, height=2160),
        ]
        result = await rotate_image(art_api, "F001", only_fullscreen=True)
        assert result is None

    @pytest.mark.asyncio
    async def test_api_error_handled(self, art_api):
        art_api.get_artmode.side_effect = Exception("Connection lost")
        with pytest.raises(Exception):
            await rotate_image(art_api, "F001")

    @pytest.mark.asyncio
    async def test_none_artmode_response(self, art_api):
        art_api.get_artmode.return_value = None
        result = await rotate_image(art_api, "F001")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_current(self, art_api):
        """get_current returns None — should still work."""
        art_api.get_current.return_value = None
        result = await rotate_image(art_api, None)
        assert result is not None


# ── Timer config tests ──────────────────────────────────────────────────

class TestTimerConfig:
    def test_off_maps_to_zero(self):
        assert ART_ROTATION_MINUTES["off"] == 0

    def test_all_intervals_positive(self):
        for opt, minutes in ART_ROTATION_MINUTES.items():
            if opt != "off":
                assert minutes > 0, f"{opt} should have positive minutes"

    def test_intervals_increasing(self):
        prev = 0
        for opt in ART_ROTATION_OPTIONS:
            cur = ART_ROTATION_MINUTES[opt]
            assert cur >= prev, f"{opt} ({cur}) should be >= previous ({prev})"
            prev = cur
