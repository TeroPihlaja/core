"""Configure iCloud tests."""

from collections.abc import Generator
from datetime import UTC, datetime
from unittest.mock import patch

from pyicloud.services.photos import AlbumContainer, PhotoAlbumFolder, PhotoAsset
import pytest

from homeassistant.components.icloud.const import DOMAIN

from .const import DEVICE, USER_INFO

from tests.common import AsyncMock, MockConfigEntry
from tests.typing import MagicMock


@pytest.fixture(autouse=True)
def icloud_not_create_dir():
    """Mock component setup."""
    with patch(
        "homeassistant.components.icloud.config_flow.os.path.exists", return_value=True
    ):
        yield


@pytest.fixture(name="icloud_client")
def mock_icloud_client() -> Generator[AsyncMock]:
    """Mock iCloud client."""
    with (
        patch(
            "homeassistant.components.icloud.account.IcloudAccount", autospec=True
        ) as mock_client,
        patch(
            "homeassistant.components.icloud.IcloudAccount",
            new=mock_client,
        ),
    ):
        client = mock_client.return_value
        client.api = MagicMock()
        client.photo_cache = None

        albums = [
            MagicMock(
                spec=PhotoAlbumFolder, id="folder_id1", title="My Folder 1", albums=[]
            ),
            MagicMock(
                id="album_id1",
                title="All Photos",
                photos=[
                    MagicMock(
                        spec=PhotoAsset,
                        id="photo_id1",
                        filename="My Photo 1.JPG",
                        item_type="image",
                        versions={
                            "original": MagicMock(
                                size=123456,
                                width=4000,
                                height=3000,
                            )
                        },
                    ),
                    MagicMock(
                        spec=PhotoAsset,
                        id="photo_id2",
                        filename="My Photo 2.heic",
                        item_type="image",
                    ),
                    MagicMock(
                        spec=PhotoAsset,
                        id="photo_id3",
                        filename="My Photo 3.png",
                        item_type="image",
                    ),
                ],
            ),
            MagicMock(
                id="album_id2",
                title="My Photos",
                photos=[
                    MagicMock(
                        spec=PhotoAsset,
                        id="photo_id2",
                        filename="My Photo 2.heic",
                        item_type="image",
                    ),
                ],
            ),
        ]

        shared = [
            MagicMock(
                id="stream_id1",
                title="Favorites",
                photos=[
                    MagicMock(
                        spec=PhotoAsset,
                        id="shared_id1",
                        filename="My Photo 1.jpg",
                        item_type="image",
                    ),
                    MagicMock(
                        spec=PhotoAsset,
                        id="shared_id2",
                        filename="My Video 1.mp4",
                        item_type="movie",
                    ),
                ],
            ),
            MagicMock(
                id="stream_id2",
                title="Random Stream",
                photos=[
                    MagicMock(
                        spec=PhotoAsset,
                        id="shared_id3",
                        filename="My Unknown file.xyz",
                        item_type="unknown",
                    ),
                ],
            ),
        ]

        client.api.photos.albums = AlbumContainer(albums)
        client.api.photos.shared_streams = AlbumContainer(shared)
        yield client


@pytest.fixture(name="config_entry")
def mock_config_entry() -> MockConfigEntry:
    """Create a mock config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="test_account_id",
        title="Test iCloud Account",
        data={
            "username": "test_user",
            "password": "test_pass",
            "with_family": False,
            "max_interval": 0,
            "gps_accuracy_threshold": 0,
        },
    )


class MockAppleDevice:
    """An Apple device whose reported status can change between fetches."""

    def __init__(self, status: dict) -> None:
        """Store the status this device reports."""
        self._status = status

    def status(self, fields) -> dict:
        """Return the current status."""
        return self._status

    def __getitem__(self, key):
        """Proxy into the raw status."""
        return self._status.get(key)


class MockDevices:
    """The devices of an account."""

    def __init__(self, user_info: dict, devices: list[MockAppleDevice]) -> None:
        """Store the account's devices."""
        self.user_info = user_info
        self._devices = devices

    def __iter__(self):
        """Iterate over the devices."""
        return iter(self._devices)

    def __len__(self) -> int:
        """Return the number of devices."""
        return len(self._devices)

    def __getitem__(self, index):
        """Return the status of a device."""
        return self._devices[index].status(None)

    def refresh(self, locate: bool = True) -> None:
        """Match the FindMyiPhone service interface."""


@pytest.fixture(name="locating_service")
def mock_locating_service() -> Generator[tuple[MagicMock, dict]]:
    """Mock an account with one device that reports a location."""
    status = {
        **DEVICE,
        "location": {
            "latitude": 1.0,
            "longitude": 2.0,
            "horizontalAccuracy": 10,
            "isOld": False,
            "timeStamp": datetime.now(tz=UTC).timestamp() * 1000,
        },
    }
    with patch(
        "homeassistant.components.icloud.account.PyiCloudService"
    ) as service_mock:
        service = service_mock.return_value
        service.requires_2fa = False
        service.devices = MockDevices(USER_INFO, [MockAppleDevice(status)])
        yield service, status
