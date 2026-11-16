"""Phone app: a small web remote served from the laptop.

The page itself only comes from the laptop; every command goes from the phone's
browser straight to the public ntfy.sh topic, and the laptop picks it up there.

The secret topic is never sent by this server: it travels in the URL #fragment of
the printed link / QR code (browsers never send fragments to servers), and the
phone remembers it. Someone scanning the LAN only gets an unlinked remote.
"""
import socket
import sys

from flask import Flask, render_template

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

    @app.after_request
    def no_store(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    return app


def print_banner(cfg: dict, port: int = PORT):
    url = f"http://{lan_ip()}:{port}/#{cfg['topic']}"
    print(f"\n  Phone remote: {url}")
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
