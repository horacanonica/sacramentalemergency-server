from app.failsafe import enter_manual_failsafe
from app.notifier import EmailConfig, Notifier, brand_alert
from app.rotation import RotationManager
from app.scheduler import SIGNAL_DOWN_MESSAGE, check_signal_health
from app.signal_client import IncomingMessage, SignalError


class FakeSignalClient:
    def __init__(self, healthy: bool = True) -> None:
        self.sent: list[tuple[list[str], str]] = []
        self.healthy = healthy
        self.fail = False

    def send(self, to_numbers: list[str], message: str, *, force: bool = False, attachments=None) -> None:
        if self.fail:
            raise SignalError("signal down")
        self.sent.append((list(to_numbers), message))

    def receive(self) -> list[IncomingMessage]:
        return []

    def is_healthy(self) -> bool:
        return self.healthy


class FakeSmsDriver:
    def __init__(self) -> None:
        self.sent: list[tuple[list[str], str]] = []
        self.fail = False

    def send_sms(self, to_numbers: list[str], message: str) -> None:
        if self.fail:
            from app.ringcentral_client import RingCentralDriverError

            raise RingCentralDriverError("sms failed")
        self.sent.append((list(to_numbers), message))


def make_notifier(signal_client, sms_driver=None, numbers=None) -> Notifier:
    cells = numbers or ["+19165550001", "+19165550002"]
    return Notifier(
        signal_client,
        EmailConfig(smtp_host="", smtp_port=587, smtp_username="", smtp_password="", smtp_from="", alert_to=[]),
        alert_numbers=cells,
        sms_driver=sms_driver,
        numbers_provider=lambda: cells,
    )


def test_brand_alert_prefixes_once():
    assert brand_alert("line is down") == "Emergency Line bot: line is down"
    assert brand_alert("Emergency Line bot: already") == "Emergency Line bot: already"


def test_alert_sends_signal_and_sms(priests_config, state_path):
    signal_client = FakeSignalClient()
    sms = FakeSmsDriver()
    notifier = make_notifier(signal_client, sms)
    notifier.alert("RingCentral update failed")
    assert signal_client.sent
    assert sms.sent
    assert all(msg.startswith("Emergency Line bot:") for _, msg in signal_client.sent)
    assert all(msg.startswith("Emergency Line bot:") for _, msg in sms.sent)
    assert signal_client.sent[0][0] == sms.sent[0][0]


def test_alert_still_sends_sms_when_signal_fails():
    signal_client = FakeSignalClient()
    signal_client.fail = True
    sms = FakeSmsDriver()
    notifier = make_notifier(signal_client, sms)
    notifier.alert("something broke")
    assert signal_client.sent == []
    assert sms.sent
    assert "Emergency Line bot:" in sms.sent[0][1]


def test_signal_down_alerts_once_then_resets(priests_config, state_path):
    rotation = RotationManager(config_path=priests_config, state_path=state_path)
    signal_client = FakeSignalClient(healthy=False)
    sms = FakeSmsDriver()
    notifier = make_notifier(signal_client, sms)
    check_signal_health(rotation, signal_client, notifier)
    check_signal_health(rotation, signal_client, notifier)
    assert len(sms.sent) == 1
    assert SIGNAL_DOWN_MESSAGE in sms.sent[0][1]
    assert "Emergency Line bot:" in sms.sent[0][1]
    signal_client.healthy = True
    check_signal_health(rotation, signal_client, notifier)
    assert rotation.signal_down_alerted is False
    signal_client.healthy = False
    check_signal_health(rotation, signal_client, notifier)
    assert len(sms.sent) == 2


def test_failsafe_uses_sms_and_brand(priests_config, state_path):
    rotation = RotationManager(config_path=priests_config, state_path=state_path)
    signal_client = FakeSignalClient()
    sms = FakeSmsDriver()
    notifier = make_notifier(signal_client, sms, numbers=["+19165550001"])
    entered = enter_manual_failsafe(
        rotation, signal_client, None, "test reason", notifier=notifier
    )
    assert entered is True
    assert any("Emergency Line bot:" in msg for _, msg in signal_client.sent)
    assert any("Emergency Line bot:" in msg for _, msg in sms.sent)
    assert any("test reason" in msg for _, msg in sms.sent)
