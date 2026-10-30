import logging
import sys

from .bot import BrainBot
from .config import Config


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config()
    cfg.validate()
    BrainBot(cfg).run()


if __name__ == "__main__":
    main()
