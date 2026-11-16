"""Phone app: a small web remote served from the laptop.

The page itself only comes from the laptop; every command goes from the phone's
browser straight to the public ntfy.sh topic, and the laptop picks it up there.

The secret topic is never sent by this server: it travels in the URL #fragment of
the printed link / QR code (browsers never send fragments to servers), and the
phone remembers it. Someone scanning the LAN only gets an unlinked remote.
"""
import socket
import sys

from flask import Flask, abort, render_template, request
from markupsafe import Markup

from common import load_config

PORT = 8000


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def create_app(cfg: dict) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template("remote.html", server=cfg["server"])

    @app.get("/pair")
    def pair():
        # The pairing page contains the secret, so it is only served to the laptop itself.
        if request.remote_addr not in ("127.0.0.1", "::1"):
            abort(404)
        url = phone_url(cfg)
        return render_template("pair.html", url=url, qr_svg=Markup(qr_svg(url)))

    @app.after_request
    def no_store(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    return app


def phone_url(cfg: dict, port: int = PORT) -> str:
    return f"http://{lan_ip()}:{port}/#{cfg['topic']}"


def qr_svg(data: str) -> str:
    import qrcode
    from qrcode.image.svg import SvgPathFillImage

    img = qrcode.make(data, image_factory=SvgPathFillImage, box_size=12, border=2)
    return img.to_string(encoding="unicode")


def open_pairing_page(port: int = PORT):
    """Show the QR code + link on the laptop screen once the server is up."""
    import time
    import urllib.request
    import webbrowser

    pair = f"http://127.0.0.1:{port}/pair"
    for _ in range(50):
        try:
            urllib.request.urlopen(pair, timeout=1).close()
            break
        except OSError:
            time.sleep(0.2)
    webbrowser.open(pair)


def print_banner(cfg: dict, port: int = PORT):
    url = phone_url(cfg, port)
    print(f"\n  Phone remote: {url}")
    print(f"  Pairing page (QR + link): http://127.0.0.1:{port}/pair")
    print("  Scan the QR with your phone (same Wi-Fi). The part after # is your secret topic.")
    print("  If the IP is wrong (VPN/WSL adapter), use the laptop's Wi-Fi IP from `ipconfig`.\n")
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except Exception:
        pass
    sys.stdout.flush()


def serve(cfg: dict | None = None, port: int = PORT):
    cfg = cfg or load_config()
    print_banner(cfg, port)
    create_app(cfg).run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    serve()
