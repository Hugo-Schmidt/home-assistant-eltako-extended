"""Support for Eltako covers. - v1510 Patch1"""
from __future__ import annotations

import asyncio
from typing import Any

from eltakobus.util import AddressExpression
from eltakobus.eep import *

from homeassistant import config_entries
from homeassistant.components.cover import CoverEntity, CoverEntityFeature, ATTR_POSITION, ATTR_TILT_POSITION
from homeassistant.const import CONF_DEVICE_CLASS, Platform, STATE_OPEN, STATE_OPENING, STATE_CLOSED, STATE_CLOSING
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType

from .device import *
from . import config_helpers 
from .config_helpers import DeviceConf
from .gateway import EnOceanGateway
from .const import CONF_SENDER, CONF_TIME_CLOSES, CONF_TIME_OPENS, CONF_TIME_TILTS, DOMAIN, MANUFACTURER, LOGGER
from . import get_gateway_from_hass, get_device_config_for_gateway
import time as time_module  # renamed to avoid shadowing by local 'time' variables

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Eltako cover platform."""
    gateway: EnOceanGateway = get_gateway_from_hass(hass, config_entry)
    config: ConfigType = get_device_config_for_gateway(hass, config_entry, gateway)

    entities: list[EltakoEntity] = []
    
    platform = Platform.COVER
    if platform in config:
        for entity_config in config[platform]:

            try:
                dev_conf = DeviceConf(entity_config, [CONF_DEVICE_CLASS, CONF_TIME_CLOSES, CONF_TIME_OPENS, CONF_TIME_TILTS])
                sender_config = config_helpers.get_device_conf(entity_config, CONF_SENDER)

                entities.append(EltakoCover(platform, gateway, dev_conf.id, dev_conf.name, dev_conf.eep, 
                                            sender_config.id, sender_config.eep, 
                                            dev_conf.get(CONF_DEVICE_CLASS), dev_conf.get(CONF_TIME_CLOSES), dev_conf.get(CONF_TIME_OPENS), dev_conf.get(CONF_TIME_TILTS)))

            except Exception as e:
                LOGGER.warning("[%s] Could not load configuration", platform)
                LOGGER.critical(e, exc_info=True)
                
        
    validate_actuators_dev_and_sender_id(entities)
    log_entities_to_be_added(entities, platform)
    async_add_entities(entities)

class EltakoCover(EltakoEntity, CoverEntity, RestoreEntity):
    """Representation of an Eltako cover device."""

    def __init__(self, platform:str, gateway: EnOceanGateway, dev_id: AddressExpression, dev_name: str, dev_eep: EEP, sender_id: AddressExpression, sender_eep: EEP, device_class: str, time_closes, time_opens, time_tilts):
        """Initialize the Eltako cover device."""
        super().__init__(platform, gateway, dev_id, dev_name, dev_eep)
        self._sender_id = sender_id
        self._sender_eep = sender_eep

        self._attr_device_class = device_class
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._attr_is_closed = None # means undefined state
        self._attr_current_cover_position = None
        self._attr_current_cover_tilt_position = None
        self._time_closes = time_closes
        self._time_opens = time_opens
        self._time_tilts = time_tilts

        # --- FSB14 time-based position tracking ---
        self._travel_task = None          # asyncio Task running during movement
        self._travel_start_time = None    # monotonic timestamp when movement started
        self._travel_direction = None     # "up" or "down"
        self._travel_start_position = None  # position at movement start (0-100)
        self._travel_target_position = None # position at movement end (0-100)
        # ------------------------------------------
        
        self._attr_supported_features = (CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP)
        
        if time_tilts is not None:
            self._attr_supported_features |= CoverEntityFeature.SET_TILT_POSITION

        if time_closes is not None and time_opens is not None:
            self._attr_supported_features |= CoverEntityFeature.SET_POSITION


    def load_value_initially(self, latest_state:State):
        try:
            self._attr_current_cover_position = latest_state.attributes.get('current_position')
            self._attr_current_cover_tilt_position = latest_state.attributes.get('current_tilt_position')

            if latest_state.state == STATE_OPEN:
                self._attr_is_opening = False
                self._attr_is_closing = False
                self._attr_is_closed = False
                self._attr_current_cover_position = 100
                self._attr_current_cover_tilt_position = 100
            elif latest_state.state == STATE_CLOSED:
                self._attr_is_opening = False
                self._attr_is_closing = False
                self._attr_is_closed = True
                self._attr_current_cover_position = 0
                self._attr_current_cover_tilt_position = 0
            elif latest_state.state == STATE_CLOSING:
                self._attr_is_opening = False
                self._attr_is_closing = True
                self._attr_is_closed = False
            elif latest_state.state == STATE_OPENING:
                self._attr_is_opening = True
                self._attr_is_closing = False
                self._attr_is_closed = False
            
        except Exception as e:
            self._attr_current_cover_position = None
            self._attr_current_cover_tilt_position = None
            self._attr_is_opening = None
            self._attr_is_closing = None
            self._attr_is_closed = None # means undefined state
        
        self.schedule_update_ha_state()
        LOGGER.debug(f"[cover {self.dev_id}] value initially loaded: [" 
                     + f"is_opening: {self.is_opening}, "
                     + f"is_closing: {self.is_closing}, "
                     + f"is_closed: {self.is_closed}, "
                     + f"current_possition: {self._attr_current_cover_position}, "
                     + f"current_tilt_position: {self._attr_current_cover_tilt_position}, "
                     + f"state: {self.state}]")

    # -------------------------------------------------------------------------
    # Travel timer helpers
    # -------------------------------------------------------------------------

    def _cancel_travel_task(self) -> None:
        """Cancel any running travel timer without updating position."""
        if self._travel_task and not self._travel_task.done():
            self._travel_task.cancel()
        self._travel_task = None

    def _calc_intermediate_position(self) -> int | None:
        """Calculate current position based on elapsed travel time."""
        if (self._travel_start_time is None
                or self._travel_direction is None
                or self._travel_start_position is None):
            return None

        elapsed = time_module.monotonic() - self._travel_start_time

        if self._travel_direction == "up" and self._time_opens:
            traveled = int((elapsed / self._time_opens) * 100)
            return min(self._travel_start_position + traveled, 100)
        elif self._travel_direction == "down" and self._time_closes:
            traveled = int((elapsed / self._time_closes) * 100)
            return max(self._travel_start_position - traveled, 0)

        return None

    async def _async_finish_travel(self, travel_time: float, target_position: int) -> None:
        """Wait for travel_time seconds then set final position.
        
        Fires after HA sends a command when no confirmation telegram is
        expected from the FSB14 (RS485 bus, HA-initiated commands).
        Physical button presses trigger value_changed() which cancels this task.
        """
        try:
            await asyncio.sleep(travel_time)
        except asyncio.CancelledError:
            return

        self._attr_current_cover_position = target_position
        self._attr_is_closed = (target_position == 0)
        self._attr_is_opening = False
        self._attr_is_closing = False
        self._travel_task = None
        self.schedule_update_ha_state()
        LOGGER.debug(f"[cover {self.dev_id}] travel timer finished: position={target_position}")

    def _start_travel(self, direction: str, start_position: int, target_position: int, travel_time: float) -> None:
        """Start movement tracking: cancel any previous task and launch a new one."""
        self._cancel_travel_task()
        self._travel_start_time = time_module.monotonic()
        self._travel_direction = direction
        self._travel_start_position = start_position
        self._travel_target_position = target_position
        self._travel_task = self.hass.async_create_task(
            self._async_finish_travel(travel_time, target_position)
        )
        LOGGER.debug(
            f"[cover {self.dev_id}] travel started: direction={direction}, "
            f"from={start_position} to={target_position}, time={travel_time}s"
        )

    # -------------------------------------------------------------------------
    # Cover commands
    # -------------------------------------------------------------------------

    def open_cover(self, **kwargs: Any) -> None:
        """Open the cover (sends bus message)."""
        if self._time_opens is not None:
            time = self._time_opens + 1
        else:
            time = 255
        
        address, _ = self._sender_id
        
        if self._sender_eep == H5_3F_7F:
            msg = H5_3F_7F(time, 0x01, 1).encode_message(address)
            self.send_message(msg)
        else:
            LOGGER.warn("[%s %s] Sender EEP %s not supported.", Platform.COVER, str(self.dev_id), self._sender_eep.eep_string)
            return
        
        if self.general_settings[CONF_FAST_STATUS_CHANGE]:
            self._attr_is_opening = True
            self._attr_is_closing = False
            self.schedule_update_ha_state()

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover and start time-based position tracking."""
        self.open_cover(**kwargs)

        if self._time_opens is not None:
            start_pos = self._attr_current_cover_position if self._attr_current_cover_position is not None else 0
            self._start_travel(
                direction="up",
                start_position=start_pos,
                target_position=100,
                travel_time=self._time_opens,
            )

    def close_cover(self, **kwargs: Any) -> None:
        """Close cover (sends bus message)."""
        if self._time_closes is not None:
            time = self._time_closes + 1
        else:
            time = 255
        
        address, _ = self._sender_id
        
        if self._sender_eep == H5_3F_7F:
            msg = H5_3F_7F(time, 0x02, 1).encode_message(address)
            self.send_message(msg)
        else:
            LOGGER.warn("[%s %s] Sender EEP %s not supported.", Platform.COVER, str(self.dev_id), self._sender_eep.eep_string)
            return
        
        if self.general_settings[CONF_FAST_STATUS_CHANGE]:
            self._attr_is_closing = True
            self._attr_is_opening = False
            self.schedule_update_ha_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover and start time-based position tracking."""
        self.close_cover(**kwargs)

        if self._time_closes is not None:
            start_pos = self._attr_current_cover_position if self._attr_current_cover_position is not None else 100
            self._start_travel(
                direction="down",
                start_position=start_pos,
                target_position=0,
                travel_time=self._time_closes,
            )

    def set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position (sends bus message)."""
        if self._time_closes is None or self._time_opens is None:
            return
        
        address, _ = self._sender_id
        position = kwargs[ATTR_POSITION]
        
        if position == self._attr_current_cover_position:
            return
        elif position == 100:
            direction = "up"
            time = self._time_opens + 1
        elif position == 0:
            direction = "down"
            time = self._time_closes + 1
        elif position > self._attr_current_cover_position:
            direction = "up"
            time = max(1, min(int(((position - self._attr_current_cover_position) / 100.0) * self._time_opens), 255))
        elif position < self._attr_current_cover_position:
            direction = "down"
            time = max(1, min(int(((self._attr_current_cover_position - position) / 100.0) * self._time_closes), 255))

        if self._sender_eep == H5_3F_7F:
            if direction == "up":
                command = 0x01
            elif direction == "down":
                command = 0x02
            
            msg = H5_3F_7F(time, command, 1).encode_message(address)
            self.send_message(msg)
        else:
            LOGGER.warn("[%s %s] Sender EEP %s not supported.", Platform.COVER, str(self.dev_id), self._sender_eep.eep_string)
            return
        
        if self.general_settings[CONF_FAST_STATUS_CHANGE]:
            if direction == "up":
                self._attr_is_opening = True
                self._attr_is_closing = False
            elif direction == "down":
                self._attr_is_closing = True
                self._attr_is_opening = False
            self.schedule_update_ha_state()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move to position and start time-based tracking."""
        if self._time_closes is None or self._time_opens is None:
            return

        position = kwargs[ATTR_POSITION]
        if position == self._attr_current_cover_position:
            return

        current = self._attr_current_cover_position if self._attr_current_cover_position is not None else 50

        if position >= 100:
            direction = "up"
            travel_time = float(self._time_opens)
        elif position <= 0:
            direction = "down"
            travel_time = float(self._time_closes)
        elif position > current:
            direction = "up"
            travel_time = max(1.0, ((position - current) / 100.0) * self._time_opens)
        else:
            direction = "down"
            travel_time = max(1.0, ((current - position) / 100.0) * self._time_closes)

        self.set_cover_position(**kwargs)  # sends the bus message

        self._start_travel(
            direction=direction,
            start_position=current,
            target_position=position,
            travel_time=travel_time,
        )

    def stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover (sends bus message)."""
        address, _ = self._sender_id

        if self._sender_eep == H5_3F_7F:
            msg = H5_3F_7F(0, 0x00, 1).encode_message(address)
            self.send_message(msg)
        
        if self.general_settings[CONF_FAST_STATUS_CHANGE]:
            self._attr_is_closing = False
            self._attr_is_opening = False
            self.schedule_update_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover and calculate intermediate position from elapsed time."""
        intermediate = self._calc_intermediate_position()

        self._cancel_travel_task()
        self.stop_cover(**kwargs)  # sends the bus message

        if intermediate is not None:
            self._attr_current_cover_position = intermediate
            self._attr_is_closed = (intermediate == 0)
            self._attr_is_opening = False
            self._attr_is_closing = False
            self.schedule_update_ha_state()
            LOGGER.debug(f"[cover {self.dev_id}] stop_cover: intermediate position={intermediate}")

    # -------------------------------------------------------------------------
    # Incoming telegram from FSB14 (physical button press or end-stop reached)
    # -------------------------------------------------------------------------

    def value_changed(self, msg):
        """Update the internal state of the cover.
        
        Only cancels the travel timer when the FSB14 reports a confirmed
        final position (0x50 closed, 0x70 open) or an intermediate position
        via runtime telegram (physically operated).
        state=0x00 is a mere ACK from the bus and must NOT cancel the timer.
        """
        try:
            decoded = self.dev_eep.decode_message(msg)
        except Exception as e:
            LOGGER.warning("Could not decode message: %s", str(e))
            return
        
        if self.dev_eep in [G5_3F_7F]:
            LOGGER.debug(f"[cover {self.dev_id}] G5_3F_7F - {decoded.__dict__}")

            if decoded.state == 0x02: # down — motor started, HA-initiated ACK, keep timer
                self._attr_is_closing = True
                self._attr_is_opening = False
                self._attr_is_closed = False
            elif decoded.state == 0x50: # closed — confirmed by hardware, cancel timer
                self._cancel_travel_task()
                self._attr_is_opening = False
                self._attr_is_closing = False
                self._attr_is_closed = True
                self._attr_current_cover_position = 0
                self._attr_current_cover_tilt_position = 0
            elif decoded.state == 0x01: # up — motor started, HA-initiated ACK, keep timer
                self._attr_is_opening = True
                self._attr_is_closing = False
                self._attr_is_closed = False
            elif decoded.state == 0x70: # open — confirmed by hardware, cancel timer
                self._cancel_travel_task()
                self._attr_is_opening = False
                self._attr_is_closing = False
                self._attr_is_closed = False
                self._attr_current_cover_position = 100
                self._attr_current_cover_tilt_position = 100

            elif decoded.time is not None and decoded.direction is not None and self._time_closes is not None and self._time_opens is not None:
                # Intermediate position from physical button press — cancel timer, use real value
                self._cancel_travel_task()

                time_in_seconds = decoded.time / 10.0

                if decoded.direction == 0x01:  # up
                    if self._attr_current_cover_position is None:
                        self._attr_current_cover_position = 0
                    
                    self._attr_current_cover_position = min(self._attr_current_cover_position + int(time_in_seconds / self._time_opens * 100.0), 100)
                    if self._time_tilts is not None:
                        self._attr_current_cover_tilt_position = min(self._attr_current_cover_tilt_position + int(decoded.time / self._time_tilts * 100.0), 100)

                else:  # down
                    if self._attr_current_cover_position is None:
                        self._attr_current_cover_position = 100
                    
                    self._attr_current_cover_position = max(self._attr_current_cover_position - int(time_in_seconds / self._time_closes * 100.0), 0)
                    if self._time_tilts is not None:
                        self._attr_current_cover_tilt_position = max(self._attr_current_cover_tilt_position - int(decoded.time / self._time_tilts * 100.0), 0)

                if self._attr_current_cover_position == 0:
                    self._attr_is_closed = True
                    self._attr_is_opening = False
                    self._attr_is_closing = False
                else:
                    self._attr_is_closed = False
                    self._attr_is_opening = False
                    self._attr_is_closing = False

            LOGGER.debug(f"[cover {self.dev_id}] state: {self.state}, opening: {self.is_opening}, closing: {self.is_closing}, closed: {self.is_closed}, position: {self._attr_current_cover_position}")

            self.schedule_update_ha_state()


    def set_cover_tilt_position(self, **kwargs: Any) -> None:
        address, _ = self._sender_id
        tilt_position = kwargs[ATTR_TILT_POSITION]
        
        if tilt_position == self._attr_current_cover_tilt_position:
            return
        elif tilt_position > self._attr_current_cover_tilt_position:
            direction = "up"
            sleeptime = min((((tilt_position - self._attr_current_cover_tilt_position) / 100.0 * self._time_tilts / 10.0) ), 255.0)
        elif tilt_position < self._attr_current_cover_tilt_position:
            direction = "down"
            sleeptime = min((((self._attr_current_cover_tilt_position - tilt_position) / 100.0 * self._time_tilts / 10.0) ), 255.0)

        if self._sender_eep == H5_3F_7F:
            if direction == "up":
                command = 0x01
            elif direction == "down":
                command = 0x02
            
            msg = H5_3F_7F(0, command, 1).encode_message(address)
            self.send_message(msg)
            time_module.sleep(sleeptime)
            
            msg = H5_3F_7F(0, 0x00, 1).encode_message(address)
            self.send_message(msg)

        if self.general_settings[CONF_FAST_STATUS_CHANGE]:
            if direction == "up":
                self._attr_is_opening = True
                self._attr_is_closing = False
            elif direction == "down":
                self._attr_is_closing = True
                self._attr_is_opening = False