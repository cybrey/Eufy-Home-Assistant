"""PTZ buttons for the PTZ lens of each dual-lens camera.

A stream is steerable when go2rtc also publishes its ``<stream>_wide`` sibling: the
add-on only does that for dual-lens cameras, whose default stream is the PTZ lens.
Commands ride the NVR's single live session, so they work only while that camera is
being viewed live (for example on a live picture card); otherwise the press fails
with a clear error instead of interrupting someone else's view.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EufyNvrConfigEntry
from .camera import _friendly_name
from .const import DOMAIN
from .coordinator import EufyNvrCoordinator
from .go2rtc_api import Go2RtcError, PtzNotLiveError, ptz_streams

_LOGGER = logging.getLogger(__name__)

PRESET_SLOTS = 8


@dataclass(frozen=True, kw_only=True)
class PtzButtonDescription(ButtonEntityDescription):
    """A button that sends one fixed PTZ command."""

    label: str
    command: dict[str, Any] = field(default_factory=dict)


BUTTONS: tuple[PtzButtonDescription, ...] = (
    PtzButtonDescription(key="pan_left", label="Pan left", icon="mdi:arrow-left-bold",
                         command={"action": "move", "direction": "left"}),
    PtzButtonDescription(key="pan_right", label="Pan right", icon="mdi:arrow-right-bold",
                         command={"action": "move", "direction": "right"}),
    PtzButtonDescription(key="tilt_up", label="Tilt up", icon="mdi:arrow-up-bold",
                         command={"action": "move", "direction": "up"}),
    PtzButtonDescription(key="tilt_down", label="Tilt down", icon="mdi:arrow-down-bold",
                         command={"action": "move", "direction": "down"}),
    PtzButtonDescription(key="zoom_in", label="Zoom in", icon="mdi:magnify-plus-outline",
                         command={"action": "zoom", "step": 1}),
    PtzButtonDescription(key="zoom_out", label="Zoom out", icon="mdi:magnify-minus-outline",
                         command={"action": "zoom", "step": -1}),
    *(
        # Disabled by default: enable the slots saved in the Eufy app.
        PtzButtonDescription(key=f"preset_{slot}", label=f"Preset {slot}",
                             icon="mdi:map-marker-radius",
                             entity_registry_enabled_default=False,
                             command={"action": "preset", "preset": slot})
        for slot in range(1, PRESET_SLOTS + 1)
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EufyNvrConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add PTZ buttons for steerable cameras, including ones discovered later."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_add_new_buttons() -> None:
        new = set(ptz_streams(coordinator.data or {})) - known
        if not new:
            return
        known.update(new)
        async_add_entities(
            EufyNvrPtzButton(coordinator, entry.entry_id, stream, description)
            for stream in sorted(new)
            for description in BUTTONS
        )

    _async_add_new_buttons()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_buttons))


class EufyNvrPtzButton(CoordinatorEntity[EufyNvrCoordinator], ButtonEntity):
    """Press to move, zoom or recall a preset on one live PTZ camera."""

    _attr_has_entity_name = True
    entity_description: PtzButtonDescription

    def __init__(
        self,
        coordinator: EufyNvrCoordinator,
        entry_id: str,
        stream: str,
        description: PtzButtonDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._stream = stream
        self._camera = _friendly_name(stream)
        self._attr_name = f"{self._camera} {description.label}"
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{stream}_{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry_id)})

    @property
    def available(self) -> bool:
        """Available while go2rtc is reachable and the camera still exists."""
        return super().available and self._stream in (self.coordinator.data or {})

    async def async_press(self) -> None:
        """Send the command to the camera's live session."""
        try:
            result = await self.coordinator.async_ptz(
                self._stream, self.entity_description.command
            )
        except PtzNotLiveError as err:
            raise HomeAssistantError(
                f"{self._camera} is not live. Open its live view, then try again."
            ) from err
        except Go2RtcError as err:
            raise HomeAssistantError(f"{self._camera} PTZ failed: {err}") from err
        if not result.get("acked"):
            _LOGGER.debug("%s %s sent; the NVR did not confirm it", self._stream,
                          self.entity_description.key)
