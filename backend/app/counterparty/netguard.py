"""SSRF guard for agent endpoints: the host must resolve only to public addresses, checked right before each call
(a URL that validated at intake can point somewhere else later)."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from app.schemas.counterparty import is_public_https_url


class BlockedDestination(Exception):
    """The URL is not https or resolves to a private, loopback, link-local or reserved address."""


async def check_public_url(url: str) -> None:
    if not is_public_https_url(url):
        raise BlockedDestination("the endpoint must be a public https URL")
    parts = urlsplit(url)
    host, port = parts.hostname or "", parts.port or 443
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise BlockedDestination("the endpoint's host could not be resolved") from exc
    if not infos:
        raise BlockedDestination("the endpoint's host could not be resolved")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise BlockedDestination("the endpoint resolves to a non-public address")
