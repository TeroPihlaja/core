"""Tests for the iCloud account."""

from datetime import timedelta
from unittest.mock import MagicMock, Mock, patch

from pyicloud.exceptions import PyiCloudAuthRequiredException, PyiCloudFailedLoginException
import pytest

from homeassistant.components.icloud.account import IcloudAccount, IcloudDevice
from homeassistant.components.icloud.const import (
    CONF_GPS_ACCURACY_THRESHOLD,
    CONF_MAX_INTERVAL,
    CONF_WITH_FAMILY,
    DOMAIN,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import utcnow

from .const import DEVICE, MOCK_CONFIG, USER_INFO, USERNAME

from tests.common import MockConfigEntry


@pytest.fixture(name="mock_store")
def mock_store_fixture():
    """Mock the storage."""
    with patch("homeassistant.components.icloud.account.Store") as store_mock:
        store_instance = Mock(spec=Store)
        store_instance.path = "/mock/path"
        store_mock.return_value = store_instance
        yield store_instance


@pytest.fixture(name="mock_icloud_service_no_userinfo")
def mock_icloud_service_no_userinfo_fixture():
    """Mock PyiCloudService with devices as dict but no userInfo."""
    with patch(
        "homeassistant.components.icloud.account.PyiCloudService"
    ) as service_mock:
        service_instance = MagicMock()
        service_instance.requires_2fa = False
        mock_device = MagicMock()
        mock_device.status = iter(DEVICE)
        mock_device.user_info = None
        service_instance.devices = mock_device
        service_mock.return_value = service_instance
        yield service_instance


async def test_setup_fails_when_userinfo_missing(
    hass: HomeAssistant,
    mock_store: Mock,
    mock_icloud_service_no_userinfo: MagicMock,
) -> None:
    """Test setup fails when userInfo is missing from devices dict."""

    assert mock_icloud_service_no_userinfo is not None

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CONFIG, entry_id="test", unique_id=USERNAME
    )
    config_entry.add_to_hass(hass)

    account = IcloudAccount(
        hass,
        MOCK_CONFIG[CONF_USERNAME],
        MOCK_CONFIG[CONF_PASSWORD],
        mock_store,
        MOCK_CONFIG[CONF_WITH_FAMILY],
        MOCK_CONFIG[CONF_MAX_INTERVAL],
        MOCK_CONFIG[CONF_GPS_ACCURACY_THRESHOLD],
        config_entry,
    )

    with pytest.raises(ConfigEntryNotReady, match="No user info found"):
        account.setup()


class MockAppleDevice:
    """Mock "Apple device" which implements the .status(...) method used by the account."""

    def __init__(self, status_dict) -> None:
        """Set status."""
        self._status = status_dict

    def status(self, key):
        """Return current status."""
        return self._status

    def __getitem__(self, key):
        """Allow indexing the device itself (device[KEY]) to proxy into the raw status dict."""
        return self._status.get(key)


class MockDevicesContainer:
    """Mock devices container which is iterable and indexable returning device status dicts."""

    def __init__(self, userinfo, devices) -> None:
        """Initialize with userinfo and list of device objects."""
        self.user_info = userinfo
        self._devices = devices

    def __iter__(self):
        """Iterate returns device objects (each must have .status(...))."""
        return iter(self._devices)

    def __len__(self):
        """Return number of devices."""
        return len(self._devices)

    def __getitem__(self, idx):
        """Indexing returns device object (which must have .status(...))."""
        dev = self._devices[idx]
        if hasattr(dev, "status"):
            return dev.status(None)
        return dev


@pytest.fixture(name="mock_icloud_service")
def mock_icloud_service_fixture():
    """Mock PyiCloudService with devices container that is iterable and indexable returning status dict."""
    with patch(
        "homeassistant.components.icloud.account.PyiCloudService",
    ) as service_mock:
        service_instance = MagicMock()
        device_obj = MockAppleDevice(DEVICE)
        devices_container = MockDevicesContainer(USER_INFO, [device_obj])

        service_instance.devices = devices_container
        service_instance.requires_2fa = False

        service_mock.return_value = service_instance
        yield service_instance


async def test_setup_success_with_devices(
    hass: HomeAssistant,
    mock_store: Mock,
    mock_icloud_service: MagicMock,
) -> None:
    """Test successful setup with devices."""

    assert mock_icloud_service is not None

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CONFIG, entry_id="test", unique_id=USERNAME
    )
    config_entry.add_to_hass(hass)

    account = IcloudAccount(
        hass,
        MOCK_CONFIG[CONF_USERNAME],
        MOCK_CONFIG[CONF_PASSWORD],
        mock_store,
        MOCK_CONFIG[CONF_WITH_FAMILY],
        MOCK_CONFIG[CONF_MAX_INTERVAL],
        MOCK_CONFIG[CONF_GPS_ACCURACY_THRESHOLD],
        config_entry,
    )

    with patch.object(account, "_schedule_next_fetch"):
        account.setup()

    assert account.api is not None
    assert account.owner_fullname == "user name"
    assert "johntravolta" in account.family_members_fullname
    assert account.family_members_fullname["johntravolta"] == "John TRAVOLTA"


# ---------------------------------------------------------------------------
# IcloudDevice.update() — stale location detection
# ---------------------------------------------------------------------------

_FETCH_INTERVAL_MIN = 30  # minutes — threshold = 30 * 60 * 1.5 = 2700 s


def _make_device(
    hass: HomeAssistant, age_seconds: float | None, is_old: bool
) -> IcloudDevice:
    """Create an IcloudDevice with a location of the given age and isOld flag."""
    mock_account = MagicMock()
    mock_account.hass = hass
    mock_account.signal_device_new = "icloud-test-device-new"
    mock_account.fetch_interval = _FETCH_INTERVAL_MIN
    mock_account.owner_fullname = "Test User"
    mock_account.family_members_fullname = {}

    ts_ms = (
        int((utcnow() - timedelta(seconds=age_seconds)).timestamp() * 1000)
        if age_seconds is not None
        else None
    )
    location = {
        "latitude": 60.1699,
        "longitude": 24.9384,
        "horizontalAccuracy": 10.0,
        "timeStamp": ts_ms,
        "isOld": is_old,
    }
    status = {**DEVICE, "location": location}
    device = IcloudDevice(mock_account, MagicMock(), dict(DEVICE))
    with patch("homeassistant.components.icloud.account.dispatcher_send"):
        device.update(status)
    return device


def test_icloud_device_clears_stale_location(hass: HomeAssistant) -> None:
    """Test that isOld=True with a timestamp older than 1.5x fetch_interval clears location.

    This is the core stale-location feature: Apple occasionally returns isOld=True
    to indicate a cached GPS fix. If that fix is older than the polling threshold,
    it is discarded so the device appears as 'unknown' rather than showing an
    outdated position.
    """
    device = _make_device(
        hass, age_seconds=3600, is_old=True
    )  # 3600s >> 2700s threshold
    assert device.location is None


def test_icloud_device_keeps_recent_location_when_is_old(hass: HomeAssistant) -> None:
    """Test that isOld=True with a recent timestamp keeps the location.

    If Apple sets isOld=True but the fix is only a minute old, it is still within
    the staleness threshold and should be accepted.
    """
    device = _make_device(hass, age_seconds=60, is_old=True)  # 60s << 2700s threshold
    assert device.location is not None


def test_icloud_device_keeps_location_when_not_is_old(hass: HomeAssistant) -> None:
    """Test that isOld=False keeps the location regardless of timestamp age.

    Only locations explicitly marked isOld=True are subject to staleness clearing.
    An old-looking timestamp alone is not sufficient to discard the fix.
    """
    device = _make_device(hass, age_seconds=3600, is_old=False)  # old but not flagged
    assert device.location is not None


def test_icloud_device_keeps_location_when_timestamp_missing(
    hass: HomeAssistant,
) -> None:
    """Test that isOld=True without a timestamp does not clear the location.

    Without a timestamp we cannot compute age_seconds, so the stale threshold
    check is skipped and the location is kept.
    """
    device = _make_device(hass, age_seconds=None, is_old=True)
    assert device.location is not None


# ---------------------------------------------------------------------------
# IcloudAccount — auth / keep_alive / 2FA handling
# ---------------------------------------------------------------------------


def _make_account(hass: HomeAssistant, mock_store: Mock) -> IcloudAccount:
    """Build an IcloudAccount with mocked config entry."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CONFIG, entry_id="test", unique_id=USERNAME
    )
    config_entry.add_to_hass(hass)
    return IcloudAccount(
        hass,
        MOCK_CONFIG[CONF_USERNAME],
        MOCK_CONFIG[CONF_PASSWORD],
        mock_store,
        MOCK_CONFIG[CONF_WITH_FAMILY],
        MOCK_CONFIG[CONF_MAX_INTERVAL],
        MOCK_CONFIG[CONF_GPS_ACCURACY_THRESHOLD],
        config_entry,
    )


async def test_setup_requires_2fa_logs_warning_not_error(
    hass: HomeAssistant,
    mock_store: Mock,
) -> None:
    """Test setup logs a warning (not error) when requires_2fa is True.

    PyiCloudFailedLoginException is raised internally for the 2FA path; logging
    'password no longer working' in that case would mislead the user.
    """
    account = _make_account(hass, mock_store)

    service_instance = MagicMock()
    service_instance.requires_2fa = True

    with (
        patch(
            "homeassistant.components.icloud.account.PyiCloudService",
            return_value=service_instance,
        ),
        patch.object(account, "_require_reauth") as mock_reauth,
        patch("homeassistant.components.icloud.account._LOGGER") as mock_logger,
    ):
        account.setup()

    mock_reauth.assert_called_once()
    assert account.api is None
    mock_logger.warning.assert_called_once()
    mock_logger.error.assert_not_called()


async def test_setup_auth_required_exception_calls_reauth(
    hass: HomeAssistant,
    mock_store: Mock,
) -> None:
    """Test setup handles PyiCloudAuthRequiredException by calling reauth.

    This covers the case where FMIP requires re-authentication even after the
    main iCloud login succeeded (e.g. MFA required specifically for Find My).
    Before this fix, the exception was unhandled and crashed setup.
    """
    account = _make_account(hass, mock_store)

    with (
        patch(
            "homeassistant.components.icloud.account.PyiCloudService",
            side_effect=PyiCloudAuthRequiredException("test@example.com", MagicMock()),
        ),
        patch.object(account, "_require_reauth") as mock_reauth,
    ):
        account.setup()

    mock_reauth.assert_called_once()
    assert account.api is None


async def test_setup_auth_required_exception_from_devices_calls_reauth(
    hass: HomeAssistant,
    mock_store: Mock,
) -> None:
    """Test setup handles PyiCloudAuthRequiredException raised when reading devices.

    This covers the case where auth is required when accessing device data
    (e.g. api.devices.user_info) after service construction succeeded.
    Before this fix, the exception was unhandled and crashed setup.
    """
    account = _make_account(hass, mock_store)

    class _DevicesAuthError:
        @property
        def user_info(self):
            raise PyiCloudAuthRequiredException("test@example.com", MagicMock())

    service_instance = MagicMock()
    service_instance.requires_2fa = False
    service_instance.devices = _DevicesAuthError()

    with (
        patch(
            "homeassistant.components.icloud.account.PyiCloudService",
            return_value=service_instance,
        ),
        patch.object(account, "_require_reauth") as mock_reauth,
        patch("homeassistant.components.icloud.account._LOGGER") as mock_logger,
    ):
        account.setup()

    mock_reauth.assert_called_once()
    assert account.api is None
    mock_logger.error.assert_called_once()
    mock_logger.warning.assert_not_called()


def test_handle_auth_required_with_2fa_logs_warning(
    hass: HomeAssistant,
    mock_store: Mock,
) -> None:
    """Test _handle_auth_required logs a warning (not error) when requires_2fa=True."""
    account = _make_account(hass, mock_store)
    account.api = MagicMock()

    with (
        patch.object(account, "_require_reauth") as mock_reauth,
        patch("homeassistant.components.icloud.account._LOGGER") as mock_logger,
    ):
        account._handle_auth_required(requires_2fa=True)

    assert account.api is None
    mock_reauth.assert_called_once()
    mock_logger.warning.assert_called_once()
    mock_logger.error.assert_not_called()


async def test_keep_alive_reschedules_when_setup_returns_api_none(
    hass: HomeAssistant,
    mock_store: Mock,
) -> None:
    """Test keep_alive reschedules at max interval when setup() returns with api None (2FA path).

    Before this fix, keep_alive() would call setup() (because api was None), then return
    early without calling _schedule_next_fetch(), permanently killing the polling loop.
    """
    account = _make_account(hass, mock_store)

    with (
        patch.object(account, "setup"),
        patch.object(account, "_schedule_next_fetch") as mock_schedule,
    ):
        account.keep_alive()

    mock_schedule.assert_called_once()
    assert account.fetch_interval == MOCK_CONFIG[CONF_MAX_INTERVAL]


async def test_keep_alive_does_not_reschedule_when_setup_detects_bad_password(
    hass: HomeAssistant,
    mock_store: Mock,
) -> None:
    """Test keep_alive stops polling when setup() detects a permanent credential failure.

    Rescheduling every max_interval with bad credentials would spam the user with
    reauth notifications. The _setup_credential_failure flag set by setup() prevents
    keep_alive from rescheduling in this case.
    """
    account = _make_account(hass, mock_store)
    account._setup_credential_failure = True

    with (
        patch.object(account, "setup"),
        patch.object(account, "_schedule_next_fetch") as mock_schedule,
    ):
        account.keep_alive()

    mock_schedule.assert_not_called()
    assert not account._setup_credential_failure


async def test_keep_alive_reschedules_when_setup_raises(
    hass: HomeAssistant,
    mock_store: Mock,
) -> None:
    """Test keep_alive reschedules when setup() raises (e.g. transient Apple API outage).

    ConfigEntryNotReady raised by setup() during a keep_alive cycle would propagate
    uncaught and permanently kill the polling loop. The exception is now caught and
    the next fetch is still scheduled at max interval.
    """
    account = _make_account(hass, mock_store)

    with (
        patch.object(
            account, "setup", side_effect=ConfigEntryNotReady("Apple API unavailable")
        ),
        patch.object(account, "_schedule_next_fetch") as mock_schedule,
    ):
        account.keep_alive()

    mock_schedule.assert_called_once()
    assert account.fetch_interval == MOCK_CONFIG[CONF_MAX_INTERVAL]


async def test_keep_alive_clears_api_and_reschedules_when_setup_raises_after_partial_init(
    hass: HomeAssistant,
    mock_store: Mock,
) -> None:
    """Test keep_alive clears api and reschedules when setup() sets api then raises.

    setup() can assign self.api (PyiCloudService) and then raise ConfigEntryNotReady
    (e.g. PyiCloudServiceUnavailable during device fetch). Without clearing api in the
    except block, keep_alive would proceed to authenticate() with a partially-initialized
    api instead of taking the reschedule path.
    """
    account = _make_account(hass, mock_store)

    def setup_sets_api_then_raises():
        account.api = MagicMock()
        raise ConfigEntryNotReady("Service unavailable")

    with (
        patch.object(account, "setup", side_effect=setup_sets_api_then_raises),
        patch.object(account, "_schedule_next_fetch") as mock_schedule,
    ):
        account.keep_alive()

    assert account.api is None
    mock_schedule.assert_called_once()
    assert account.fetch_interval == MOCK_CONFIG[CONF_MAX_INTERVAL]


async def test_keep_alive_auth_exception_reschedules(
    hass: HomeAssistant,
    mock_store: Mock,
) -> None:
    """Test keep_alive reschedules on transient auth error instead of dying permanently.

    Before this fix, any exception from authenticate() propagated out of keep_alive()
    without scheduling the next call, permanently killing the polling loop.
    """
    account = _make_account(hass, mock_store)
    account.api = MagicMock()
    account.api.authenticate.side_effect = Exception("Session expired")

    with patch.object(account, "_schedule_next_fetch") as mock_schedule:
        account.keep_alive()

    mock_schedule.assert_called_once()
    assert account.fetch_interval == 2


async def test_keep_alive_failed_login_triggers_reauth(
    hass: HomeAssistant,
    mock_store: Mock,
) -> None:
    """Test keep_alive triggers reauth on PyiCloudFailedLoginException instead of retrying.

    Permanent credential failures (bad password) must not be retried — the integration
    should stop polling and prompt the user to re-enter credentials via reauth.
    """
    account = _make_account(hass, mock_store)
    account.api = MagicMock()
    account.api.requires_2fa = False
    account.api.authenticate.side_effect = PyiCloudFailedLoginException("bad password")

    with (
        patch.object(account, "_require_reauth") as mock_reauth,
        patch.object(account, "_schedule_next_fetch") as mock_schedule,
        patch("homeassistant.components.icloud.account._LOGGER") as mock_logger,
    ):
        account.keep_alive()

    mock_reauth.assert_called_once()
    mock_schedule.assert_not_called()
    assert account.api is None
    mock_logger.error.assert_called_once()
    mock_logger.warning.assert_not_called()


async def test_keep_alive_failed_login_2fa_logs_warning_not_error(
    hass: HomeAssistant,
    mock_store: Mock,
) -> None:
    """Test keep_alive logs a warning (not error) when the failure is due to 2FA.

    PyiCloudFailedLoginException is also raised for 2FA flows; logging 'password
    no longer working' in that case would mislead the user.
    """
    account = _make_account(hass, mock_store)
    account.api = MagicMock()
    account.api.requires_2fa = True
    account.api.authenticate.side_effect = PyiCloudFailedLoginException("2FA required")

    with (
        patch.object(account, "_require_reauth") as mock_reauth,
        patch.object(account, "_schedule_next_fetch") as mock_schedule,
        patch("homeassistant.components.icloud.account._LOGGER") as mock_logger,
    ):
        account.keep_alive()

    mock_reauth.assert_called_once()
    mock_schedule.assert_called_once()
    assert account.api is None
    assert account.fetch_interval == MOCK_CONFIG[CONF_MAX_INTERVAL]
    mock_logger.warning.assert_called_once()
    mock_logger.error.assert_not_called()


async def test_keep_alive_success_sends_locate_before_update(
    hass: HomeAssistant,
    mock_store: Mock,
) -> None:
    """Test keep_alive sends an active locate push before updating devices."""
    account = _make_account(hass, mock_store)
    account.api = MagicMock()
    account.api.requires_2fa = False

    with (
        patch.object(account, "update_devices") as mock_update,
        patch.object(account, "_schedule_next_fetch"),
    ):
        account.keep_alive()

    account.api.devices.refresh.assert_called_once_with(locate=True)
    mock_update.assert_called_once()


async def test_update_devices_requires_2fa_reschedules(
    hass: HomeAssistant,
    mock_store: Mock,
) -> None:
    """Test update_devices reschedules the next poll even when requires_2fa is True.

    Before this fix, the requires_2fa early return skipped _schedule_next_fetch(),
    permanently killing the polling loop while waiting for re-authentication.
    """
    account = _make_account(hass, mock_store)
    account.api = MagicMock()
    account.api.requires_2fa = True

    with (
        patch.object(account, "_require_reauth") as mock_reauth,
        patch.object(account, "_schedule_next_fetch") as mock_schedule,
    ):
        account.update_devices()

    mock_reauth.assert_called_once()
    mock_schedule.assert_called_once()
    assert account.fetch_interval == MOCK_CONFIG[CONF_MAX_INTERVAL]
