from __future__ import annotations

import time
from decimal import Decimal


def sleep_decimal(seconds: Decimal) -> None:
    time.sleep(float(seconds))
