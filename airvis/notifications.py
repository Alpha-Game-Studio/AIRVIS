from __future__ import annotations

import logging

log = logging.getLogger("airvis.notifications")


def notify(message: str) -> None:
    log.info("AIRVIS notification: %s", message)
