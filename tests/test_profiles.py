from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from wambridge.discovery import DiscoveredSpeaker
from wambridge.profiles import (
    DeviceProfile,
    ProfileStore,
    remember_device,
    resolve_device,
)
from wambridge.samsung import WamApiError, WamIdentity


class DeviceProfileTests(TestCase):
    def test_store_round_trip_is_case_insensitive(self) -> None:
        with TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "devices.json")
            profile = DeviceProfile(
                alias="M5",
                device_id="A1B2C3D4E5F6",
                name="[Samsung] M5",
                last_ip="10.0.0.118",
            )

            store.put(profile)

            self.assertEqual(store.get("m5"), profile)
            self.assertEqual(store.all(), [profile])

    def test_remember_reads_stable_device_id(self) -> None:
        with TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "devices.json")

            def identify_func(ip: str, **_kwargs) -> WamIdentity:
                self.assertEqual(ip, "10.0.0.118")
                return WamIdentity(device_id="a1:b2:c3:d4:e5:f6", name="[Samsung] M5")

            profile = remember_device(
                "Living room",
                "10.0.0.118",
                store=store,
                identify_func=identify_func,
            )

            self.assertEqual(profile.device_id, "A1B2C3D4E5F6")
            self.assertEqual(store.get("living ROOM").last_ip, "10.0.0.118")

    def test_resolve_updates_changed_ip_by_device_id(self) -> None:
        with TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "devices.json")
            store.put(
                DeviceProfile(
                    alias="M5",
                    device_id="A1B2C3D4E5F6",
                    name="[Samsung] M5",
                    last_ip="10.0.0.118",
                )
            )

            def identify_func(ip: str, **_kwargs) -> WamIdentity:
                if ip == "10.0.0.118":
                    raise WamApiError("old lease")
                if ip == "10.0.0.141":
                    return WamIdentity(
                        device_id="A1:B2:C3:D4:E5:F6",
                        name="[Samsung] M5",
                    )
                return WamIdentity(device_id="000000000000", name="Other")

            def discover_func(**_kwargs) -> list[DiscoveredSpeaker]:
                return [
                    DiscoveredSpeaker(ip="10.0.0.90", source="ssdp"),
                    DiscoveredSpeaker(ip="10.0.0.141", source="ssdp"),
                ]

            resolved = resolve_device(
                "M5",
                store=store,
                identify_func=identify_func,
                discover_func=discover_func,
            )

            self.assertEqual(resolved.last_ip, "10.0.0.141")
            self.assertEqual(store.get("M5").last_ip, "10.0.0.141")
