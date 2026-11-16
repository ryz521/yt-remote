"""Start both apps on the laptop: the phone web remote and the browser controller."""
import threading

import controller
import web
from common import load_config


def main():
    cfg = load_config()
    threading.Thread(target=web.serve, args=(cfg,), daemon=True).start()
    controller.run(cfg)  # Playwright must own the main thread


if __name__ == "__main__":
    main()
