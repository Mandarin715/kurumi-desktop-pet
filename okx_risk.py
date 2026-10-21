"""Local observed equity baseline, UTC+8 day; not exchange daily realized PnL."""
from datetime import datetime, timedelta, timezone
import json
import math


class EquityTracker:
    def __init__(self, settings):
        self.settings = settings

    def observe(self, scope, equity, threshold, today=None):
        if not scope or equity is None or not math.isfinite(equity) or equity < 0:
            return None, False
        today = today or datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        key = 'fx/okx_equity/'+scope
        try:
            data = json.loads(self.settings.value(key, '{}'))
            first = data.get('first')
            if (data.get('date') != today or type(first) not in (int, float)
                    or not math.isfinite(first) or first <= 0):
                data = {'date': today, 'first': equity, 'warned': False}
        except (ValueError, TypeError, AttributeError):
            data = {'date': today, 'first': equity, 'warned': False}
        first = data['first']
        percent = round((equity/first-1)*100, 9) if first > 0 else None
        crossed = percent is not None and percent <= -threshold
        announce = crossed and not data.get('warned', False)
        data.update(last=equity, warned=bool(data.get('warned')) or announce)
        self.settings.setValue(key, json.dumps(data))
        self.settings.sync()
        return percent, announce
