"""Play a video on a smart TV (Samsung, LG, Sony...) without screen mirroring.

Most smart TVs are DLNA "media renderers": you give them a link to a video and they fetch and play it
themselves, so picture and sound stay in sync and the phone only works as a remote.
1. find:  ask the home network (SSDP, UDP multicast) which devices can play media
2. play:  send the TV the video's link (SetAVTransportURI) and Play; then Pause / Seek / Stop
3. watch: ask where it is (GetPositionInfo), so your progress is saved like on the phone
Pure Python (sockets + httpx), so it installs on Termux too.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import socket
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
from xml.sax.saxutils import escape

import httpx

log = logging.getLogger(__name__)

SSDP_ADDR = ("239.255.255.250", 1900)
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
SEARCH_TARGETS = (AVT, "urn:schemas-upnp-org:device:MediaRenderer:1")
# what a TV is told about the video: plain HTTP streaming, seeking by bytes allowed
DLNA_FEATURES = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"


def _ssdp_search(timeout: float, targets=SEARCH_TARGETS) -> set[str]:
    """Every LOCATION (device description address) that answered on the home network."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    found: set[str] = set()
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.5)
        sock.bind(("", 0))
        for _ in range(2):  # UDP may get lost: ask twice
            for st in targets:
                msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
                       f"MX: 2\r\nST: {st}\r\n\r\n")
                try:
                    sock.sendto(msg.encode(), SSDP_ADDR)
                except OSError as e:
                    log.warning("TV search: could not send on the network: %s", e)
                    return found
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            try:
                data, _ = sock.recvfrom(65507)
            except socket.timeout:
                continue
            except OSError:
                break
            m = re.search(rb"^location:\s*(\S+)", data, re.I | re.M)
            if m:
                found.add(m.group(1).decode(errors="ignore"))
    finally:
        sock.close()
    return found


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_description(xml_text: str, location: str) -> dict | None:
    """The TV's name and where to send AVTransport commands, from its device description."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    base = location
    for el in root.iter():
        if _local(el.tag) == "URLBase" and (el.text or "").strip():
            base = el.text.strip()
    name, control, udn = None, None, None
    for dev in (el for el in root.iter() if _local(el.tag) == "device"):
        fields = {_local(c.tag): (c.text or "").strip() for c in dev}
        for svc in (el for el in dev.iter() if _local(el.tag) == "service"):
            s = {_local(c.tag): (c.text or "").strip() for c in svc}
            if s.get("serviceType", "").startswith("urn:schemas-upnp-org:service:AVTransport:"):
                control = urljoin(base, s.get("controlURL", ""))
                name = fields.get("friendlyName") or name
                udn = fields.get("UDN") or udn
                break
        if control:
            break
    if not control:
        return None
    return {"id": hashlib.sha1((udn or location).encode()).hexdigest()[:12], "name": name or "טלוויזיה",
            "control": control, "location": location}


async def discover(timeout: float = 3.0) -> list[dict]:
    """The TVs (and other players) on the home network that can play a video link."""
    locations = await asyncio.to_thread(_ssdp_search, timeout)
    out: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=4) as client:
        async def one(loc: str) -> None:
            try:
                r = await client.get(loc)
                dev = parse_description(r.text, loc)
                if dev:
                    out[dev["id"]] = dev
            except Exception as e:  # noqa: BLE001 - a device that does not answer is just left out
                log.info("TV search: %s did not answer (%s)", loc, type(e).__name__)
        await asyncio.gather(*(one(loc) for loc in locations))
    log.info("TV search: %d answers, %d can play videos: %s", len(locations), len(out),
             ", ".join(d["name"] for d in out.values()) or "-")
    return sorted(out.values(), key=lambda d: d["name"])


def hms(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def parse_hms(text: str | None) -> float | None:
    m = re.match(r"^(\d+):(\d{1,2}):(\d{1,2})(?:\.\d+)?$", (text or "").strip())
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else None


def didl(title: str, url: str, mime: str) -> str:
    """What the TV shows about the video (its name), and how to fetch it."""
    return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            f'<item id="0" parentID="-1" restricted="1"><dc:title>{escape(title)}</dc:title>'
            '<upnp:class>object.item.videoItem</upnp:class>'
            f'<res protocolInfo="http-get:*:{escape(mime)}:{DLNA_FEATURES}">{escape(url)}</res></item></DIDL-Lite>')


class TVError(Exception):
    pass


async def soap(control: str, action: str, args: dict | None = None, timeout: float = 8) -> dict:
    """Send one command to the TV; returns the answer's fields."""
    params = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in {"InstanceID": 0, **(args or {})}.items())
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{AVT}">{params}</u:{action}></s:Body></s:Envelope>')
    headers = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{AVT}#{action}"'}
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(control, content=body.encode(), headers=headers)
    if r.status_code != 200:
        code = re.search(r"<errorCode>(\d+)</errorCode>", r.text)
        desc = re.search(r"<errorDescription>([^<]*)</errorDescription>", r.text)
        log.warning("TV said no to %s: HTTP %s %s %s", action, r.status_code,
                    code.group(1) if code else "", desc.group(1) if desc else "")
        raise TVError(f"{action}: {r.status_code} {code.group(1) if code else ''}".strip())
    out = {}
    try:
        for el in ET.fromstring(r.text).iter():
            if not list(el):
                out[_local(el.tag)] = (el.text or "").strip()
    except ET.ParseError:
        pass
    return out


class TVPlayer:
    """One video playing on one TV; remembers where it is so your progress is saved."""

    def __init__(self, device: dict, media_id: int, url: str, title: str, mime: str):
        self.device, self.media_id, self.url, self.title, self.mime = device, media_id, url, title, mime
        self.state = "starting"  # starting / PLAYING / PAUSED_PLAYBACK / STOPPED / error
        self.position: float | None = None
        self.duration: float | None = None
        self.error: str | None = None

    async def start(self, at: float = 0) -> None:
        c = self.device["control"]
        try:
            await soap(c, "Stop")  # whatever the TV was playing
        except Exception:  # noqa: BLE001 - nothing was playing
            pass
        await soap(c, "SetAVTransportURI", {"CurrentURI": self.url,
                                            "CurrentURIMetaData": didl(self.title, self.url, self.mime)})
        await soap(c, "Play", {"Speed": 1})
        self.state = "PLAYING"
        if at > 5:  # TVs only accept a jump once the video is running
            for _ in range(30):
                await asyncio.sleep(0.5)
                if (await self.refresh()) == "PLAYING":
                    break
            try:
                await soap(c, "Seek", {"Unit": "REL_TIME", "Target": hms(at)})
            except TVError:
                log.info("the TV did not accept the jump to %s", hms(at))

    async def refresh(self) -> str:
        c = self.device["control"]
        info = await soap(c, "GetTransportInfo", timeout=4)
        self.state = info.get("CurrentTransportState") or self.state
        pos = await soap(c, "GetPositionInfo", timeout=4)
        p, d = parse_hms(pos.get("RelTime")), parse_hms(pos.get("TrackDuration"))
        if p is not None and (p > 0 or self.position is None):
            self.position = p
        if d:
            self.duration = d
        return self.state

    async def command(self, action: str, value: float | None = None) -> None:
        c = self.device["control"]
        if action == "pause":
            await soap(c, "Pause")
            self.state = "PAUSED_PLAYBACK"
        elif action == "play":
            await soap(c, "Play", {"Speed": 1})
            self.state = "PLAYING"
        elif action == "seek" and value is not None:
            await soap(c, "Seek", {"Unit": "REL_TIME", "Target": hms(value)})
            self.position = max(0.0, value)
        elif action == "stop":
            try:
                await soap(c, "Stop")
            finally:
                self.state = "STOPPED"
