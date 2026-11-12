"""How a message reaches a counterparty. Each transport sends one message and raises ``TransportError`` on failure;
none of them decides anything. Credentials come from settings and never appear in errors or logs."""

import asyncio
import base64
import hashlib
import hmac
import smtplib
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol

import httpx

from app.counterparty.netguard import BlockedDestination, check_public_url

_TWILIO_API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
_MAX_AGENT_REPLY_BYTES = 64 * 1024


class TransportError(Exception):
    """The message could not be delivered. The text is safe to show (no credentials, no provider internals)."""


class TransportUnavailable(TransportError):
    """This transport is not configured in this deployment."""


class EmailSender(Protocol):
    async def send_email(self, to: str, subject: str, body: str) -> None: ...


class TextSender(Protocol):
    async def send_text(self, to: str, body: str, *, whatsapp: bool) -> None: ...


class AgentClient(Protocol):
    async def post(self, url: str, payload: Mapping[str, object]) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class SmtpEmail:
    host: str
    port: int
    sender: str
    username: str | None = None
    password: str | None = None
    starttls: bool = True
    timeout: float = 20.0

    def _send(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            if self.starttls:
                smtp.starttls()
            if self.username and self.password:
                smtp.login(self.username, self.password)
            smtp.send_message(message)

    async def send_email(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.sender, to, subject
        message.set_content(body)
        try:
            await asyncio.to_thread(self._send, message)
        except (smtplib.SMTPException, OSError) as exc:
            raise TransportError("the email could not be sent") from exc


class TwilioText:
    """SMS and WhatsApp through Twilio's REST API (no SDK)."""

    def __init__(
        self, account_sid: str, auth_token: str, *, sms_from: str | None, whatsapp_from: str | None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._sid, self._token = account_sid, auth_token
        self._sms_from, self._whatsapp_from = sms_from, whatsapp_from
        self._client = client or httpx.AsyncClient(timeout=15.0)

    async def send_text(self, to: str, body: str, *, whatsapp: bool) -> None:
        sender = self._whatsapp_from if whatsapp else self._sms_from
        if not sender:
            raise TransportUnavailable("WhatsApp is not configured" if whatsapp else "SMS is not configured")
        prefix = "whatsapp:" if whatsapp else ""
        try:
            response = await self._client.post(
                _TWILIO_API.format(sid=self._sid), auth=(self._sid, self._token),
                data={"From": prefix + sender, "To": prefix + to, "Body": body[:1500]},
            )
        except httpx.HTTPError as exc:
            raise TransportError("the text message could not be sent") from exc
        if response.status_code >= 300:
            raise TransportError(f"the text message was refused (HTTP {response.status_code})")

    async def aclose(self) -> None:
        await self._client.aclose()


def twilio_signature_valid(auth_token: str, url: str, params: Mapping[str, str], signature: str) -> bool:
    """Twilio's request signature: base64(HMAC-SHA1(token, url + each param name and value, sorted by name))."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    expected = base64.b64encode(hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(expected, signature)


class HttpAgentClient:
    """JSON over HTTPS to another agent. No redirects (each hop would need its own SSRF check), bounded reply size."""

    def __init__(self, *, timeout: float, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    async def post(self, url: str, payload: Mapping[str, object]) -> dict[str, Any] | None:
        try:
            await check_public_url(url)
            response = await self._client.post(url, json=dict(payload))
        except BlockedDestination as exc:
            raise TransportError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise TransportError("the agent could not be reached") from exc
        if response.status_code == 202 or response.status_code == 204:
            return None  # accepted: the reply comes later through the callback URL
        if response.status_code >= 300:
            raise TransportError(f"the agent refused the request (HTTP {response.status_code})")
        if len(response.content) > _MAX_AGENT_REPLY_BYTES:
            raise TransportError("the agent's reply was too large")
        try:
            body = response.json()
        except ValueError as exc:
            raise TransportError("the agent's reply was not JSON") from exc
        return body if isinstance(body, dict) else None

    async def aclose(self) -> None:
        await self._client.aclose()
