"""Shared config: the ntfy server and the secret topic both apps talk on."""
import json
import os
import re
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
PROFILE_DIR = ROOT / ".browser-profile"

TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")  # use fullmatch: "$" would accept a trailing newline


def load_config() -> dict:
    """Load config.json, creating it with a random (unguessable) topic on first run.

    The topic name is the only secret: anyone who knows it can send commands,
    so it is long and random. Delete config.json to rotate it.
    Env overrides: NTFY_SERVER, YTR_TOPIC.
    """
    cfg = {}
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    changed = False
    if not cfg.get("server"):
        cfg["server"] = "https://ntfy.sh"
        changed = True
    if not cfg.get("topic"):
        cfg["topic"] = "ytr-" + secrets.token_urlsafe(18)
        changed = True
    if changed:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    cfg["server"] = os.environ.get("NTFY_SERVER", cfg["server"]).rstrip("/")
    cfg["topic"] = os.environ.get("YTR_TOPIC", cfg["topic"])
    if not TOPIC_RE.fullmatch(cfg["topic"]):
        raise SystemExit(f"Invalid topic {cfg['topic']!r}: use 1-64 chars of A-Z a-z 0-9 _ -")
    # Status goes back to the phone on a sibling topic.
    cfg["status_topic"] = cfg["topic"][:57] + "-status"
    return cfg
