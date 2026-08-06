import asyncio
import importlib
import sys
import types

import pytest


def _install_bluez_peripheral_fakes():
    package = types.ModuleType("bluez_peripheral")
    package.__path__ = []

    service_module = types.ModuleType("bluez_peripheral.gatt.service")

    class FakeService:
        def __init__(self, *args, **kwargs):
            pass

    service_module.Service = FakeService

    characteristic_module = types.ModuleType("bluez_peripheral.gatt.characteristic")

    class FakeCharacteristic:
        def __call__(self, function):
            return self

        def setter(self, function):
            return self

        def changed(self, value):
            pass

    def fake_characteristic(*args, **kwargs):
        return lambda function: FakeCharacteristic()

    class FakeFlags:
        NOTIFY = 1
        WRITE = 2
        WRITE_WITHOUT_RESPONSE = 4

    characteristic_module.characteristic = fake_characteristic
    characteristic_module.CharacteristicFlags = FakeFlags

    advert_module = types.ModuleType("bluez_peripheral.advert")

    class FakeAdvertisement:
        _MANAGER_INTERFACE = "org.bluez.LEAdvertisingManager1"

        def __init__(self, *args, **kwargs):
            pass

    advert_module.Advertisement = FakeAdvertisement

    util_module = types.ModuleType("bluez_peripheral.util")

    class PlaceholderAdapter:
        pass

    util_module.Adapter = PlaceholderAdapter
    util_module.get_message_bus = lambda: None

    gatt_package = types.ModuleType("bluez_peripheral.gatt")
    gatt_package.__path__ = []
    sys.modules.update({
        "bluez_peripheral": package,
        "bluez_peripheral.gatt": gatt_package,
        "bluez_peripheral.gatt.service": service_module,
        "bluez_peripheral.gatt.characteristic": characteristic_module,
        "bluez_peripheral.advert": advert_module,
        "bluez_peripheral.util": util_module,
    })


try:
    importlib.import_module("bluez_peripheral")
except ModuleNotFoundError:
    _install_bluez_peripheral_fakes()

ble_nus = importlib.import_module("companion.ble_nus")


ADAPTER_OBJECTS = {
    "/org/bluez/hci0": {
        "org.bluez.Adapter1": {"Address": "2C:CF:67:E5:21:9B"},
    },
    "/org/bluez/hci1": {
        "org.bluez.Adapter1": {"Address": "38:7A:CC:84:A4:14"},
    },
    "/org/bluez/test": {
        "org.bluez.SimAccessTest1": {"Address": "AA:BB:CC:DD:EE:FF"},
    },
}


class FakeProxy:
    def __init__(self, path, interfaces):
        self.path = path
        self.address = interfaces.get("org.bluez.Adapter1", {}).get("Address")


class FakeObjectManager:
    def __init__(self, objects):
        self.objects = objects

    async def call_get_managed_objects(self):
        return self.objects


class FakeRootProxy:
    def __init__(self, object_manager):
        self.object_manager = object_manager

    def get_interface(self, name):
        assert name == "org.freedesktop.DBus.ObjectManager"
        return self.object_manager


class FakeBus:
    def __init__(self, objects):
        self.objects = objects
        self.introspected_paths = []
        self.object_manager = FakeObjectManager(objects)

    async def introspect(self, service, path):
        assert service == "org.bluez"
        self.introspected_paths.append(path)
        return f"introspection:{path}"

    def get_proxy_object(self, service, path, introspection):
        assert service == "org.bluez"
        assert introspection == f"introspection:{path}"
        if path == "/":
            return FakeRootProxy(self.object_manager)
        return FakeProxy(path, self.objects[path])


class FakeAdapter:
    def __init__(self, proxy):
        self._proxy = proxy

    async def get_address(self):
        return self._proxy.address


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def adapter_fakes(monkeypatch):
    monkeypatch.setattr(ble_nus, "Adapter", FakeAdapter)


def test_non_adapter_managed_object_is_ignored(adapter_fakes):
    bus = FakeBus(ADAPTER_OBJECTS)

    adapters = run(ble_nus.NusPeripheral._enumerate_adapters(bus))

    assert [adapter._proxy.path for adapter in adapters] == [
        "/org/bluez/hci0",
        "/org/bluez/hci1",
    ]
    assert "/org/bluez/test" not in bus.introspected_paths


def test_real_adapter1_objects_are_enumerated(adapter_fakes):
    bus = FakeBus(ADAPTER_OBJECTS)

    adapters = run(ble_nus.NusPeripheral._enumerate_adapters(bus))

    assert [run(adapter.get_address()) for adapter in adapters] == [
        "2C:CF:67:E5:21:9B",
        "38:7A:CC:84:A4:14",
    ]


def test_exact_requested_address_selects_correct_adapter(adapter_fakes):
    bus = FakeBus(ADAPTER_OBJECTS)

    selected = run(ble_nus.NusPeripheral._select_adapter(bus, "38:7A:CC:84:A4:14"))

    assert selected._proxy.path == "/org/bluez/hci1"


def test_address_matching_is_case_insensitive(adapter_fakes):
    bus = FakeBus(ADAPTER_OBJECTS)

    selected = run(ble_nus.NusPeripheral._select_adapter(bus, "38:7a:cc:84:a4:14"))

    assert selected._proxy.path == "/org/bluez/hci1"


def test_missing_address_lists_only_real_adapter_addresses(adapter_fakes):
    bus = FakeBus(ADAPTER_OBJECTS)

    with pytest.raises(RuntimeError) as error:
        run(ble_nus.NusPeripheral._select_adapter(bus, "00:00:00:00:00:00"))

    message = str(error.value)
    assert "2C:CF:67:E5:21:9B" in message
    assert "38:7A:CC:84:A4:14" in message
    assert "AA:BB:CC:DD:EE:FF" not in message


def test_environment_address_overrides_legacy_default(monkeypatch):
    monkeypatch.setenv("GERALD_BT_ADAPTER_ADDR", "38:7A:CC:84:A4:14")

    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)

    assert peripheral.adapter_addr == "38:7A:CC:84:A4:14"


def test_without_environment_address_legacy_default_remains(monkeypatch):
    monkeypatch.delenv("GERALD_BT_ADAPTER_ADDR", raising=False)

    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)

    assert peripheral.adapter_addr == ble_nus.ADAPTER_ADDR
