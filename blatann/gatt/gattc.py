from __future__ import annotations
import logging
from typing import List, Optional, Iterable, Tuple

from blatann import gatt
from blatann.gatt.gattc_attribute import GattcAttribute
from blatann.gatt.managers import _ReadWriteManager
from blatann.uuids import Uuid, DeclarationUuid, DescriptorUuid
from blatann.event_type import EventSource, Event
from blatann.gatt.reader import GattcReader
from blatann.gatt.writer import GattcWriter
from blatann.nrf import nrf_types, nrf_events
from blatann.waitables.event_waitable import EventWaitable, IdBasedEventWaitable
from blatann.exceptions import InvalidOperationException
from blatann.event_args import *


logger = logging.getLogger(__name__)


class GattcCharacteristic(gatt.Characteristic):
    def __init__(self, ble_device, peer, uuid: Uuid,
                 properties: gatt.CharacteristicProperties,
                 decl_attr: GattcAttribute,
                 value_attr: GattcAttribute,
                 cccd_attr: GattcAttribute = None,
                 attributes: List[GattcAttribute] = None):
        super(GattcCharacteristic, self).__init__(ble_device, peer, uuid, properties)
        self._decl_attr = decl_attr
        self._value_attr = value_attr
        self._cccd_attr = cccd_attr
        self._on_notification_event = EventSource("On Notification", logger)
        self._attributes = tuple(sorted(attributes, key=lambda d: d.handle)) or ()
        self.peer = peer

        self._on_read_complete_event = EventSource("On Read Complete", logger)
        self._on_write_complete_event = EventSource("Write Complete", logger)
        self._on_cccd_write_complete_event = EventSource("CCCD Write Complete", logger)

        self._value_attr.on_read_complete.register(self._read_complete)
        self._value_attr.on_write_complete.register(self._write_complete)
        if self._cccd_attr:
            self._cccd_attr.on_write_complete.register(self._cccd_write_complete)

        self.peer.driver_event_subscribe(self._on_indication_notification, nrf_events.GattcEvtHvx)

    """
    Properties
    """

    @property
    def declaration_attribute(self) -> GattcAttribute:
        return self._decl_attr

    @property
    def value_attribute(self) -> GattcAttribute:
        return self._value_attr

    @property
    def value(self) -> bytes:
        """
        The current value of the characteristic

        :return: The last known value of the characteristic
        """
        return self._value_attr.value

    @property
    def readable(self) -> bool:
        """
        Gets if the characteristic can be read from
        """
        return self._properties.read

    @property
    def writable(self) -> bool:
        """
        Gets if the characteristic can be written to
        """
        return self._properties.write

    @property
    def writable_without_response(self) -> bool:
        """
        Gets if the characteristic accepts write commands that don't require a confirmation response
        """
        return self._properties.write_no_response

    @property
    def subscribable(self) -> bool:
        """
        Gets if the characteristic can be subscribed to
        """
        return self._properties.notify or self._properties.indicate

    @property
    def subscribed(self) -> bool:
        """
        Gets if the characteristic is currently subscribed to
        """
        return self.cccd_state != gatt.SubscriptionState.NOT_SUBSCRIBED

    @property
    def attributes(self) -> Tuple[GattcAttribute]:
        """
        Returns the list of all attributes that reside in the characteristic.
        This includes the declaration attribute, value attribute, and descriptors (CCCD, Name, etc.)
        """
        return self._attributes

    @property
    def string_encoding(self):
        """
        The default method for encoding strings into bytes when a string is provided as a value
        """
        return self._value_attr.string_encoding

    @string_encoding.setter
    def string_encoding(self, encoding):
        """
        Sets how the characteristic value is encoded when provided a string

        :param encoding: the encoding to use (utf8, ascii, etc.)
        """
        self._value_attr.string_encoding = encoding

    """
    Events
    """

    @property
    def on_read_complete(self) -> Event[GattcCharacteristic, ReadCompleteEventArgs]:
        return self._on_read_complete_event

    @property
    def on_write_complete(self) -> Event[GattcCharacteristic, WriteCompleteEventArgs]:
        return self._on_write_complete_event

    @property
    def on_notification_received(self) -> Event[GattcCharacteristic, NotificationReceivedEventArgs]:
        return self._on_notification_event

    """
    Public Methods
    """

    def subscribe(self, on_notification_handler: Callable[[GattcCharacteristic, NotificationReceivedEventArgs], None],
                  prefer_indications=False) -> EventWaitable[GattcCharacteristic, SubscriptionWriteCompleteEventArgs]:
        """
        Subscribes to the characteristic's indications or notifications, depending on what's available and the
        prefer_indications setting. Returns a Waitable that executes when the subscription on the peripheral finishes.

        The Waitable returns two parameters: (GattcCharacteristic this, SubscriptionWriteCompleteEventArgs event args)

        :param on_notification_handler: The handler to be called when an indication or notification is received from
            the peripheral. Must take two parameters: (GattcCharacteristic this, NotificationReceivedEventArgs event args)
        :param prefer_indications: If the peripheral supports both indications and notifications,
            will subscribe to indications instead of notifications
        :return: A Waitable that will fire when the subscription finishes
        :raises: InvalidOperationException if the characteristic cannot be subscribed to
            (characteristic does not support indications or notifications)
        """
        if not self.subscribable:
            raise InvalidOperationException("Cannot subscribe to Characteristic {}".format(self.uuid))
        if prefer_indications and self._properties.indicate or not self._properties.notify:
            value = gatt.SubscriptionState.INDICATION
        else:
            value = gatt.SubscriptionState.NOTIFY
        self._on_notification_event.register(on_notification_handler)
        waitable = self._cccd_attr.write(gatt.SubscriptionState.to_buffer(value))
        return IdBasedEventWaitable(self._on_cccd_write_complete_event, waitable.id)

    def unsubscribe(self) -> EventWaitable[GattcCharacteristic, SubscriptionWriteCompleteEventArgs]:
        """
        Unsubscribes from indications and notifications from the characteristic and clears out all handlers
        for the characteristic's on_notification event handler. Returns a Waitable that executes when the unsubscription
        finishes.

        The Waitable returns two parameters: (GattcCharacteristic this, SubscriptionWriteCompleteEventArgs event args)

        :return: A Waitable that will fire when the unsubscription finishes
        """
        if not self.subscribable:
            raise InvalidOperationException("Cannot subscribe to Characteristic {}".format(self.uuid))
        value = gatt.SubscriptionState.NOT_SUBSCRIBED
        waitable = self._cccd_attr.write(gatt.SubscriptionState.to_buffer(value))
        self._on_notification_event.clear_handlers()

        return IdBasedEventWaitable(self._on_cccd_write_complete_event, waitable.id)

    def read(self) -> EventWaitable[GattcCharacteristic, ReadCompleteEventArgs]:
        """
        Initiates a read of the characteristic and returns a Waitable that executes when the read finishes with
        the data read.

        The Waitable returns two parameters: (GattcCharacteristic this, ReadCompleteEventArgs event args)

        :return: A waitable that will fire when the read finishes
        :raises: InvalidOperationException if characteristic not readable
        """
        if not self.readable:
            raise InvalidOperationException("Characteristic {} is not readable".format(self.uuid))
        waitable = self._value_attr.read()
        return IdBasedEventWaitable(self._on_read_complete_event, waitable.id)

    def write(self, data) -> EventWaitable[GattcCharacteristic, WriteCompleteEventArgs]:
        """
        Initiates a write of the data provided to the characteristic and returns a Waitable that executes
        when the write completes and the confirmation response is received from the other device.

        The Waitable returns two parameters: (GattcCharacteristic this, WriteCompleteEventArgs event args)

        :param data: The data to write. Can be a string, bytes, or anything that can be converted to bytes
        :type data: str or bytes or bytearray
        :return: A waitable that returns when the write finishes
        :raises: InvalidOperationException if characteristic is not writable
        """
        if not self.writable:
            raise InvalidOperationException("Characteristic {} is not writable".format(self.uuid))
        if isinstance(data, str):
            data = data.encode(self.string_encoding)
        waitable = self._value_attr.write(bytes(data), True)
        return IdBasedEventWaitable(self._on_write_complete_event, waitable.id)

    def write_without_response(self, data) -> EventWaitable[GattcCharacteristic, WriteCompleteEventArgs]:
        """
        Peforms a Write command, which does not require the peripheral to send a confirmation response packet.
        This is a faster but lossy operation, in the case that the packet is dropped/never received by the other device.
        This returns a waitable that executes when the write is transmitted to the peripheral device.

        .. note:: Data sent without responses must fit within a single MTU minus 3 bytes for the operation overhead.

        :param data: The data to write. Can be a string, bytes, or anything that can be converted to bytes
        :type data: str or bytes or bytearray
        :return: A waitable that returns when the write finishes
        :raises: InvalidOperationException if characteristic is not writable without responses
        """
        if not self.writable_without_response:
            raise InvalidOperationException("Characteristic {} does not accept "
                                            "writes without responses".format(self.uuid))
        if isinstance(data, str):
            data = data.encode(self.string_encoding)
        waitable = self._value_attr.write(bytes(data), False)
        return IdBasedEventWaitable(self._on_write_complete_event, waitable.id)

    def find_descriptor(self, uuid: Uuid) -> Optional[GattcAttribute]:
        """
        Searches for the descriptor matching the UUID provided and returns the attribute. If not found, returns None

        :param uuid: The UUID to search for
        :return: THe descriptor attribute, if found
        """
        for attr in self._attributes:
            if attr.uuid == uuid:
                return attr

    """
    Event Handlers
    """

    def _read_complete(self, sender: GattcAttribute, event_args: ReadCompleteEventArgs):
        """
        Handler for GattcAttribute.on_read_complete.
        Dispatches the on_read_complete event and updates the internal value if read was successful
        """
        self._on_read_complete_event.notify(self, event_args)

    def _write_complete(self, sender: GattcAttribute, event_args: WriteCompleteEventArgs):
        """
        Handler for value_attribute.on_write_complete. Dispatches on_write_complete.
        """
        self._on_write_complete_event.notify(self, event_args)

    def _cccd_write_complete(self, sender: GattcAttribute, event_args: WriteCompleteEventArgs):
        """
        Handler for cccd_attribute.on_write_complete. Dispatches on_cccd_write_complete.
        """
        if event_args.status == nrf_types.BLEGattStatusCode.success:
            self.cccd_state = gatt.SubscriptionState.from_buffer(bytearray(event_args.value))
        args = SubscriptionWriteCompleteEventArgs(event_args.id, self.cccd_state,
                                                  event_args.status, event_args.reason)
        self._on_cccd_write_complete_event.notify(self, args)

    def _on_indication_notification(self, driver, event):
        """
        Handler for GattcEvtHvx. Dispatches the on_notification_event to listeners

        :type event: nrf_events.GattcEvtHvx
        """
        if (event.conn_handle != self.peer.conn_handle or
                event.attr_handle != self._value_attr.handle):
            return

        is_indication = False
        if event.hvx_type == nrf_events.BLEGattHVXType.indication:
            is_indication = True
            self.ble_device.ble_driver.ble_gattc_hv_confirm(event.conn_handle, event.attr_handle)

        # Update the value attribute with the data that was provided
        self._value_attr.update(bytearray(event.data))
        self._on_notification_event.notify(self, NotificationReceivedEventArgs(self.value, is_indication))

    """
    Factory methods
    """

    @classmethod
    def from_discovered_characteristic(cls, ble_device, peer, read_write_manager, nrf_characteristic):
        """
        Internal factory method used to create a new characteristic from a discovered nRF Characteristic

        :type ble_device: blatann.BleDevice
        :type peer: blatann.peer.Peer
        :type read_write_manager: _ReadWriteManager
        :type nrf_characteristic: nrf_types.BLEGattCharacteristic
        """
        char_decl_uuid = DeclarationUuid.characteristic
        char_uuid = ble_device.uuid_manager.nrf_uuid_to_uuid(nrf_characteristic.uuid)
        cccd_uuid = DescriptorUuid.cccd

        properties = gatt.CharacteristicProperties.from_nrf_properties(nrf_characteristic.char_props)

        decl_attr = None
        value_attr = None
        cccd_attr = None
        attributes = []
        for nrf_desc in nrf_characteristic.descs:
            d_uuid = ble_device.uuid_manager.nrf_uuid_to_uuid(nrf_desc.uuid)
            d_attr = GattcAttribute(d_uuid, nrf_desc.handle, read_write_manager)

            if d_uuid == char_decl_uuid:
                decl_attr = d_attr
            elif d_uuid == char_uuid:
                value_attr = d_attr
            elif d_uuid == cccd_uuid:
                cccd_attr = d_attr
            attributes.append(d_attr)

        if not decl_attr:
            logger.debug(f"Failed to find declaration attribute within list "
                         f"(char uuid: {char_uuid}), creating new...")
            decl_attr = GattcAttribute(char_decl_uuid, nrf_characteristic.handle_decl,
                                       read_write_manager, nrf_characteristic.data_decl)
            attributes.append(decl_attr)
        if not value_attr:
            logger.debug(f"Failed to find value attribute within list "
                         f"(char uuid: {char_uuid}), creating new...")
            value_attr = GattcAttribute(char_uuid, nrf_characteristic.handle_value,
                                        read_write_manager, nrf_characteristic.data_value)
            attributes.append(value_attr)

        return GattcCharacteristic(ble_device, peer, char_uuid, properties,
                                   decl_attr, value_attr, cccd_attr, attributes)


class GattcService(gatt.Service):
    @property
    def characteristics(self) -> List[GattcCharacteristic]:
        """
        Gets the list of characteristics within the service

        :rtype: list of GattcCharacteristic
        """
        return self._characteristics

    def find_characteristic(self, characteristic_uuid: Uuid) -> Optional[GattcCharacteristic]:
        """
        Finds the characteristic matching the given UUID inside the service. If not found, returns None

        :param characteristic_uuid: The UUID of the characteristic to find
        :return: The characteristic if found, otherwise None
        """
        for c in self.characteristics:
            if c.uuid == characteristic_uuid:
                return c

    @classmethod
    def from_discovered_service(cls, ble_device, peer, read_write_manager, nrf_service):
        """
        Internal factory method used to create a new service from a discovered nRF Service.
        Also takes care of creating and adding all characteristics within the service

        :type ble_device: blatann.device.BleDevice
        :type peer: blatann.peer.Peer
        :type read_write_manager: _ReadWriteManager
        :type nrf_service: nrf_types.BLEGattService
        """
        service_uuid = ble_device.uuid_manager.nrf_uuid_to_uuid(nrf_service.uuid)
        service = GattcService(ble_device, peer, service_uuid, gatt.ServiceType.PRIMARY,
                               nrf_service.start_handle, nrf_service.end_handle)
        for c in nrf_service.chars:
            char = GattcCharacteristic.from_discovered_characteristic(ble_device, peer, read_write_manager, c)
            service.characteristics.append(char)
        return service


class GattcDatabase(gatt.GattDatabase):
    """
    Represents a remote GATT Database which lives on a connected peripheral. Contains all discovered services,
    characteristics, and descriptors
    """
    def __init__(self, ble_device, peer):
        super(GattcDatabase, self).__init__(ble_device, peer)
        self._writer = GattcWriter(ble_device, peer)
        self._reader = GattcReader(ble_device, peer)
        self._read_write_manager = _ReadWriteManager(self._reader, self._writer)

    @property
    def services(self) -> List[GattcService]:
        """
        :rtype: list of GattcService
        """
        return self._services

    def find_service(self, service_uuid: Uuid) -> Optional[GattcService]:
        """
        Finds the characteristic matching the given UUID inside the database. If not found, returns None

        :param service_uuid: The UUID of the service to find
        :type service_uuid: blatann.uuid.Uuid
        :return: The service if found, otherwise None
        :rtype: GattcService
        """
        for s in self.services:
            if s.uuid == service_uuid:
                return s

    def find_characteristic(self, characteristic_uuid):
        """
        Finds the characteristic matching the given UUID inside the database. If not found, returns None

        :param characteristic_uuid: The UUID of the characteristic to find
        :type characteristic_uuid: blatann.uuid.Uuid
        :return: The characteristic if found, otherwise None
        :rtype: GattcCharacteristic
        """
        for c in self.iter_characteristics():
            if c.uuid == characteristic_uuid:
                return c

    def iter_characteristics(self) -> Iterable[GattcCharacteristic]:
        """
        Iterates through all the characteristics in the database

        :return: An iterable of the characterisitcs in the database
        :rtype: collections.Iterable[GattcCharacteristic]
        """
        for s in self.services:
            for c in s.characteristics:
                yield c

    def add_discovered_services(self, nrf_services):
        """
        Adds the discovered NRF services from the service_discovery module.
        Used for internal purposes primarily.

        :param nrf_services: The discovered services with all the characteristics and descriptors
        :type nrf_services: list of nrf_types.BLEGattService
        """
        for service in nrf_services:
            self.services.append(GattcService.from_discovered_service(self.ble_device, self.peer,
                                                                      self._read_write_manager, service))
