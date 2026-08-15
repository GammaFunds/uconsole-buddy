import asyncio

from bridge.ble_central import BleCentral, NUS_RX


def test_send_line_serializes_concurrent_logical_messages():
    class FakeClient:
        mtu_size = 6  # 3 payload bytes per BLE write

        def __init__(self):
            self.writes = []

        async def write_gatt_char(self, characteristic, data, response=False):
            assert characteristic == NUS_RX
            assert response is False
            self.writes.append(bytes(data))

            # Give a competing send_line() task a chance to run between
            # chunks. Without message-level serialization this exposes the
            # framing race deterministically.
            await asyncio.sleep(0)

    async def scenario():
        central = BleCentral(lambda _line: None)
        client = FakeClient()

        central._client = client
        central._connected = True

        results = await asyncio.gather(
            central.send_line("AAAAAA"),
            central.send_line("BBBBBB"),
        )

        assert results == [True, True]

        a = [b"AAA", b"AAA"]
        b = [b"BBB", b"BBB"]

        assert client.writes in (a + b, b + a), (
            "chunks from concurrent logical messages interleaved: "
            f"{client.writes!r}"
        )

    asyncio.run(scenario())


def test_connect_accepts_name_fallback_when_bluez_service_uuids_are_stale(
    monkeypatch,
):
    """A paired BlueZ cache must not hide a live Gerald NUS advertisement.

    Linux/BlueZ can expose stale Device1.UUIDs from the persisted paired
    service cache even while the current over-the-air advertisement contains
    NUS. In that case the configured Gerald name is the discovery fallback;
    the subsequent GATT/NUS setup still has to succeed before the central is
    considered connected.
    """
    from types import SimpleNamespace

    import bridge.ble_central as ble_module

    device = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="Claude-uConsole",
    )

    stale_advertisement = SimpleNamespace(
        local_name="Claude-uConsole",
        service_uuids=[
            "00001800-0000-1000-8000-00805f9b34fb",
            "00001801-0000-1000-8000-00805f9b34fb",
        ],
    )

    observations = {
        "predicate_match": None,
        "client_connected": False,
        "notify_started": False,
    }

    async def fake_find_device_by_filter(
        cls,
        filterfunc,
        timeout=10.0,
        **kwargs,
    ):
        assert timeout == 15.0

        matched = filterfunc(
            device,
            stale_advertisement,
        )
        observations["predicate_match"] = matched

        return device if matched else None

    class FakeClient:
        def __init__(
            self,
            selected_device,
            disconnected_callback=None,
        ):
            assert selected_device is device
            self.disconnected_callback = disconnected_callback

        async def connect(self):
            observations["client_connected"] = True

        async def start_notify(
            self,
            characteristic,
            callback,
        ):
            assert characteristic == ble_module.NUS_TX
            assert callable(callback)
            observations["notify_started"] = True

        async def disconnect(self):
            return None

    monkeypatch.setattr(
        ble_module.BleakScanner,
        "find_device_by_filter",
        classmethod(fake_find_device_by_filter),
    )
    monkeypatch.setattr(
        ble_module,
        "BleakClient",
        FakeClient,
    )

    async def scenario():
        central = ble_module.BleCentral(
            lambda _line: None,
            device_name="Claude-uConsole",
        )

        await central.connect()

        assert observations["predicate_match"] is True
        assert observations["client_connected"] is True
        assert observations["notify_started"] is True
        assert central._connected is True

    asyncio.run(scenario())


def test_connect_cleans_up_name_fallback_client_when_nus_notify_setup_fails(
    monkeypatch,
):
    """A name-fallback candidate is not accepted until NUS notify setup works."""
    from types import SimpleNamespace

    import bridge.ble_central as ble_module

    device = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="Claude-uConsole",
    )

    stale_advertisement = SimpleNamespace(
        local_name="Claude-uConsole",
        service_uuids=[
            "00001800-0000-1000-8000-00805f9b34fb",
            "00001801-0000-1000-8000-00805f9b34fb",
        ],
    )

    observations = {
        "scan_calls": 0,
        "client_connected": False,
        "notify_attempted": False,
        "disconnect_called": False,
    }

    async def fake_find_device_by_filter(
        cls,
        filterfunc,
        timeout=10.0,
        **kwargs,
    ):
        assert timeout == 15.0
        observations["scan_calls"] += 1

        matched = filterfunc(
            device,
            stale_advertisement,
        )
        return device if matched else None

    class FakeClient:
        def __init__(
            self,
            selected_device,
            disconnected_callback=None,
        ):
            assert selected_device is device
            self.disconnected_callback = disconnected_callback

        async def connect(self):
            observations["client_connected"] = True

        async def start_notify(
            self,
            characteristic,
            callback,
        ):
            assert characteristic == ble_module.NUS_TX
            assert callable(callback)
            observations["notify_attempted"] = True
            raise RuntimeError("simulated NUS notify setup failure")

        async def disconnect(self):
            observations["disconnect_called"] = True

    monkeypatch.setattr(
        ble_module.BleakScanner,
        "find_device_by_filter",
        classmethod(fake_find_device_by_filter),
    )
    monkeypatch.setattr(
        ble_module,
        "BleakClient",
        FakeClient,
    )

    async def scenario():
        central = ble_module.BleCentral(
            lambda _line: None,
            device_name="Claude-uConsole",
        )

        error = None
        try:
            await central.connect()
        except RuntimeError as exc:
            error = exc

        assert error is not None
        assert str(error) == "simulated NUS notify setup failure"

        # First scan rejects stale UUIDs; second scan accepts configured name.
        assert observations["scan_calls"] == 2
        assert observations["client_connected"] is True
        assert observations["notify_attempted"] is True

        # Failed GATT/NUS validation must leave no live/stale client behind.
        assert observations["disconnect_called"] is True
        assert central._client is None
        assert central._connected is False

    asyncio.run(scenario())


def test_connect_preserves_client_when_failed_nus_cleanup_disconnect_raises(
    monkeypatch,
):
    """An uncertain disconnect must not discard the owned client reference."""
    from types import SimpleNamespace

    import bridge.ble_central as ble_module

    device = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="Claude-uConsole",
    )
    stale_advertisement = SimpleNamespace(
        local_name="Claude-uConsole",
        service_uuids=[
            "00001800-0000-1000-8000-00805f9b34fb",
            "00001801-0000-1000-8000-00805f9b34fb",
        ],
    )

    observations = {
        "scan_calls": 0,
        "disconnect_calls": 0,
        "disconnect_raises": True,
    }
    holder = {}

    async def fake_find_device_by_filter(
        cls,
        filterfunc,
        timeout=10.0,
        **kwargs,
    ):
        assert timeout == 15.0
        observations["scan_calls"] += 1

        # Only the first connect attempt reaches the fallback candidate.
        if observations["scan_calls"] > 2:
            return None

        matched = filterfunc(
            device,
            stale_advertisement,
        )
        return device if matched else None

    class FakeClient:
        def __init__(
            self,
            selected_device,
            disconnected_callback=None,
        ):
            assert selected_device is device
            self.disconnected_callback = disconnected_callback
            holder["client"] = self

        async def connect(self):
            return None

        async def start_notify(
            self,
            characteristic,
            callback,
        ):
            assert characteristic == ble_module.NUS_TX
            assert callable(callback)
            raise RuntimeError("simulated NUS notify setup failure")

        async def disconnect(self):
            observations["disconnect_calls"] += 1
            if observations["disconnect_raises"]:
                raise RuntimeError("simulated cleanup disconnect failure")

    monkeypatch.setattr(
        ble_module.BleakScanner,
        "find_device_by_filter",
        classmethod(fake_find_device_by_filter),
    )
    monkeypatch.setattr(
        ble_module,
        "BleakClient",
        FakeClient,
    )

    async def scenario():
        central = ble_module.BleCentral(
            lambda _line: None,
            device_name="Claude-uConsole",
        )

        first_error = None
        try:
            await central.connect()
        except RuntimeError as exc:
            first_error = exc

        # The original validation failure remains authoritative.
        assert first_error is not None
        assert str(first_error) == "simulated NUS notify setup failure"

        # Disconnect status is uncertain, so ownership must be retained.
        assert observations["disconnect_calls"] == 1
        assert central._client is holder["client"]
        assert central._connected is False

        # A later connect can retry cleanup rather than losing the handle.
        observations["disconnect_raises"] = False

        second_error = None
        try:
            await central.connect()
        except RuntimeError as exc:
            second_error = exc

        assert second_error is not None
        assert "kein Gerät mit NUS-Service" in str(second_error)
        assert observations["disconnect_calls"] == 2
        assert central._client is None
        assert central._connected is False

    asyncio.run(scenario())


def test_connect_cleanup_disconnect_does_not_trigger_reconnect(
    monkeypatch,
):
    """Intentional cleanup callbacks must not enter the reconnect lifecycle."""
    from types import SimpleNamespace

    import bridge.ble_central as ble_module

    device = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="Claude-uConsole",
    )
    stale_advertisement = SimpleNamespace(
        local_name="Claude-uConsole",
        service_uuids=[
            "00001800-0000-1000-8000-00805f9b34fb",
            "00001801-0000-1000-8000-00805f9b34fb",
        ],
    )

    observations = {
        "disconnect_called": False,
        "external_disconnect_events": 0,
    }
    holder = {}

    async def fake_find_device_by_filter(
        cls,
        filterfunc,
        timeout=10.0,
        **kwargs,
    ):
        assert timeout == 15.0
        matched = filterfunc(
            device,
            stale_advertisement,
        )
        return device if matched else None

    class FakeClient:
        def __init__(
            self,
            selected_device,
            disconnected_callback=None,
        ):
            assert selected_device is device
            self.disconnected_callback = disconnected_callback
            holder["client"] = self

        async def connect(self):
            return None

        async def start_notify(
            self,
            characteristic,
            callback,
        ):
            assert characteristic == ble_module.NUS_TX
            assert callable(callback)
            raise RuntimeError("simulated NUS notify setup failure")

        async def disconnect(self):
            observations["disconnect_called"] = True
            if self.disconnected_callback is not None:
                self.disconnected_callback(self)

    monkeypatch.setattr(
        ble_module.BleakScanner,
        "find_device_by_filter",
        classmethod(fake_find_device_by_filter),
    )
    monkeypatch.setattr(
        ble_module,
        "BleakClient",
        FakeClient,
    )

    def on_disconnect():
        observations["external_disconnect_events"] += 1

    async def scenario():
        central = ble_module.BleCentral(
            lambda _line: None,
            device_name="Claude-uConsole",
            on_disconnect=on_disconnect,
        )

        error = None
        try:
            await central.connect()
        except RuntimeError as exc:
            error = exc

        assert error is not None
        assert str(error) == "simulated NUS notify setup failure"
        assert observations["disconnect_called"] is True

        # Cleanup of an unvalidated candidate is intentional, not a lost
        # validated Gerald connection.
        assert observations["external_disconnect_events"] == 0
        assert central._reconnect_task is None
        assert central._client is None
        assert central._connected is False

        # A delayed callback from the discarded client must also be inert
        # after some replacement client has become current.
        replacement = object()
        central._client = replacement
        central._connected = True

        old_client = holder["client"]
        old_client.disconnected_callback(old_client)

        assert observations["external_disconnect_events"] == 0
        assert central._reconnect_task is None

    asyncio.run(scenario())


def test_connect_rejects_disconnect_during_successful_nus_validation(
    monkeypatch,
):
    """A disconnect during NUS validation must prevent a false connected state."""
    from types import SimpleNamespace

    import bridge.ble_central as ble_module

    device = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="Claude-uConsole",
    )
    advertisement = SimpleNamespace(
        local_name="Claude-uConsole",
        service_uuids=[ble_module.NUS_SERVICE],
    )

    observations = {
        "scan_calls": 0,
        "external_disconnect_events": 0,
    }
    holder = {}

    async def fake_find_device_by_filter(
        cls,
        filterfunc,
        timeout=10.0,
        **kwargs,
    ):
        assert timeout == 15.0
        observations["scan_calls"] += 1
        assert observations["scan_calls"] == 1
        assert filterfunc(device, advertisement) is True
        return device

    class FakeClient:
        def __init__(
            self,
            selected_device,
            disconnected_callback=None,
        ):
            assert selected_device is device
            self.disconnected_callback = disconnected_callback
            holder["client"] = self

        async def connect(self):
            return None

        async def start_notify(
            self,
            characteristic,
            callback,
        ):
            assert characteristic == ble_module.NUS_TX
            assert callable(callback)

            # The physical link disappears while validation is in progress,
            # but the backend operation itself still returns successfully.
            assert self.disconnected_callback is not None
            self.disconnected_callback(self)

        async def disconnect(self):
            raise AssertionError(
                "disconnect callback already established that the link is gone"
            )

    monkeypatch.setattr(
        ble_module.BleakScanner,
        "find_device_by_filter",
        classmethod(fake_find_device_by_filter),
    )
    monkeypatch.setattr(
        ble_module,
        "BleakClient",
        FakeClient,
    )

    def on_disconnect():
        observations["external_disconnect_events"] += 1

    async def scenario():
        central = ble_module.BleCentral(
            lambda _line: None,
            device_name="Claude-uConsole",
            on_disconnect=on_disconnect,
        )

        error = None
        try:
            await central.connect()
        except RuntimeError as exc:
            error = exc

        assert error is not None
        assert str(error) == "BLE disconnected during NUS setup"

        # The callback confirms that this candidate is already disconnected;
        # it must never be published as the live central.
        assert central._connected is False
        assert central._client is None

        # Caller-level initial/reconnect retry owns recovery. Do not create a
        # second reconnect lifecycle from inside the validation attempt.
        assert central._reconnect_task is None
        assert observations["external_disconnect_events"] == 0
        assert observations["scan_calls"] == 1

    asyncio.run(scenario())


def test_connect_uuid_match_does_not_enter_name_fallback(
    monkeypatch,
):
    """A direct NUS UUID match must finish discovery without name fallback."""
    from types import SimpleNamespace

    import bridge.ble_central as ble_module

    device = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="unrelated-device-name",
    )
    advertisement = SimpleNamespace(
        local_name="unrelated-device-name",
        service_uuids=[
            "00001800-0000-1000-8000-00805f9b34fb",
            ble_module.NUS_SERVICE.upper(),
        ],
    )

    observations = {
        "scan_calls": 0,
        "notify_started": False,
    }

    async def fake_find_device_by_filter(
        cls,
        filterfunc,
        timeout=10.0,
        **kwargs,
    ):
        assert timeout == 15.0
        observations["scan_calls"] += 1

        if observations["scan_calls"] != 1:
            raise AssertionError(
                "name fallback was entered after successful UUID discovery"
            )

        assert filterfunc(device, advertisement) is True
        return device

    class FakeClient:
        def __init__(
            self,
            selected_device,
            disconnected_callback=None,
        ):
            assert selected_device is device
            self.disconnected_callback = disconnected_callback

        async def connect(self):
            return None

        async def start_notify(
            self,
            characteristic,
            callback,
        ):
            assert characteristic == ble_module.NUS_TX
            assert callable(callback)
            observations["notify_started"] = True

        async def disconnect(self):
            return None

    monkeypatch.setattr(
        ble_module.BleakScanner,
        "find_device_by_filter",
        classmethod(fake_find_device_by_filter),
    )
    monkeypatch.setattr(
        ble_module,
        "BleakClient",
        FakeClient,
    )

    async def scenario():
        central = ble_module.BleCentral(
            lambda _line: None,
            device_name="Claude-uConsole",
        )

        await central.connect()

        assert observations["scan_calls"] == 1
        assert observations["notify_started"] is True
        assert central._connected is True

    asyncio.run(scenario())


def test_connect_rejects_disconnect_during_bleak_client_connect(
    monkeypatch,
):
    """A callback during BleakClient.connect must invalidate the attempt."""
    from types import SimpleNamespace

    import bridge.ble_central as ble_module

    device = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        name="Claude-uConsole",
    )
    advertisement = SimpleNamespace(
        local_name="Claude-uConsole",
        service_uuids=[ble_module.NUS_SERVICE],
    )

    observations = {
        "scan_calls": 0,
        "start_notify_called": False,
        "external_disconnect_events": 0,
    }

    async def fake_find_device_by_filter(
        cls,
        filterfunc,
        timeout=10.0,
        **kwargs,
    ):
        assert timeout == 15.0
        observations["scan_calls"] += 1
        assert observations["scan_calls"] == 1
        assert filterfunc(device, advertisement) is True
        return device

    class FakeClient:
        def __init__(
            self,
            selected_device,
            disconnected_callback=None,
        ):
            assert selected_device is device
            self.disconnected_callback = disconnected_callback

        async def connect(self):
            # Backend reports a disconnect while connect() itself is still
            # in flight, then returns normally.
            assert self.disconnected_callback is not None
            self.disconnected_callback(self)

        async def start_notify(
            self,
            characteristic,
            callback,
        ):
            observations["start_notify_called"] = True

        async def disconnect(self):
            raise AssertionError(
                "callback already established that this candidate disconnected"
            )

    monkeypatch.setattr(
        ble_module.BleakScanner,
        "find_device_by_filter",
        classmethod(fake_find_device_by_filter),
    )
    monkeypatch.setattr(
        ble_module,
        "BleakClient",
        FakeClient,
    )

    def on_disconnect():
        observations["external_disconnect_events"] += 1

    async def scenario():
        central = ble_module.BleCentral(
            lambda _line: None,
            device_name="Claude-uConsole",
            on_disconnect=on_disconnect,
        )

        error = None
        try:
            await central.connect()
        except RuntimeError as exc:
            error = exc

        assert error is not None
        assert str(error) == "BLE disconnected during client connect"

        # Once the connect-phase callback has established link loss, NUS
        # validation must not even begin.
        assert observations["start_notify_called"] is False

        assert central._client is None
        assert central._connected is False
        assert central._validating_client is None
        assert central._validation_disconnected is False

        # Existing caller-level retry owns recovery.
        assert central._reconnect_task is None
        assert observations["external_disconnect_events"] == 0
        assert observations["scan_calls"] == 1

    asyncio.run(scenario())
