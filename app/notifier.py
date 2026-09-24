"""Notification layer: Signal first, email fallback if Signal itself is
the thing that's broken (so an outage doesn't fail silently).
Error alerts also go out as RingCentral SMS from the emergency-line number.
"""
from __future__ import annotations

import logging
import smtplib
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage

from app.ringcentral_client import RingCentralDriver, RingCentralDriverError
from app.signal_client import SignalClient, SignalError

logger = logging.getLogger(__name__)

BOT_LABEL = "Emergency Line bot"


def brand_alert(message: str) -> str:
    """Prefix so an SMS from the emergency-line number is not mistaken
    for a caller or a personal text."""
    text = (message or "").strip()
    if text.startswith(BOT_LABEL):
        return text
    return f"{BOT_LABEL}: {text}"


@dataclass
class EmailConfig:
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str
    alert_to: list[str]

    @property
    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from and self.alert_to)


class Notifier:
    def __init__(
        self,
        signal_client: SignalClient,
        email_config: EmailConfig,
        alert_numbers: list[str] | None = None,
        sms_driver: RingCentralDriver | None = None,
        numbers_provider: Callable[[], list[str]] | None = None,
    ) -> None:
        self.signal_client = signal_client
        self.email_config = email_config
        # Numbers to Signal-alert on failures (typically the same three
        # priests, but kept separate so an admin/sysadmin number can be
        # added here without touching the priest roster).
        self.alert_numbers = alert_numbers or []
        self.sms_driver = sms_driver
        self.numbers_provider = numbers_provider

    def error_numbers(self) -> list[str]:
        if self.numbers_provider is not None:
            return [n for n in self.numbers_provider() if n]
        return list(self.alert_numbers)

    def notify_all(self, numbers: list[str], message: str) -> None:
        """Best-effort confirmation to priests after a successful rotation.
        A Signal failure here still gets an email alert (so someone
        notices), but does not raise - the rotation itself already
        succeeded and that's the important part."""
        try:
            self.signal_client.send(numbers, message)
        except SignalError as exc:
            logger.exception("Failed to send Signal confirmation")
            self._email_fallback(
                subject="Sacramental Line: rotation succeeded, but Signal notification failed",
                body=f"Rotation succeeded. Attempted to send this confirmation via Signal but it failed:\n\n{exc}\n\nIntended message:\n{message}",
            )

    def alert(self, message: str) -> None:
        """Something actually failed (rotation itself, API auth, Signal
        down, etc.). Send Signal and SMS to every priest number. Email
        is a last resort if both of those fail."""
        branded = brand_alert(message)
        logger.error("ALERT: %s", branded)
        numbers = self.error_numbers()
        signal_ok = False
        sms_ok = False
        if numbers and self.signal_client is not None:
            try:
                self.signal_client.send(numbers, branded, force=True)
                signal_ok = True
            except SignalError:
                logger.exception("Failed to send Signal alert")
        if numbers and self.sms_driver is not None:
            try:
                self.sms_driver.send_sms(numbers, branded)
                sms_ok = True
            except RingCentralDriverError:
                logger.exception("Failed to send SMS alert")
        if not signal_ok and not sms_ok:
            self._email_fallback(subject="Sacramental Line: ALERT", body=branded)

    def _email_fallback(self, subject: str, body: str) -> None:
        if not self.email_config.is_configured:
            logger.error("Email fallback not configured; alert was only logged: %s", subject)
            return
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = self.email_config.smtp_from
            msg["To"] = ", ".join(self.email_config.alert_to)
            msg.set_content(body)
            with smtplib.SMTP(self.email_config.smtp_host, self.email_config.smtp_port, timeout=15) as server:
                server.starttls()
                if self.email_config.smtp_username:
                    server.login(self.email_config.smtp_username, self.email_config.smtp_password)
                server.send_message(msg)
        except Exception:  # noqa: BLE001 - this IS the last-resort fallback; must not raise further
            logger.exception("Email fallback ALSO failed. This alert only exists in the logs now: %s", subject)
