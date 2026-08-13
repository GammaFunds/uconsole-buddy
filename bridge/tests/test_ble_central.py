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
