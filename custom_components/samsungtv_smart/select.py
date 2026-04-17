"""Samsung TV Smart - Select entities (matte type/color + picture mode)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import timedelta

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.const import CONF_HOST, CONF_ID, CONF_NAME, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api.art import SamsungTVAsyncArt
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

from .const import (
    ART_ROTATION_MINUTES,
    ART_ROTATION_OPTIONS,
    AUTH_METHOD_OAUTH,
    CONF_API_KEY,
    CONF_ART_ROTATION_FULLSCREEN,
    CONF_ART_ROTATION_INTERVAL,
    CONF_AUTH_METHOD,
    CONF_DEVICE_ID,
    CONF_OAUTH_TOKEN,
    CONF_WS_NAME,
    DATA_ART_API,
    DATA_CFG,
    DEFAULT_ART_ROTATION_FULLSCREEN,
    DEFAULT_ART_ROTATION_INTERVAL,
    DEFAULT_PORT,
    DOMAIN,
    FRAME_HEIGHT,
    FRAME_WIDTH,
    WS_PREFIX,
)

_LOGGER = logging.getLogger(__name__)

# Retry settings when TV is off at startup
_RETRY_INTERVAL = 30  # seconds between retries
_MAX_RETRIES = 10  # give up after 5 minutes

# SmartThings REST API
_API_DEVICES = "https://api.smartthings.com/v1/devices"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Samsung TV select entities from config entry."""
    config = hass.data[DOMAIN][entry.entry_id][DATA_CFG]
    host = config[CONF_HOST]
    port = config.get(CONF_PORT, DEFAULT_PORT)
    token = config.get(CONF_TOKEN)
    ws_name = config.get(CONF_WS_NAME, "HomeAssistant")
    device_unique_id = config.get(CONF_ID, entry.entry_id)
    device_name = config.get(CONF_NAME) or entry.title or host

    session = async_get_clientsession(hass)

    entities = []

    # ── Frame TV Matte selects ────────────────────────────────────────────
    # Reuse shared art_api if already created by sensor.py, otherwise create one
    art_api = hass.data[DOMAIN][entry.entry_id].get(DATA_ART_API)
    if not art_api:
        art_api = SamsungTVAsyncArt(
            host=host,
            port=port,
            token=token,
            session=session,
            timeout=5,
            name=f"{WS_PREFIX} {ws_name} Art Select",
        )

    # Check Frame TV support quickly - if not supported, skip matte entities
    try:
        async with asyncio.timeout(5):
            is_frame_supported = await art_api.supported()
    except Exception:
        is_frame_supported = False

    matte_type_select = None
    matte_color_select = None
    if is_frame_supported:
        matte_type_select = SamsungTVMatteTypeSelect(
            hass, entry, art_api, device_name, device_unique_id
        )
        matte_color_select = SamsungTVMatteColorSelect(
            hass, entry, art_api, device_name, device_unique_id
        )
        entities.extend([matte_type_select, matte_color_select])

    # ── Picture Mode select (SmartThings) ─────────────────────────────────
    api_key = config.get(CONF_API_KEY)
    device_id = config.get(CONF_DEVICE_ID)
    auth_method = config.get(CONF_AUTH_METHOD)
    if auth_method == AUTH_METHOD_OAUTH and not api_key:
        oauth_token = config.get(CONF_OAUTH_TOKEN)
        if oauth_token and isinstance(oauth_token, dict):
            api_key = oauth_token.get("access_token")

    if api_key and device_id:
        picture_mode_select = SamsungTVPictureModeSelect(
            hass=hass,
            entry=entry,
            device_name=device_name,
            device_unique_id=device_unique_id,
            api_key=api_key,
            device_id=device_id,
            session=session,
        )
        entities.append(picture_mode_select)
        _LOGGER.debug("Picture Mode select entity created for %s", device_name)
    else:
        picture_mode_select = None
        _LOGGER.debug(
            "SmartThings not configured for %s, skipping Picture Mode select",
            device_name,
        )

    # ── Art Rotation select (Frame TV only) ─────────────────────────────
    if is_frame_supported:
        rotation_select = SamsungTVArtRotationSelect(
            hass, entry, art_api, device_name, device_unique_id
        )
        entities.append(rotation_select)

    if entities:
        async_add_entities(entities)

    # ── Background tasks ──────────────────────────────────────────────────
    if matte_type_select and matte_color_select:
        hass.async_create_background_task(
            _load_matte_options(hass, art_api, matte_type_select, matte_color_select),
            f"samsungtv_matte_options_{entry.entry_id}",
        )

    if picture_mode_select:
        hass.async_create_background_task(
            _load_picture_mode_options(hass, picture_mode_select),
            f"samsungtv_picture_mode_options_{entry.entry_id}",
        )


# ══════════════════════════════════════════════════════════════════════════
# Background loaders
# ══════════════════════════════════════════════════════════════════════════


async def _load_matte_options(
    hass: HomeAssistant,
    art_api: SamsungTVAsyncArt,
    type_select: "SamsungTVMatteTypeSelect",
    color_select: "SamsungTVMatteColorSelect",
) -> None:
    """Fetch matte list from TV and populate select options, with retries."""
    for attempt in range(_MAX_RETRIES):
        try:
            async with asyncio.timeout(10):
                matte_types, matte_colors = await art_api.get_matte_list(
                    include_color=True
                )

            type_options = [
                m["matte_type"] if isinstance(m, dict) else str(m) for m in matte_types
            ]
            color_options = [
                m["color"] if isinstance(m, dict) else str(m) for m in matte_colors
            ]

            if type_options:
                type_select.set_options(type_options)
            if color_options:
                color_select.set_options(color_options)

            _LOGGER.info(
                "Matte selects populated — types: %s | colors: %s",
                type_options,
                color_options,
            )
            return

        except asyncio.TimeoutError:
            _LOGGER.debug(
                "Timeout fetching matte list (attempt %d/%d), retrying in %ds",
                attempt + 1,
                _MAX_RETRIES,
                _RETRY_INTERVAL,
            )
        except Exception as ex:
            _LOGGER.debug(
                "Error fetching matte list (attempt %d/%d): %s",
                attempt + 1,
                _MAX_RETRIES,
                ex,
            )

        await asyncio.sleep(_RETRY_INTERVAL)

    _LOGGER.warning("Could not populate matte options after %d attempts", _MAX_RETRIES)


async def _load_picture_mode_options(
    hass: HomeAssistant,
    select_entity: "SamsungTVPictureModeSelect",
) -> None:
    """Fetch picture mode list from SmartThings, with retries."""
    for attempt in range(_MAX_RETRIES):
        try:
            async with asyncio.timeout(10):
                await select_entity.async_fetch_picture_modes()

            if select_entity.options:
                _LOGGER.info(
                    "Picture mode select populated — modes: %s",
                    select_entity.options,
                )
                return

        except asyncio.TimeoutError:
            _LOGGER.debug(
                "Timeout fetching picture modes (attempt %d/%d), retrying in %ds",
                attempt + 1,
                _MAX_RETRIES,
                _RETRY_INTERVAL,
            )
        except Exception as ex:
            _LOGGER.debug(
                "Error fetching picture modes (attempt %d/%d): %s",
                attempt + 1,
                _MAX_RETRIES,
                ex,
            )

        await asyncio.sleep(_RETRY_INTERVAL)

    _LOGGER.warning(
        "Could not populate picture mode options after %d attempts", _MAX_RETRIES
    )


# ══════════════════════════════════════════════════════════════════════════
# Matte select entities (Frame TV only)
# ══════════════════════════════════════════════════════════════════════════


class SamsungTVMatteSelectBase(SelectEntity):
    """Base class for Samsung Frame TV matte select entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        art_api: SamsungTVAsyncArt,
        device_name: str,
        device_unique_id: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._art_api = art_api
        self._device_name = device_name
        self._device_unique_id = device_unique_id
        self._attr_options: list[str] = []
        self._attr_current_option: str | None = None
        self._attr_should_poll = False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_unique_id)},
            name=self._device_name,
        )

    def set_options(self, options: list[str]) -> None:
        """Update available options and refresh HA state."""
        self._attr_options = options
        if self._attr_current_option not in options:
            self._attr_current_option = options[0] if options else None
        if self.platform is not None:
            self.async_write_ha_state()

    async def _async_refresh_current(self) -> None:
        """Read current artwork matte from TV and update state."""
        try:
            async with asyncio.timeout(5):
                current = await self._art_api.get_current()
            if current:
                matte_id: str = current.get("matte_id", "")
                self._parse_matte_id(matte_id)
                self.async_write_ha_state()
        except Exception as ex:
            _LOGGER.debug("Could not refresh current matte: %s", ex)

    def _parse_matte_id(self, matte_id: str) -> None:
        """To be implemented by subclass."""
        raise NotImplementedError


class SamsungTVMatteTypeSelect(SamsungTVMatteSelectBase):
    """Select entity for matte frame type (e.g. modern, shadowbox, triptych...)."""

    _attr_icon = "mdi:border-style"
    _attr_has_entity_name = True

    def __init__(self, hass, entry, art_api, device_name, device_unique_id):
        super().__init__(hass, entry, art_api, device_name, device_unique_id)
        self._attr_unique_id = f"{device_unique_id}_matte_type"
        self._attr_name = "Matte Type"
        self._attr_options = ["none"]
        self._attr_current_option = "none"

    def _parse_matte_id(self, matte_id: str) -> None:
        """Extract type part from matte_id (format: type_color or just type)."""
        if "_" in matte_id:
            matte_type = matte_id.rsplit("_", 1)[0]
        else:
            matte_type = matte_id or "none"
        if matte_type in self._attr_options:
            self._attr_current_option = matte_type

    async def async_select_option(self, option: str) -> None:
        """Called when user picks a new matte type."""
        try:
            current = await self._art_api.get_current()
            if not current:
                _LOGGER.warning("Cannot change matte type: no current artwork")
                return

            content_id: str = current.get("content_id", "")
            existing_matte: str = current.get("matte_id", "none_polar")

            if "_" in existing_matte:
                color_part = existing_matte.rsplit("_", 1)[1]
            else:
                color_part = "polar"

            new_matte_id = f"{option}_{color_part}" if option != "none" else "none"

            await self._art_api.change_matte(
                content_id=content_id, matte_id=new_matte_id
            )
            self._attr_current_option = option
            self.async_write_ha_state()
            _LOGGER.debug(
                "Matte type changed to %s (full id: %s)", option, new_matte_id
            )

        except Exception as ex:
            _LOGGER.error("Error changing matte type: %s", ex)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_refresh_current()


class SamsungTVMatteColorSelect(SamsungTVMatteSelectBase):
    """Select entity for matte color (e.g. polar, black, apricot...)."""

    _attr_icon = "mdi:palette"
    _attr_has_entity_name = True

    def __init__(self, hass, entry, art_api, device_name, device_unique_id):
        super().__init__(hass, entry, art_api, device_name, device_unique_id)
        self._attr_unique_id = f"{device_unique_id}_matte_color"
        self._attr_name = "Matte Color"
        self._attr_options = ["polar"]
        self._attr_current_option = "polar"

    def _parse_matte_id(self, matte_id: str) -> None:
        """Extract color part from matte_id (format: type_color)."""
        if "_" in matte_id:
            color = matte_id.rsplit("_", 1)[1]
            if color in self._attr_options:
                self._attr_current_option = color

    async def async_select_option(self, option: str) -> None:
        """Called when user picks a new matte color."""
        try:
            current = await self._art_api.get_current()
            if not current:
                _LOGGER.warning("Cannot change matte color: no current artwork")
                return

            content_id: str = current.get("content_id", "")
            existing_matte: str = current.get("matte_id", "none")

            if "_" in existing_matte:
                type_part = existing_matte.rsplit("_", 1)[0]
            else:
                type_part = existing_matte or "shadowbox"

            new_matte_id = f"{type_part}_{option}"

            await self._art_api.change_matte(
                content_id=content_id, matte_id=new_matte_id
            )
            self._attr_current_option = option
            self.async_write_ha_state()
            _LOGGER.debug(
                "Matte color changed to %s (full id: %s)", option, new_matte_id
            )

        except Exception as ex:
            _LOGGER.error("Error changing matte color: %s", ex)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_refresh_current()


# ══════════════════════════════════════════════════════════════════════════
# Picture Mode select entity (SmartThings)
# ══════════════════════════════════════════════════════════════════════════


class SamsungTVPictureModeSelect(SelectEntity):
    """Select entity for Samsung TV picture mode (Standard, Movie, Dynamic, etc.)."""

    _attr_icon = "mdi:image-filter-hdr"
    _attr_has_entity_name = True
    _attr_should_poll = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_name: str,
        device_unique_id: str,
        api_key: str,
        device_id: str,
        session,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._device_name = device_name
        self._device_unique_id = device_unique_id
        self._api_key = api_key
        self._device_id = device_id
        self._session = session

        self._attr_unique_id = f"{device_unique_id}_picture_mode"
        self._attr_name = "Picture Mode"
        self._attr_options: list[str] = []
        self._attr_current_option: str | None = None

        # Map display name -> API id (e.g. "Standard" -> "modeStandard")
        self._mode_map: dict[str, str] = {}
        # Which capability the TV uses
        self._capability: str | None = None
        # Cooldown: skip polls briefly after setting a mode
        self._skip_poll_until: float = 0

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_unique_id)},
            name=self._device_name,
        )

    @property
    def available(self) -> bool:
        """Only available when we have options loaded."""
        return len(self._attr_options) > 0

    def _get_api_key(self) -> str:
        """Get current API key, refreshing from entry data for OAuth."""
        entry = self.hass.config_entries.async_get_entry(self._entry.entry_id)
        if entry:
            auth_method = entry.data.get(CONF_AUTH_METHOD)
            if auth_method == AUTH_METHOD_OAUTH:
                oauth_token = entry.data.get(CONF_OAUTH_TOKEN)
                if oauth_token and isinstance(oauth_token, dict):
                    new_key = oauth_token.get("access_token")
                    if new_key:
                        self._api_key = new_key
                        return new_key
            api_key = entry.data.get(CONF_API_KEY)
            if api_key:
                self._api_key = api_key
        return self._api_key

    async def _rest_get_capability_status(self, capability: str) -> dict | None:
        """Fetch capability status via SmartThings REST API."""
        api_key = self._get_api_key()
        url = (
            f"{_API_DEVICES}/{self._device_id}"
            f"/components/main/capabilities/{capability}/status"
        )
        try:
            async with self._session.get(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                },
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception as ex:
            _LOGGER.debug("Error fetching picture mode capability: %s", ex)
            return None

    async def async_fetch_picture_modes(self) -> None:
        """Fetch picture mode list and current mode from SmartThings REST API."""
        for cap_name in ("samsungvd.pictureMode", "custom.picturemode"):
            data = await self._rest_get_capability_status(cap_name)
            if not data:
                continue

            # Parse supported modes map
            raw_map = data.get("supportedPictureModesMap", {}).get("value")
            if raw_map:
                new_map: dict[str, str] = {}
                for entry in raw_map:
                    if isinstance(entry, dict):
                        m_id = entry.get("id", "")
                        m_name = entry.get("name", m_id)
                    elif isinstance(entry, str):
                        m_id = m_name = entry
                    else:
                        continue
                    if m_id:
                        new_map[m_name] = m_id
                if new_map:
                    self._mode_map = new_map
                    self._attr_options = list(new_map.keys())
                    self._capability = cap_name

            # Fallback: supportedPictureModes (names only)
            if not self._attr_options:
                raw_list = data.get("supportedPictureModes", {}).get("value")
                if raw_list and isinstance(raw_list, list):
                    self._attr_options = raw_list
                    self._capability = cap_name

            # Read current mode
            raw_mode = data.get("pictureMode", {}).get("value")
            if raw_mode and self._attr_options:
                if self._mode_map:
                    reverse = {v: k for k, v in self._mode_map.items()}
                    self._attr_current_option = reverse.get(raw_mode, raw_mode)
                else:
                    self._attr_current_option = raw_mode

                if self.platform is not None:
                    self.async_write_ha_state()

                _LOGGER.debug(
                    "Picture mode: current=%s, options=%s (cap=%s)",
                    self._attr_current_option,
                    self._attr_options,
                    cap_name,
                )
                return

        _LOGGER.debug("No picture mode capability found on this TV")

    async def async_select_option(self, option: str) -> None:
        """Called when user picks a new picture mode.

        Delegates to the samsungtv_smart.select_picture_mode service which
        uses the SmartThingsTV instance (with active OAuth token refresh).
        Direct REST calls from select.py used a potentially stale token.
        """
        # Find the media_player entity for this device
        entity_id = self._get_media_player_entity_id()
        if not entity_id:
            _LOGGER.error(
                "Picture mode: no media_player entity found for %s",
                self._device_name,
            )
            return

        try:
            await self.hass.services.async_call(
                DOMAIN,
                "select_picture_mode",
                {"entity_id": entity_id, "picture_mode": option},
                blocking=True,
            )
            _LOGGER.debug("Picture mode set to %s via service", option)

            self._attr_current_option = option
            self.async_write_ha_state()
            # Skip polls for 5 seconds to let the TV apply the change;
            # without this, the next async_update() reads the OLD mode
            # from the TV (which hasn't switched yet) and overwrites ours.
            self._skip_poll_until = time.time() + 5

        except Exception as ex:
            _LOGGER.error("Error setting picture mode to %s: %s", option, ex)

    def _get_media_player_entity_id(self) -> str | None:
        """Find the media_player entity for this TV."""
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self.hass)
        for entity in registry.entities.get_entries_for_config_entry_id(
            self._entry.entry_id
        ):
            if entity.domain == "media_player":
                return entity.entity_id
        return None

    async def async_update(self) -> None:
        """Poll current picture mode from SmartThings."""
        if not self._capability:
            return

        # Skip polling briefly after a mode change to let the TV apply it
        if time.time() < self._skip_poll_until:
            return

        data = await self._rest_get_capability_status(self._capability)
        if not data:
            return

        raw_mode = data.get("pictureMode", {}).get("value")
        if not raw_mode:
            return

        if self._mode_map:
            reverse = {v: k for k, v in self._mode_map.items()}
            new_mode = reverse.get(raw_mode, raw_mode)
        else:
            new_mode = raw_mode

        # If the TV reports a mode we don't know about (e.g. Filmmaker),
        # add it dynamically to options and map
        if new_mode not in self._attr_options:
            _LOGGER.info(
                "Picture mode: discovered new mode '%s' (raw: %s), adding to options",
                new_mode,
                raw_mode,
            )
            self._attr_options = [*self._attr_options, new_mode]
            if raw_mode != new_mode:
                self._mode_map[new_mode] = raw_mode

        if new_mode != self._attr_current_option:
            _LOGGER.debug(
                "Picture mode updated: %s -> %s", self._attr_current_option, new_mode
            )
            self._attr_current_option = new_mode


# ══════════════════════════════════════════════════════════════════════════
# Art Rotation Select
# ══════════════════════════════════════════════════════════════════════════


class SamsungTVArtRotationSelect(SelectEntity):
    """Select entity for client-side art rotation interval.

    Samsung's set_auto_rotation_status WebSocket API is unreliable (times out
    on many models). This entity implements client-side rotation by calling
    select_image on a timer, following the approach used by Nick Waterton's
    samsung-tv-ws-api reference implementation.
    """

    _attr_icon = "mdi:image-auto-adjust"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        art_api: SamsungTVAsyncArt,
        device_name: str,
        device_unique_id: str,
    ) -> None:
        """Initialize the art rotation select."""
        self._hass = hass
        self._entry = entry
        self._art_api = art_api
        self._device_unique_id = device_unique_id
        self._rotation_unsub: Callable | None = None
        self._state_unsub: Callable | None = None

        self._attr_unique_id = f"{device_unique_id}_art_rotation"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_unique_id)},
        )
        self._attr_options = ART_ROTATION_OPTIONS
        self._attr_current_option = entry.options.get(
            CONF_ART_ROTATION_INTERVAL, DEFAULT_ART_ROTATION_INTERVAL
        )
        self._only_fullscreen = entry.options.get(
            CONF_ART_ROTATION_FULLSCREEN, DEFAULT_ART_ROTATION_FULLSCREEN
        )
        self._attr_name = "Art Rotation"

    async def async_added_to_hass(self) -> None:
        """Start rotation timer and listen to TV state changes."""
        if self._attr_current_option != "off":
            self._start_rotation_timer()

        # Listen to media_player state to auto-pause/resume rotation.
        # This prevents WebSocket calls from waking the TV when it's off.
        mp_entity_id = self._find_media_player_entity_id()
        if mp_entity_id:
            self._state_unsub = async_track_state_change_event(
                self._hass, mp_entity_id, self._on_tv_state_change
            )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel rotation timer and state listener on entity removal."""
        self._cancel_rotation_timer()
        if self._state_unsub:
            self._state_unsub()
            self._state_unsub = None

    @callback
    def _on_tv_state_change(self, event) -> None:
        """Auto-pause rotation when TV turns off, resume when on."""
        new_state = event.data.get("new_state")
        if not new_state:
            return

        if new_state.state in ("off", "unavailable"):
            if self._rotation_unsub:
                _LOGGER.info("Art rotation paused: TV is %s", new_state.state)
                self._cancel_rotation_timer()
        elif new_state.state == "on":
            if self._attr_current_option != "off" and not self._rotation_unsub:
                _LOGGER.info("Art rotation resumed: TV is on")
                self._start_rotation_timer()

    async def async_select_option(self, option: str) -> None:
        """Handle interval selection."""
        self._cancel_rotation_timer()
        self._attr_current_option = option
        self.async_write_ha_state()

        # Persist in config entry options
        new_options = {**self._entry.options, CONF_ART_ROTATION_INTERVAL: option}
        self._hass.config_entries.async_update_entry(self._entry, options=new_options)

        if option != "off":
            self._start_rotation_timer()
            _LOGGER.info("Art rotation set to %s", option)
        else:
            _LOGGER.info("Art rotation disabled")

    def _start_rotation_timer(self) -> None:
        """Start the periodic rotation timer."""
        minutes = ART_ROTATION_MINUTES.get(self._attr_current_option, 0)
        if minutes <= 0:
            return
        self._rotation_unsub = async_track_time_interval(
            self._hass,
            self._async_rotate_image,
            timedelta(minutes=minutes),
        )
        _LOGGER.debug("Art rotation timer started: every %d minutes", minutes)

    def _cancel_rotation_timer(self) -> None:
        """Cancel the periodic rotation timer."""
        if self._rotation_unsub:
            self._rotation_unsub()
            self._rotation_unsub = None

    def _find_media_player_entity_id(self) -> str | None:
        """Find the media player entity for this TV."""
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(self._hass)
        for entity in registry.entities.values():
            if (entity.config_entry_id == self._entry.entry_id
                    and entity.domain == "media_player"):
                return entity.entity_id
        return None

    async def _async_rotate_image(self, _now=None) -> None:
        """Select a random image from My Photos."""
        import random

        try:
            # Check media_player state first — avoids WebSocket connection
            # that could wake the TV from standby
            mp_entity_id = self._find_media_player_entity_id()
            if mp_entity_id:
                state = self._hass.states.get(mp_entity_id)
                if state and state.state in ("off", "unavailable", "unknown"):
                    _LOGGER.debug("Art rotation skipped: TV is %s", state.state)
                    return

            # Only rotate when in art mode
            artmode = await self._art_api.get_artmode()
            if artmode != "on":
                _LOGGER.debug("Art rotation skipped: not in art mode (%s)", artmode)
                return

            # Get available images
            images = await self._art_api.available(category="MY-C0002")
            if not images or len(images) < 2:
                _LOGGER.debug("Art rotation skipped: fewer than 2 images available")
                return

            # Get current image to avoid re-selecting it
            current = await self._art_api.get_current()
            current_id = current.get("content_id") if current else None

            # Optionally filter to fullscreen images only
            self._only_fullscreen = self._entry.options.get(
                CONF_ART_ROTATION_FULLSCREEN, DEFAULT_ART_ROTATION_FULLSCREEN
            )
            if self._only_fullscreen:
                candidates = [
                    img for img in images
                    if img.get("width") == FRAME_WIDTH
                    and img.get("height") == FRAME_HEIGHT
                    and img.get("content_id") != current_id
                ]
            else:
                candidates = [
                    img for img in images
                    if img.get("content_id") != current_id
                ]

            if not candidates:
                _LOGGER.debug("Art rotation skipped: no eligible candidates")
                return

            selected = random.choice(candidates)
            await self._art_api.select_image(selected["content_id"])
            _LOGGER.info(
                "Art rotation: switched to %s (%d candidates)",
                selected["content_id"],
                len(candidates),
            )
        except Exception as ex:
            _LOGGER.error("Art rotation failed: %s", ex)
