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


class CleanupService:
    def __init__(self, events, error=None):
        self.events = events
        self.error = error

    async def unregister(self):
        self.events.append("service.unregister")
        if self.error is not None:
            raise self.error


class CleanupManager:
    def __init__(self, events, error=None):
        self.events = events
        self.error = error
        self.paths = []

    async def call_unregister_advertisement(self, path):
        self.events.append("advertisement.unregister")
        self.paths.append(path)
        if self.error is not None:
            raise self.error


class CleanupProxy:
    def __init__(self, manager):
        self.path = "/org/bluez/hci0"
        self.manager = manager

    def get_interface(self, name):
        assert name == ble_nus.Advertisement._MANAGER_INTERFACE
        return self.manager


class CleanupAdapter:
    def __init__(self, manager):
        self._proxy = CleanupProxy(manager)

    async def get_address(self):
        return "2C:CF:67:E5:21:9B"


class CleanupBus:
    def __init__(self, events, error=None):
        self.events = events
        self.error = error

    def disconnect(self):
        self.events.append("bus.disconnect")
        if self.error is not None:
            raise self.error


def prepared_peripheral(*, advertisement_error=None, service_error=None,
                        bus_error=None, bus_owned=True):
    events = []
    manager = CleanupManager(events, advertisement_error)
    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)
    peripheral._bus = CleanupBus(events, bus_error)
    peripheral._bus_owned = bus_owned
    peripheral._adapter = CleanupAdapter(manager)
    peripheral._svc = CleanupService(events, service_error)
    peripheral._advertisement_registered = True
    peripheral._advertisement_path = ble_nus.ADVERT_PATH
    peripheral._service_registered = True
    return peripheral, events, manager


class FakeOwnedTask:
    def __init__(self, events, *, error=None, cancelled=False):
        self.events = events
        self.error = error
        self._cancelled = cancelled
        self._done = error is not None or cancelled

    def done(self):
        return self._done

    def cancelled(self):
        return self._cancelled

    def cancel(self):
        self.events.append("task.cancel")
        self._cancelled = True
        self._done = True
        return True

    def __await__(self):
        async def observe():
            self.events.append("task.await")
            if self._cancelled:
                raise asyncio.CancelledError
            if self.error is not None:
                raise self.error

        return observe().__await__()


class CallbackObjectManager:
    def __init__(self, events, objects=None):
        self.events = events
        self.objects = objects or {}
        self.interfaces_added = None

    def on_interfaces_added(self, callback):
        self.events.append("object.on")
        self.interfaces_added = callback

    def off_interfaces_added(self, callback):
        assert callback is self.interfaces_added
        self.events.append("object.off")
        self.interfaces_added = None

    async def call_get_managed_objects(self):
        return self.objects


class CallbackProperties:
    def __init__(self, events):
        self.events = events
        self.properties_changed = None

    def on_properties_changed(self, callback):
        self.events.append("properties.on")
        self.properties_changed = callback

    def off_properties_changed(self, callback):
        assert callback is self.properties_changed
        self.events.append("properties.off")
        self.properties_changed = None


class CallbackDeviceProxy:
    def __init__(self, properties):
        self.properties = properties

    def get_interface(self, name):
        assert name == "org.freedesktop.DBus.Properties"
        return self.properties


class CallbackRootProxy:
    def __init__(self, manager):
        self.manager = manager

    def get_interface(self, name):
        assert name == "org.freedesktop.DBus.ObjectManager"
        return self.manager


class CallbackBus:
    def __init__(self, manager, device, properties):
        self.manager = manager
        self.device = device
        self.properties = properties

    async def introspect(self, service, path):
        assert service == "org.bluez"
        return path

    def get_proxy_object(self, service, path, introspection):
        assert service == "org.bluez"
        if path == "/":
            return CallbackRootProxy(self.manager)
        return CallbackDeviceProxy(self.properties)


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


def test_aclose_before_start_is_harmless_and_closed():
    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)

    run(peripheral.aclose())

    assert peripheral._closed is True
    assert peripheral._bus is None


def test_aclose_unregisters_advertisement_then_service_then_owned_bus():
    peripheral, events, manager = prepared_peripheral()

    run(peripheral.aclose())

    assert events == [
        "advertisement.unregister",
        "service.unregister",
        "bus.disconnect",
    ]
    assert manager.paths == [ble_nus.ADVERT_PATH]
    assert peripheral._advertisement_registered is False
    assert peripheral._service_registered is False
    assert peripheral._bus_owned is False
    assert peripheral._closed is True


def test_aclose_second_call_performs_no_cleanup_again():
    peripheral, events, _ = prepared_peripheral()

    run(peripheral.aclose())
    run(peripheral.aclose())

    assert events == [
        "advertisement.unregister",
        "service.unregister",
        "bus.disconnect",
    ]


def test_aclose_continues_after_advertisement_failure_and_retains_marker():
    peripheral, events, manager = prepared_peripheral(
        advertisement_error=RuntimeError("advertisement failure"),
    )

    with pytest.raises(ExceptionGroup) as raised:
        run(peripheral.aclose())

    assert events == [
        "advertisement.unregister",
        "service.unregister",
        "bus.disconnect",
    ]
    assert manager.paths == [ble_nus.ADVERT_PATH]
    assert [str(error) for error in raised.value.exceptions] == [
        "advertisement failure",
    ]
    assert peripheral._advertisement_registered is True
    assert peripheral._advertisement_path == ble_nus.ADVERT_PATH
    assert peripheral._service_registered is False
    assert peripheral._bus_owned is False
    assert peripheral._closed is False


def test_aclose_continues_after_service_failure_and_retains_marker():
    peripheral, events, _ = prepared_peripheral(
        service_error=RuntimeError("service failure"),
    )

    with pytest.raises(ExceptionGroup) as raised:
        run(peripheral.aclose())

    assert events == [
        "advertisement.unregister",
        "service.unregister",
        "bus.disconnect",
    ]
    assert [str(error) for error in raised.value.exceptions] == [
        "service failure",
    ]
    assert peripheral._advertisement_registered is False
    assert peripheral._service_registered is True
    assert peripheral._bus_owned is False
    assert peripheral._closed is False


def test_aclose_does_not_disconnect_caller_owned_bus():
    peripheral, events, _ = prepared_peripheral(bus_owned=False)

    run(peripheral.aclose())

    assert events == [
        "advertisement.unregister",
        "service.unregister",
    ]
    assert peripheral._closed is True


def test_start_marks_owned_bus_service_and_advertisement(monkeypatch):
    events = []

    class StartBus:
        pass

    class StartAdapter:
        class Proxy:
            path = "/org/bluez/hci0"

        _proxy = Proxy()

        async def get_address(self):
            return "2C:CF:67:E5:21:9B"

    class StartService:
        async def register(self, bus, *, adapter):
            events.append("service.register")

    class StartAdvertisement:
        _MANAGER_INTERFACE = "org.bluez.LEAdvertisingManager1"

        def __init__(self, *args):
            pass

        async def register(self, bus, *, adapter, path):
            events.append(("advertisement.register", path))

    async def get_bus():
        events.append("bus.create")
        return StartBus()

    async def select_adapter(bus, addr):
        return StartAdapter()

    async def watch_connections(self):
        return None

    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)
    peripheral._svc = StartService()
    monkeypatch.setattr(ble_nus, "get_message_bus", get_bus)
    monkeypatch.setattr(peripheral, "_select_adapter", select_adapter)
    monkeypatch.setattr(ble_nus, "Advertisement", StartAdvertisement)
    monkeypatch.setattr(peripheral, "_watch_connections", watch_connections.__get__(peripheral))

    run(peripheral.start())

    assert events == [
        "bus.create",
        "service.register",
        ("advertisement.register", ble_nus.ADVERT_PATH),
    ]
    assert peripheral._bus_owned is True
    assert peripheral._service_registered is True
    assert peripheral._advertisement_registered is True
    assert peripheral._advertisement_path == ble_nus.ADVERT_PATH


def test_aclose_aggregates_all_cleanup_failures_in_operation_order():
    class AdvertisementCleanupFailure(RuntimeError):
        pass

    class ServiceCleanupFailure(RuntimeError):
        pass

    class BusDisconnectFailure(RuntimeError):
        pass

    advertisement_failure = AdvertisementCleanupFailure(
        "advertisement cleanup failure"
    )
    service_failure = ServiceCleanupFailure("service cleanup failure")
    bus_failure = BusDisconnectFailure("bus disconnect failure")
    peripheral, events, manager = prepared_peripheral(
        advertisement_error=advertisement_failure,
        service_error=service_failure,
        bus_error=bus_failure,
    )
    retained_path = peripheral._advertisement_path

    with pytest.raises(ExceptionGroup) as raised:
        run(peripheral.aclose())

    assert events == [
        "advertisement.unregister",
        "service.unregister",
        "bus.disconnect",
    ]
    assert manager.paths == [retained_path]
    failures = raised.value.exceptions
    assert len(failures) == 3
    assert failures[0] is advertisement_failure
    assert failures[1] is service_failure
    assert failures[2] is bus_failure
    assert [type(error) for error in failures] == [
        AdvertisementCleanupFailure,
        ServiceCleanupFailure,
        BusDisconnectFailure,
    ]
    assert [str(error) for error in failures] == [
        "advertisement cleanup failure",
        "service cleanup failure",
        "bus disconnect failure",
    ]
    assert peripheral._advertisement_registered is True
    assert peripheral._advertisement_path == retained_path
    assert peripheral._service_registered is True
    assert peripheral._bus_owned is True
    assert peripheral._closed is False


def test_aclose_retains_owned_bus_after_disconnect_failure():
    bus_failure = RuntimeError("bus disconnect failure")
    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)
    events = []
    bus = CleanupBus(events, bus_failure)
    peripheral._bus = bus
    peripheral._bus_owned = True

    with pytest.raises(ExceptionGroup) as raised:
        run(peripheral.aclose())

    assert events == ["bus.disconnect"]
    assert len(raised.value.exceptions) == 1
    assert raised.value.exceptions[0] is bus_failure
    assert type(raised.value.exceptions[0]) is RuntimeError
    assert str(raised.value.exceptions[0]) == "bus disconnect failure"
    assert peripheral._bus is bus
    assert peripheral._bus_owned is True
    assert peripheral._closed is False


def test_owned_task_is_registered_and_cancelled_then_awaited():
    events = []
    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)
    task = FakeOwnedTask(events)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            ble_nus.asyncio,
            "create_task",
            lambda coroutine: (coroutine.close(), task)[1],
        )
        owned = peripheral._create_owned_task(asyncio.sleep(0))

    assert owned is task
    run(peripheral.aclose())

    assert events == ["task.cancel", "task.await"]
    assert peripheral._owned_tasks == []


def test_completed_successful_owned_task_is_observed_without_cancel():
    events = []
    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)
    task = FakeOwnedTask(events)
    task._done = True

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            ble_nus.asyncio,
            "create_task",
            lambda coroutine: (coroutine.close(), task)[1],
        )
        peripheral._create_owned_task(asyncio.sleep(0))

    run(peripheral.aclose())

    assert events == ["task.await"]
    assert peripheral._owned_tasks == []


def test_already_cancelled_owned_task_is_harmless():
    events = []
    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)
    task = FakeOwnedTask(events, cancelled=True)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            ble_nus.asyncio,
            "create_task",
            lambda coroutine: (coroutine.close(), task)[1],
        )
        peripheral._create_owned_task(asyncio.sleep(0))

    run(peripheral.aclose())

    assert events == ["task.await"]
    assert peripheral._owned_tasks == []


def test_completed_owned_task_failure_is_reported_and_removed():
    events = []
    failure = RuntimeError("owned task failure")
    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)
    task = FakeOwnedTask(events, error=failure)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            ble_nus.asyncio,
            "create_task",
            lambda coroutine: (coroutine.close(), task)[1],
        )
        peripheral._create_owned_task(asyncio.sleep(0))

    with pytest.raises(ExceptionGroup) as raised:
        run(peripheral.aclose())

    assert events == ["task.await"]
    assert raised.value.exceptions == (failure,)
    assert peripheral._owned_tasks == []


def test_foreign_task_is_not_cancelled_or_awaited():
    events = []
    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)

    async def wait_forever():
        await asyncio.sleep(60)

    async def exercise():
        foreign_task = asyncio.create_task(wait_forever())
        await peripheral.aclose()
        assert foreign_task.cancelled() is False
        foreign_task.cancel()
        try:
            await foreign_task
        except asyncio.CancelledError:
            pass

    run(exercise())
    assert events == []


def test_multiple_owned_task_failures_follow_registration_order():
    events = []
    first = RuntimeError("first task failure")
    second = RuntimeError("second task failure")
    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)
    tasks = [FakeOwnedTask(events, error=first), FakeOwnedTask(events, error=second)]

    with pytest.MonkeyPatch.context() as monkeypatch:
        def fake_create_task(coroutine):
            coroutine.close()
            return tasks.pop(0)

        monkeypatch.setattr(ble_nus.asyncio, "create_task", fake_create_task)
        peripheral._create_owned_task(asyncio.sleep(0))
        peripheral._create_owned_task(asyncio.sleep(0))

    with pytest.raises(ExceptionGroup) as raised:
        run(peripheral.aclose())

    assert raised.value.exceptions == (first, second)


def test_watch_connections_records_object_and_property_callback_removers():
    events = []
    manager = CallbackObjectManager(
        events,
        {
            "/org/bluez/hci0/dev_AA": {
                "org.bluez.Device1": {
                    "Connected": types.SimpleNamespace(value=False),
                },
            },
        },
    )
    properties = CallbackProperties(events)
    bus = CallbackBus(manager, "/org/bluez/hci0/dev_AA", properties)
    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)
    peripheral._bus = bus
    peripheral._adapter = CleanupAdapter(CleanupManager(events))

    run(peripheral._watch_connections())

    assert events == ["object.on", "properties.on"]
    assert len(peripheral._callback_removers) == 2


def test_callback_removal_follows_task_shutdown_and_precedes_advertisement():
    peripheral, events, manager = prepared_peripheral()
    peripheral._owned_tasks = [FakeOwnedTask(events)]
    peripheral._callback_removers = [
        ("callback", lambda: events.append("callback.off")),
    ]
    run(peripheral.aclose())

    assert events == [
        "task.cancel",
        "task.await",
        "callback.off",
        "advertisement.unregister",
        "service.unregister",
        "bus.disconnect",
    ]
    assert peripheral._callback_removers == []


def test_callback_removal_failure_does_not_stop_registration_cleanup():
    peripheral, events, manager = prepared_peripheral()
    peripheral._callback_removers = [
        ("callback", lambda: (_ for _ in ()).throw(RuntimeError("callback failure"))),
    ]
    with pytest.raises(ExceptionGroup) as raised:
        run(peripheral.aclose())

    assert events == [
        "advertisement.unregister",
        "service.unregister",
        "bus.disconnect",
    ]
    assert str(raised.value.exceptions[0]) == "callback failure"
    assert len(peripheral._callback_removers) == 1


def test_start_rolls_back_bus_when_service_registration_fails(monkeypatch):
    events = []
    startup_failure = RuntimeError("service startup failure")
    bus = CleanupBus(events)
    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)

    class FailingService:
        async def register(self, bus, *, adapter):
            raise startup_failure

    async def get_bus():
        return bus

    async def select_adapter(bus, addr):
        return CleanupAdapter(CleanupManager(events))

    peripheral._svc = FailingService()
    monkeypatch.setattr(ble_nus, "get_message_bus", get_bus)
    monkeypatch.setattr(peripheral, "_select_adapter", select_adapter)

    with pytest.raises(RuntimeError) as raised:
        run(peripheral.start())

    assert raised.value is startup_failure
    assert events == ["bus.disconnect"]
    assert peripheral._bus_owned is False


def test_start_rolls_back_service_and_bus_when_advertisement_fails(monkeypatch):
    events = []
    startup_failure = RuntimeError("advertisement startup failure")
    bus = CleanupBus(events)
    manager = CleanupManager(events)
    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)

    class Service:
        async def register(self, bus, *, adapter):
            events.append("service.register")

        async def unregister(self):
            events.append("service.unregister")

    class FailingAdvertisement:
        _MANAGER_INTERFACE = "org.bluez.LEAdvertisingManager1"

        def __init__(self, *args):
            pass

        async def register(self, bus, *, adapter, path):
            raise startup_failure

    async def get_bus():
        return bus

    async def select_adapter(bus, addr):
        return CleanupAdapter(manager)

    peripheral._svc = Service()
    monkeypatch.setattr(ble_nus, "get_message_bus", get_bus)
    monkeypatch.setattr(peripheral, "_select_adapter", select_adapter)
    monkeypatch.setattr(ble_nus, "Advertisement", FailingAdvertisement)

    with pytest.raises(RuntimeError) as raised:
        run(peripheral.start())

    assert raised.value is startup_failure
    assert events == ["service.register", "service.unregister", "bus.disconnect"]


def test_start_preserves_startup_exception_when_rollback_also_fails(monkeypatch):
    events = []
    startup_failure = RuntimeError("primary startup failure")
    rollback_failure = RuntimeError("rollback advertisement failure")
    bus = CleanupBus(events)
    manager = CleanupManager(events, rollback_failure)
    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)

    class Service:
        async def register(self, bus, *, adapter):
            pass

        async def unregister(self):
            events.append("service.unregister")

    class Advertisement:
        _MANAGER_INTERFACE = "org.bluez.LEAdvertisingManager1"

        def __init__(self, *args):
            pass

        async def register(self, bus, *, adapter, path):
            pass

    async def get_bus():
        return bus

    async def select_adapter(bus, addr):
        return CleanupAdapter(manager)

    async def failing_watch(self):
        raise startup_failure

    peripheral._svc = Service()
    monkeypatch.setattr(ble_nus, "get_message_bus", get_bus)
    monkeypatch.setattr(peripheral, "_select_adapter", select_adapter)
    monkeypatch.setattr(ble_nus, "Advertisement", Advertisement)
    monkeypatch.setattr(peripheral, "_watch_connections", failing_watch.__get__(peripheral))

    with pytest.raises(RuntimeError) as raised:
        run(peripheral.start())

    assert raised.value is startup_failure
    assert raised.value.__notes__
    assert "rollback advertisement failure" in raised.value.__notes__[0]
    assert peripheral._advertisement_registered is True
    assert peripheral._service_registered is False
    assert peripheral._bus_owned is False


def test_start_rolls_back_owned_task_and_callback_before_registrations(monkeypatch):
    events = []
    startup_failure = RuntimeError("later startup failure")
    bus = CleanupBus(events)
    manager = CleanupManager(events)
    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)

    class Service:
        async def register(self, bus, *, adapter):
            pass

        async def unregister(self):
            events.append("service.unregister")

    class Advertisement:
        _MANAGER_INTERFACE = "org.bluez.LEAdvertisingManager1"

        def __init__(self, *args):
            pass

        async def register(self, bus, *, adapter, path):
            pass

    async def get_bus():
        return bus

    async def select_adapter(bus, addr):
        return CleanupAdapter(manager)

    async def failing_watch(self):
        self._callback_removers.append(
            ("callback", lambda: events.append("callback.off"))
        )
        self._create_owned_task(asyncio.sleep(60))
        raise startup_failure

    peripheral._svc = Service()
    monkeypatch.setattr(ble_nus, "get_message_bus", get_bus)
    monkeypatch.setattr(peripheral, "_select_adapter", select_adapter)
    monkeypatch.setattr(ble_nus, "Advertisement", Advertisement)
    monkeypatch.setattr(peripheral, "_watch_connections", failing_watch.__get__(peripheral))

    with pytest.raises(RuntimeError) as raised:
        run(peripheral.start())

    assert raised.value is startup_failure
    assert events == [
        "callback.off",
        "advertisement.unregister",
        "service.unregister",
        "bus.disconnect",
    ]
    assert peripheral._owned_tasks == []
    assert peripheral._callback_removers == []


def test_aclose_after_failed_rollback_retries_only_remaining_ownership(monkeypatch):
    events = []
    startup_failure = RuntimeError("startup failure")
    rollback_failure = RuntimeError("advertisement remains")
    bus = CleanupBus(events)
    manager = CleanupManager(events, rollback_failure)
    peripheral = ble_nus.NusPeripheral("Gerald", lambda line: None)

    class Service:
        async def register(self, bus, *, adapter):
            pass

        async def unregister(self):
            events.append("service.unregister")

    class Advertisement:
        _MANAGER_INTERFACE = "org.bluez.LEAdvertisingManager1"

        def __init__(self, *args):
            pass

        async def register(self, bus, *, adapter, path):
            pass

    async def get_bus():
        return bus

    async def select_adapter(bus, addr):
        return CleanupAdapter(manager)

    async def failing_watch(self):
        raise startup_failure

    peripheral._svc = Service()
    monkeypatch.setattr(ble_nus, "get_message_bus", get_bus)
    monkeypatch.setattr(peripheral, "_select_adapter", select_adapter)
    monkeypatch.setattr(ble_nus, "Advertisement", Advertisement)
    monkeypatch.setattr(peripheral, "_watch_connections", failing_watch.__get__(peripheral))

    with pytest.raises(RuntimeError) as raised:
        run(peripheral.start())

    assert raised.value is startup_failure
    assert peripheral._advertisement_registered is True
    assert peripheral._service_registered is False
    assert peripheral._bus_owned is False

    manager.error = None
    run(peripheral.aclose())

    assert events == [
        "advertisement.unregister",
        "service.unregister",
        "bus.disconnect",
        "advertisement.unregister",
    ]
    assert peripheral._closed is True


def test_aclose_aggregates_task_callback_and_registration_failures_by_phase():
    class TaskCleanupFailure(RuntimeError):
        pass

    class CallbackCleanupFailure(RuntimeError):
        pass

    class AdvertisementCleanupFailure(RuntimeError):
        pass

    class ServiceCleanupFailure(RuntimeError):
        pass

    class BusDisconnectFailure(RuntimeError):
        pass

    task_failure = TaskCleanupFailure("task cleanup failure")
    callback_failure = CallbackCleanupFailure("callback cleanup failure")
    advertisement_failure = AdvertisementCleanupFailure(
        "advertisement cleanup failure"
    )
    service_failure = ServiceCleanupFailure("service cleanup failure")
    bus_failure = BusDisconnectFailure("bus disconnect failure")
    peripheral, events, manager = prepared_peripheral(
        advertisement_error=advertisement_failure,
        service_error=service_failure,
        bus_error=bus_failure,
    )
    retained_path = peripheral._advertisement_path

    def remove_callback():
        events.append("callback")
        raise callback_failure

    peripheral._callback_removers = [("callback", remove_callback)]

    async def fail_owned_task():
        raise task_failure

    async def exercise():
        task = peripheral._create_owned_task(fail_owned_task())
        await asyncio.sleep(0)
        assert task.done() is True

        with pytest.raises(ExceptionGroup) as raised:
            await peripheral.aclose()

        failures = raised.value.exceptions
        assert [type(failure) for failure in failures] == [
            TaskCleanupFailure,
            CallbackCleanupFailure,
            AdvertisementCleanupFailure,
            ServiceCleanupFailure,
            BusDisconnectFailure,
        ]
        assert [str(failure) for failure in failures] == [
            "task cleanup failure",
            "callback cleanup failure",
            "advertisement cleanup failure",
            "service cleanup failure",
            "bus disconnect failure",
        ]
        assert events == [
            "callback",
            "advertisement.unregister",
            "service.unregister",
            "bus.disconnect",
        ]
        assert manager.paths == [retained_path]
        assert peripheral._owned_tasks == []
        assert len(peripheral._callback_removers) == 1
        assert peripheral._advertisement_registered is True
        assert peripheral._advertisement_path == retained_path
        assert peripheral._service_registered is True
        assert peripheral._bus_owned is True
        assert peripheral._closed is False

    run(exercise())
