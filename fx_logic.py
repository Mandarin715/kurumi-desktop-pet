"""Price history and expression policy; no screen, network or GUI dependencies."""
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import math
import random


DEFAULT_FX = {
    'interval_ms': 3000, 'okx_interval_ms': 2000, 'account_interval_ms': 5000,
    'window_seconds': 120, 'fast_window_seconds': 30, 'hold_seconds': 10,
    'warmup_seconds': 10, 'warmup_samples': 4, 'confirm_samples': 2,
    'flat_percent': .08, 'flat_exit_percent': .04,
    'strong_percent': .3, 'strong_exit_percent': .2, 'panic_drop_percent': 3.,
    'flat_sleep_seconds': 300,
    'rest_after_seconds': 3600, 'max_sample_gap_seconds': 30,
    'emotion_seconds': 20, 'ocr_confidence': .90, 'stop_loss': None,
    'exclude_from_capture': True, 'debug_log': False,
    'poses': {'up_strong': [1, 5], 'up': [2], 'flat': [6], 'sleep': [4],
              'down': [11, 7], 'down_strong': [3, 9], 'panic': [13, 8, 14]},
    'lines_intense': {
        '1': ['连着涨！FX也太简单了吧～♪'], '2': ['又涨了！冷静、冷静……我快飘起来了！'],
        '3': ['怎么还在跌啊啊啊！'], '4': ['行情不动，我先抱着枕头歇一会儿……'],
        '5': ['啊哈哈哈！连续上涨，笑意完全憋不住了～'],
        '6': ['怎么还是不动……'], '7': ['连续下跌……神啊，再救我一次吧！'],
        '8': ['一直在跌……早知道就止损了呜呜！'],
        '9': ['结界也挡不住了吗！别再跌了啊～！'],
        '10': ['先冷静，深呼吸……'], '11': ['又跌了？我真的看不懂了……'],
        '12': ['不能再冲动了……'], '13': ['连续下跌！保证金要撑不住了啊啊啊！'],
        '14': ['利润全融化了……我快崩溃了啦！']},
}
LABELS = {'up_strong': '大涨', 'up': '上涨', 'flat': '横盘', 'sleep': '横盘犯困',
          'down': '下跌', 'down_strong': '大跌', 'panic': '慌张', 'waiting': '等待窗口数据'}

SENSITIVITY_PRESETS = {
    'forex_stock': ('外汇股票', .05, .20, 2.),
    'default': ('默认', .08, .30, 3.),
    'crypto': ('加密货币', .15, .60, 5.),
}
SENSITIVITY_KEYS = ('flat_percent', 'strong_percent', 'panic_drop_percent',
                    'flat_exit_percent', 'strong_exit_percent')


def sensitivity_values(flat, strong, panic):
    values = (flat, strong, panic)
    if any(type(n) not in (int, float) or not math.isfinite(n) or n <= 0 for n in values):
        raise ValueError('三个阈值必须是大于 0 的有限数字')
    if not flat < strong < panic:
        raise ValueError('阈值必须满足：横盘 < 强波动 < 恐慌跌幅')
    strong_exit = strong * (2 / 3)
    if strong_exit <= flat:
        strong_exit = (flat + strong) / 2
    return dict(zip(SENSITIVITY_KEYS, (flat, strong, panic, flat / 2, strong_exit)))


def sensitivity_name(policy):
    # Numeric settings remain authoritative when a user edits JSON by hand.
    for name, (_, flat, strong, panic) in SENSITIVITY_PRESETS.items():
        expected = sensitivity_values(flat, strong, panic)
        if all(math.isclose(policy[key], value, rel_tol=1e-9, abs_tol=1e-12)
               for key, value in expected.items()):
            return name
    return 'custom'


def validated_policy(data=None):
    result = deepcopy(DEFAULT_FX)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError('fx 必须是对象')
    for key in DEFAULT_FX:
        if key in data:
            if isinstance(result[key], dict):
                if not isinstance(data[key], dict):
                    raise ValueError(f'fx.{key} 必须是对象')
                result[key].update(data[key])
            else:
                result[key] = data[key]
    for key, value in result.items():
        if key in ('exclude_from_capture', 'debug_log'):
            if type(value) is not bool:
                raise ValueError(f'fx.{key} 必须是布尔值')
            continue
        if key in ('poses', 'lines_intense', 'stop_loss'):
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'fx.{key} 必须是正数')
    if not .5 <= result['ocr_confidence'] <= 1:
        raise ValueError('OCR 置信度必须为 0.5～1')
    if not result['flat_percent'] < result['strong_percent'] < result['panic_drop_percent']:
        raise ValueError('涨跌阈值必须递增')
    if not (result['flat_exit_percent'] < result['flat_percent']
            <= result['strong_exit_percent'] < result['strong_percent']):
        raise ValueError('退出阈值必须低于进入阈值')
    if not result['warmup_seconds'] <= result['fast_window_seconds'] <= result['window_seconds']:
        raise ValueError('预热、短窗口、长窗口时间必须递增')
    for key in ('warmup_samples', 'confirm_samples'):
        if type(result[key]) is not int or result[key] < 2:
            raise ValueError('采样确认次数必须为至少 2 的整数')
    if result['max_sample_gap_seconds'] < max(result['interval_ms'], result['okx_interval_ms'])/1000:
        raise ValueError('容许采样间隔不能短于轮询间隔')
    if result['stop_loss'] is not None and (type(result['stop_loss']) not in (int, float)
            or not math.isfinite(result['stop_loss']) or result['stop_loss'] <= 0):
        raise ValueError('止损价必须为正数或 null')
    for mood in DEFAULT_FX['poses']:
        pool = result['poses'][mood]
        if not isinstance(pool, list) or not pool or any(type(n) is not int or not 1 <= n <= 14 for n in pool):
            raise ValueError('姿势编号必须为 1～14')
        result['poses'][mood] = list(dict.fromkeys(pool))
    for n in range(1, 15):
        lines = result['lines_intense'][str(n)]
        if not isinstance(lines, list) or not lines or any(not isinstance(s, str) or not s.strip() for s in lines):
            raise ValueError('高情绪台词必须为非空字符串数组')
    result['sensitivity_preset'] = sensitivity_name(result)
    return result


@dataclass
class MoodUpdate:
    pose: int | None = None
    tier: int = 0
    text: str | None = None
    mood: str = 'waiting'
    percent: float | None = None
    emotion: int = 0
    long_percent: float | None = None
    warming: bool = True
    span_seconds: float = 0.


class PriceMoodEngine:
    def __init__(self, policy, current=10, now=0., stop_loss=None):
        self.policy, self.current, self.started = policy, current, now
        self.stop_loss = stop_loss
        self.history = deque()
        self.mood, self.percent, self.emotion = 'waiting', None, 0
        self.long_percent, self.span_seconds = None, 0.
        self.fast_complete = False
        self.pending_mood, self.pending_count = None, 0
        self.direction_since, self.direction = None, 0
        self.flat_since = None
        self.last_pose_at, self.last_pose_percent = now-policy['hold_seconds'], None
        self.applied = ('waiting', 0)
        self.below_stop = False
        self.stop_pending = False
        self.rest_done, self.rest_pending = False, False
        self.account_risk = None
        self.account_notice = None
        self.risk_pending = False

    def apply_policy(self, policy, now):
        """Reconfigure without replaying samples or reusing old confirmations."""
        self.policy = validated_policy(policy)
        self.pending_mood, self.pending_count = None, 0
        # Cached returns describe real samples; do not append/replay the last
        # quote to manufacture a confirmation when the user presses Save.
        panic = any(value is not None and value < -self.policy['panic_drop_percent']
                    for value in (self.percent, self.long_percent))
        if panic or self.below_stop or self.account_risk:
            urgent = self.mood != 'panic'
            self.mood = 'panic'
            return self.advance(now, urgent=urgent)
        candidate = self.classify()
        if candidate not in ('flat', 'sleep'):
            self.flat_since = None
        return self.result()

    def set_account_risk(self, kind, text=None):
        if kind and kind != self.account_risk:
            self.risk_pending = True
        elif not kind:
            self.risk_pending = False
        self.account_risk = kind
        if text:
            self.account_notice = text

    def tier(self):
        return int(bool(self.direction and abs(self.emotion) >= 10 and self.long_percent is not None
                        and self.long_percent*self.direction > self.policy['flat_exit_percent']))

    def window_return(self, price, now, seconds, partial=False):
        tolerance = self.policy['interval_ms']/2000
        anchor = min(self.history, key=lambda sample: abs(sample[0]-(now-seconds)))
        span = now-anchor[0]
        complete = abs(span-seconds) <= tolerance
        if not complete and partial:
            # Before a complete window exists, compare with the oldest observed
            # quote. Never extrapolate a ten-second move to a thirty-second move.
            anchor = self.history[0] if now-self.history[0][0] < seconds else anchor
            span = now-anchor[0]
        valid = complete or partial and span >= self.policy['warmup_seconds']
        return (round((price/anchor[1]-1)*100, 9) if valid else None), span, complete

    def classify(self):
        value, p = self.percent, self.policy
        if value is None:
            return 'waiting'
        sign = 1 if value >= 0 else -1
        magnitude = abs(value)
        old_direction = 1 if self.mood.startswith('up') else -1 if self.mood.startswith('down') else 0
        same = sign == old_direction
        strong = p['strong_exit_percent'] if same and self.mood.endswith('_strong') else p['strong_percent']
        ordinary = p['flat_exit_percent'] if same else p['flat_percent']
        if self.fast_complete and magnitude >= strong:
            return 'up_strong' if sign > 0 else 'down_strong'
        if magnitude >= ordinary:
            return 'up' if sign > 0 else 'down'
        return 'flat'

    def receive(self, price, now, preserve_gap=False):
        if not math.isfinite(price) or price <= 0:
            return self.result()
        p = self.policy
        if self.history and now <= self.history[-1][0]:
            return self.result()  # Duplicate/out-of-order quotes cannot confirm a mood.
        if self.history and not preserve_gap and now-self.history[-1][0] > p['max_sample_gap_seconds']:
            self.history.clear()
            self.flat_since, self.emotion = None, 0
            self.pending_mood, self.pending_count = None, 0
            self.direction_since, self.direction = None, 0
            self.mood = 'waiting'
        self.history.append((now, price))
        # A nominal 120s-old quote may be 119.99s old due to timer jitter.
        # Use half a polling interval for both pruning and warmup, so choosing
        # that quote does not alternate between a valid window and waiting.
        tolerance = p['interval_ms']/2000
        cutoff = now-p['window_seconds']+tolerance
        while len(self.history) > 1 and self.history[1][0] <= cutoff:
            self.history.popleft()
        enough = len(self.history) >= p['warmup_samples']
        self.percent, self.span_seconds, self.fast_complete = self.window_return(
            price, now, p['fast_window_seconds'], partial=True)
        self.long_percent, _, _ = self.window_return(price, now, p['window_seconds'])
        if not enough:
            self.percent = self.long_percent = None
        below = self.stop_loss is not None and price < self.stop_loss
        crossed = below and not self.below_stop
        self.below_stop = below
        self.stop_pending = self.stop_pending or crossed
        previous_mood = self.mood
        panic = any(value is not None and value < -p['panic_drop_percent']
                    for value in (self.percent, self.long_percent))
        if below or self.account_risk or panic:
            self.mood = 'panic'
            self.pending_mood, self.pending_count = None, 0
        else:
            candidate = self.classify()
            if candidate == 'flat' and self.flat_since is not None and now-self.flat_since >= p['flat_sleep_seconds']:
                candidate = 'sleep'
            if candidate == 'waiting':
                self.mood = 'waiting'
                self.pending_mood, self.pending_count = None, 0
            elif candidate == self.mood:
                self.pending_mood, self.pending_count = None, 0
            else:
                self.pending_count = self.pending_count+1 if candidate == self.pending_mood else 1
                self.pending_mood = candidate
                if self.pending_count >= p['confirm_samples']:
                    self.mood = candidate
                    self.pending_mood, self.pending_count = None, 0
        if self.mood in ('flat', 'sleep'):
            if self.flat_since is None:
                self.flat_since = now
        else:
            self.flat_since = None
        direction = 1 if self.mood.startswith('up') else -1 if self.mood in ('down', 'down_strong', 'panic') else 0
        if (direction and self.percent is not None and not self.account_risk
                and self.percent*direction < p['flat_exit_percent']):
            direction = 0
        if direction != self.direction or not direction:
            self.direction_since = now if direction else None
        self.direction = direction
        elapsed = now-self.direction_since if self.direction_since is not None else 0.
        self.emotion = direction*min(10, int(elapsed/p['emotion_seconds']*10))
        return self.advance(now, urgent=crossed or self.mood == 'panic' and previous_mood != 'panic')

    def advance(self, now, urgent=False):
        p = self.policy
        if self.account_risk:
            self.mood = 'panic'
        urgent = urgent or self.risk_pending
        if not self.rest_done and now-self.started >= p['rest_after_seconds']:
            self.rest_pending = True
        allowed = now-self.last_pose_at >= p['hold_seconds'] or urgent
        if not allowed:
            return self.result()
        self.risk_pending = False
        if self.account_notice:
            text, self.account_notice = self.account_notice, None
            return self.choose('panic', now, text=text, force=True)
        if self.stop_pending:
            self.stop_pending = False
            return self.choose('panic', now, text='该止损了……真的要止损吗？', force=True)
        if self.rest_pending:
            self.rest_pending, self.rest_done = False, True
            return self.choose('sleep', now, text='休息一下吧', force=True)
        if self.mood != 'waiting' and (self.applied != (self.mood, self.tier())
                                      or self.current not in p['poses'][self.mood]):
            return self.choose(self.mood, now)
        return self.result()

    def choose(self, mood, now, text=None, force=False):
        allowed = [8, 14] if mood == 'panic' and self.account_risk == 'daily' else self.policy['poses'][mood]
        pool = [n for n in allowed if n != self.current]
        tier = self.tier()
        self.applied = (mood, tier)
        if not pool:
            if force:
                self.last_pose_at = now
                return self.result(text=text)
            return self.result()
        self.current = random.choice(pool)
        self.last_pose_at, self.last_pose_percent = now, self.percent
        return self.result(pose=self.current, text=text)

    def manual(self, now):
        if self.mood == 'waiting':
            return self.result()
        return self.choose(self.mood, now)

    def result(self, pose=None, text=None):
        return MoodUpdate(pose, self.tier(), text, self.mood, self.percent, self.emotion,
                          self.long_percent, not self.fast_complete, self.span_seconds)
