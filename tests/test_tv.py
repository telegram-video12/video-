"""Playing on a smart TV (DLNA) against a pretend TV on this computer."""
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_api import client, login, scan  # noqa: F401 - pytest fixture

from app import dlna

DESC = """<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>
<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType><friendlyName>[TV] Samsung סלון</friendlyName>
<UDN>uuid:1234</UDN><serviceList><service><serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
<controlURL>/rc</controlURL></service><service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
<controlURL>/upnp/control/AVTransport1</controlURL></service></serviceList></device></root>"""


class FakeTV(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = DESC.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        data = self.rfile.read(int(self.headers["Content-Length"])).decode()
        action = self.headers["SOAPAction"].strip('"').split("#")[1]
        FakeTV.calls.append((action, data))
        extra = {"GetTransportInfo": "<CurrentTransportState>PLAYING</CurrentTransportState>",
                 "GetPositionInfo": "<RelTime>0:01:40</RelTime><TrackDuration>0:45:00</TrackDuration>"}.get(action, "")
        body = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                f'<u:{action}Response xmlns:u="{dlna.AVT}">{extra}</u:{action}Response></s:Body></s:Envelope>').encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_description_and_time_helpers():
    d = dlna.parse_description(DESC, "http://10.0.0.5:9197/dmr")
    assert d["name"] == "[TV] Samsung סלון" and d["control"] == "http://10.0.0.5:9197/upnp/control/AVTransport1"
    assert dlna.parse_description("<root/>", "x") is None and dlna.parse_description("not xml", "x") is None
    assert dlna.hms(3725) == "1:02:05" and dlna.parse_hms("1:02:05") == 3725 and dlna.parse_hms("NOT_IMPLEMENTED") is None
    assert "&lt;" in dlna.didl("a<b", "http://x/?t=1&y=2", "video/mp4") and "&amp;y=2" in dlna.didl("a", "http://x/?t=1&y=2", "v")


def test_search_does_not_crash_without_tvs():
    assert isinstance(dlna._ssdp_search(0.5), set)


def test_play_on_tv_end_to_end(client, monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeTV)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    FakeTV.calls = []
    monkeypatch.setattr(dlna, "_ssdp_search", lambda timeout, targets=None: {f"http://127.0.0.1:{srv.server_port}/dmr"})
    try:
        login(client)
        scan(client)
        devs = client.get("/api/tv/devices?fresh=1").json()["devices"]
        assert [d["name"] for d in devs] == ["[TV] Samsung סלון"]
        mid = client.get("/api/library").json()["titles"][0]["first_media_id"]
        r = client.post("/api/tv/play", json={"device": devs[0]["id"], "media_id": mid, "position": 100})
        assert r.status_code == 200, r.text
        acts = [a for a, _ in FakeTV.calls]
        assert acts[:3] == ["Stop", "SetAVTransportURI", "Play"] and "Seek" in acts
        sent = dict(FakeTV.calls)["SetAVTransportURI"]
        url = re.search(r"<CurrentURI>([^<]+)</CurrentURI>", sent).group(1).replace("&amp;", "&")
        assert f"/api/stream/{mid}?t=" in url and "0:01:40" in dict(FakeTV.calls)["Seek"]
        # the link works for the TV without logging in, and says it is a DLNA stream
        path = url.split(":", 2)[2].split("/", 1)[1]
        client.cookies.clear()
        h = client.head("/" + path)
        assert h.status_code == 200 and h.headers["transferMode.dlna.org"] == "Streaming"
        assert client.get("/" + path, headers={"Range": "bytes=0-9"}).status_code == 206
        assert client.get(f"/api/stream/{mid}").status_code == 401  # without the key: no
        login(client)
        st = client.get("/api/tv/status").json()
        assert st["active"] and st["device"] == "[TV] Samsung סלון"
        assert client.post("/api/tv/control", json={"action": "pause"}).json()["state"] == "PAUSED_PLAYBACK"
        client.post("/api/tv/control", json={"action": "seek", "delta": 10})
        assert "0:01:50" in [d for a, d in FakeTV.calls if a == "Seek"][-1]
        client.post("/api/tv/control", json={"action": "stop"})
        assert FakeTV.calls[-1][0] == "Stop" and client.get("/api/tv/status").json()["active"] is False
        media = client.get(f"/api/media/{mid}").json()["media"]
        assert media["position"] >= 100  # where the TV got to is saved
    finally:
        srv.shutdown()
