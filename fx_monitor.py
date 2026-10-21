"""Opt-in rectangle capture and local OCR, with a single background inference."""
import ctypes
from datetime import date
import hashlib
import json
import logging
import math
import sys
import time
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from fx_logic import LABELS, PriceMoodEngine
from fx_ocr import image_rgb, read_price
from fx_region import load_region, resolve_region
from okx_api import OKXClient, load_credentials
from okx_risk import EquityTracker
from fx_debug import record as debug_record, stamp

OKX_FAILURE_LIMIT = 3
OKX_RETRY_SECONDS = 30.


class JobSignals(QObject):
    done = Signal(int, object)


class OCRJob(QRunnable):
    def __init__(self, generation, image, confidence, captured_at):
        super().__init__()
        self.generation, self.image = generation, image
        self.confidence, self.captured_at = confidence, captured_at
        self.signals = JobSignals()

    def run(self):
        price, error = None, False
        try:
            price = read_price(image_rgb(self.image), self.confidence)
        except Exception:
            logging.error('本地 OCR 运行失败，请检查模型和运行库')
            error = True
        self.image = None
        self.signals.done.emit(self.generation, (price, self.captured_at, error))


class OKXJob(QRunnable):
    method = 'poll_ticker'
    def __init__(self, generation, client):
        super().__init__()
        self.generation, self.client = generation, client
        self.signals = JobSignals()

    def run(self):
        snapshot = None
        try:
            snapshot = getattr(self.client, self.method)()
        except Exception:
            # No exception/response/header logging: a private request may contain keys.
            pass
        self.signals.done.emit(self.generation, snapshot)


class OKXAccountJob(OKXJob):
    method = 'poll_account'


class FXMonitor(QObject):
    reading = Signal(object)
    status = Signal(str)
    notice = Signal(str)
    source_changed = Signal(str)
    needs_region = Signal()
    permission_warning = Signal(str)

    def __init__(self, pet):
        super().__init__(pet)
        self.pet, self.policy = pet, pet.config.fx
        self.active, self.busy, self.generation = False, False, 0
        self.job, self.engine, self.last_price = None, None, None
        self.status_text = '已关闭'
        self.region = load_region(pet.settings)
        self.stored_region = load_region(pet.settings)
        self.source = pet.config.data_source
        self.preferred_source = self.source
        self.okx_failures = 0
        self.okx_retry_at = None
        self.okx_probe = False
        self.preserve_next_history = False
        self.history_scope = None
        self.client = None
        self.account_client = None
        self.account_busy, self.account_job = False, None
        self.account_context = None
        self.account_pool = QThreadPool(self)
        self.account_pool.setMaxThreadCount(1)
        self.account_status = ''
        self.warned_permissions = False
        self.position_breached = False
        self.sample_sequence = 0
        self.pose_sequence = 0
        self.pending_sample = None
        self.equity_tracker = EquityTracker(pet.settings)
        self.timer = QTimer(self)
        self.timer.setInterval(self.sampling_interval())
        self.timer.timeout.connect(self.capture)
        self.account_timer = QTimer(self)
        self.account_timer.setInterval(round(self.policy['account_interval_ms']))
        self.account_timer.timeout.connect(self.capture_account)
        self.rest_timer = QTimer(self)
        self.rest_timer.setInterval(1000)
        self.rest_timer.timeout.connect(self.check_rest)

    def set_status(self, text):
        self.status_text = text
        self.status.emit(text)

    def stop_loss(self):
        value = self.pet.settings.value('fx/stop_loss', self.policy['stop_loss'])
        try:
            number = float(value)
            return number if math.isfinite(number) and number > 0 else None
        except (TypeError, ValueError):
            return None

    def set_active(self, active, source_override=None, preserve_history=False):
        self.generation += 1
        self.active = active
        if active:
            self.refresh_region()
            self.source = source_override or self.pet.config.data_source
            self.preferred_source = ('okx' if self.pet.config.data_source == 'okx'
                                     else self.source)
            self.okx_failures = 0
            self.okx_retry_at = (time.monotonic()+OKX_RETRY_SECONDS
                                 if self.source == 'ocr' and self.preferred_source == 'okx' else None)
            self.source_changed.emit(self.source)
            self.client = None
            self.account_client = None
            self.account_status = ''
            self.warned_permissions = False
            self.position_breached = False
            # OCR cannot identify its instrument. Even matching price scales
            # are insufficient evidence to reuse quotes across source changes.
            keep_quotes = (preserve_history and self.engine is not None
                           and self.source == 'okx' and self.history_scope == self.price_scope())
            if not keep_quotes:
                self.reset_price_window(preserve_session=preserve_history)
            self.preserve_next_history = keep_quotes
            self.engine.stop_loss = self.stop_loss()
            self.engine.set_account_risk(None)
            self.engine.account_notice = None
            self.apply_capture_policy()
            name = 'OKX' if self.source == 'okx' else '本地 OCR'
            self.set_status(f'等待{name} · 至少积累{self.policy["warmup_seconds"]:g}秒、{self.policy["warmup_samples"]}个价格')
            if self.source == 'okx':
                try:
                    self.ensure_okx_client()
                except Exception:
                    self.set_status('OKX 本地配置暂时无法读取，将在采样时重试')
            self.timer.start(self.sampling_interval())
            self.account_timer.start()
            self.rest_timer.start()
            QTimer.singleShot(250, self.capture)
            QTimer.singleShot(500, self.capture_account)
        else:
            self.timer.stop()
            self.account_timer.stop()
            self.rest_timer.stop()
            self.set_status('已关闭')
            self.capture_affinity(0)
            self.client = None
            self.account_client = None
        return self.summary()

    def apply_capture_policy(self):
        excluded = self.policy['exclude_from_capture']
        return self.capture_affinity(0x11 if excluded else 0) and excluded

    def sample_context(self):
        self.sample_sequence += 1
        return {'sample_id': self.sample_sequence, 'generation': self.generation,
                'region_key': self.ocr_day_key(),
                'price_scope': self.price_scope(),
                'source': self.source, 'sample_time': stamp(), 'started_s': time.monotonic()}

    def log_sample(self, context, price=None, skipped=False, reason='accepted', **details):
        if not self.policy['debug_log']:
            return
        context = context or self.sample_context()  # Also supports direct synthetic-result tests.
        engine = self.engine if context['generation'] == self.generation else None
        debug_record(True, 'sample', sample_id=context['sample_id'],
                     generation=context['generation'], source=context['source'],
                     sample_time=context['sample_time'],
                     elapsed_ms=round((time.monotonic()-context['started_s'])*1000, 3),
                     price=price, percent=engine.percent if engine else None,
                     long_percent=engine.long_percent if engine else None,
                     fast_window_seconds=self.policy['fast_window_seconds'],
                     span_seconds=round(engine.span_seconds, 3) if engine else 0.,
                     warming=not engine.fast_complete if engine else True,
                     window_seconds=self.policy['window_seconds'],
                     mood=engine.mood if engine else 'waiting',
                     tier=engine.tier() if engine else 0,
                     skipped=skipped, reason=reason, **details)

    def pose_audio_reporter(self, before, after, update):
        if not self.policy['debug_log'] or before == after:
            return None
        self.pose_sequence += 1
        # Freeze metadata before the audio worker runs or the data source changes.
        metadata = {'pose_id': self.pose_sequence, 'source': self.source,
                    'from_pose': before, 'pose': after, 'mood': update.mood,
                    'tier': update.tier, 'percent': update.percent,
                    'long_percent': update.long_percent, 'warming': update.warming,
                    'span_seconds': round(update.span_seconds, 3)}
        reported = False

        def report(audio):
            nonlocal reported
            event = 'audio_result' if reported else 'pose'
            reported = True
            debug_record(True, event, **metadata, **audio)
        return report

    def capture_affinity(self, value):
        if sys.platform == 'win32':
            try:
                user = ctypes.WinDLL('user32', use_last_error=True)
                user.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                return bool(user.SetWindowDisplayAffinity(int(self.pet.winId()), value))
            except (OSError, AttributeError):
                pass
        return False

    def capture(self):
        if not self.active:
            return
        self.refresh_region()
        context = self.sample_context()
        reason = ('busy' if self.busy else 'dragging' if self.pet.dragging
                  else 'menu_open' if self.pet.menu.isVisible()
                  else 'settings_open' if QApplication.activeModalWidget() is not None else None)
        if reason:
            self.log_sample(context, skipped=True, reason=reason)
            return
        retry = (self.source == 'ocr' and self.preferred_source == 'okx'
                 and self.okx_retry_at is not None and time.monotonic() >= self.okx_retry_at)
        if self.source == 'okx' or retry:
            context['source'] = 'okx'
            context['price_scope'] = self.price_scope('okx')
            self.okx_probe = retry
            self.busy = True
            self.pending_sample = context
            try:
                self.ensure_okx_client()
            except Exception:
                self.on_okx_result(self.generation, None)
                return
            self.job = OKXJob(self.generation, self.client)
            self.job.signals.done.connect(self.on_okx_result)
            QThreadPool.globalInstance().start(self.job)
            return
        resolved = resolve_region(self.region)
        if resolved is None:
            self.set_status('区域已失效，请在 FX设置 中重新框选')
            self.log_sample(context, skipped=True, reason='invalid_region')
            return
        screen, rect = resolved
        if not self.apply_capture_policy() and self.pet.geometry().intersects(rect.translated(screen.geometry().topLeft())):
            self.set_status('桌宠挡住价格，请拖开')
            self.log_sample(context, skipped=True, reason='pet_occludes_region')
            return
        try:
            pixmap = screen.grabWindow(0, rect.x(), rect.y(), rect.width(), rect.height())
        except RuntimeError:
            self.set_status('本次无法读取价格区域，已跳过')
            self.log_sample(context, skipped=True, reason='capture_error')
            return
        if pixmap.isNull():
            self.set_status('本次无法读取价格区域，已跳过')
            self.log_sample(context, skipped=True, reason='capture_empty')
            return
        image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGB888).copy()
        self.busy = True
        self.pending_sample = context
        self.job = OCRJob(self.generation, image, self.policy['ocr_confidence'], time.monotonic())
        self.job.signals.done.connect(self.on_result)
        QThreadPool.globalInstance().start(self.job)

    @Slot(int, object)
    def on_result(self, generation, result):
        context, self.pending_sample = self.pending_sample, None
        self.busy, self.job = False, None
        if not self.active or generation != self.generation or self.source != 'ocr':
            self.log_sample(context, price=result[0], skipped=True, reason='stale_result')
            return
        self.refresh_region()
        if context and context.get('region_key') != self.ocr_day_key():
            self.log_sample(context, price=result[0], skipped=True, reason='region_changed')
            return
        price, captured_at, error = result
        if price is None:
            self.set_status('本地 OCR 加载失败，请检查安装' if error else '本次未读到可信的单个价格，已跳过')
            self.log_sample(context, skipped=True, reason='ocr_error' if error else 'ocr_unreadable')
            return
        self.ensure_history_scope()
        if self.record_price(price):
            self.reset_price_window(preserve_session=True)
        self.last_price = price
        update = self.receive_price(price, captured_at)
        self.log_sample(context, price=price)
        self.publish(update)

    @Slot(int, object)
    def on_okx_result(self, generation, snapshot):
        context, self.pending_sample = self.pending_sample, None
        self.busy, self.job = False, None
        probe, self.okx_probe = self.okx_probe, False
        if not self.active or generation != self.generation or (self.source != 'okx' and not probe):
            self.log_sample(context, price=snapshot.price if snapshot else None,
                            skipped=True, reason='stale_result')
            return
        if context and context.get('price_scope') != self.price_scope('okx'):
            self.log_sample(context, price=snapshot.price if snapshot else None,
                            skipped=True, reason='stale_result')
            return
        if snapshot is None:
            self.okx_failures += 1
            if probe:
                self.okx_retry_at = time.monotonic()+OKX_RETRY_SECONDS
                self.log_sample(context, skipped=True, reason='okx_retry_failed',
                                consecutive_failures=self.okx_failures, retry_seconds=OKX_RETRY_SECONDS)
                QTimer.singleShot(0, self.capture)
            elif self.okx_failures >= OKX_FAILURE_LIMIT:
                self.log_sample(context, skipped=True, reason='okx_error_fallback',
                                consecutive_failures=self.okx_failures, retry_seconds=OKX_RETRY_SECONDS)
                self.fallback_to_ocr()
            else:
                # Same API instrument, no OCR boundary crossed. Preserve the
                # window on recovery even if two slow attempts exceed gap limit.
                self.preserve_next_history = True
                self.log_sample(context, skipped=True, reason='okx_error_retry',
                                consecutive_failures=self.okx_failures)
                self.set_status(f'OKX 本次读取失败（{self.okx_failures}/{OKX_FAILURE_LIMIT}），下次采样重试')
            return
        self.okx_failures = 0
        if probe:
            self.change_source('okx')
            self.okx_retry_at = None
            debug_record(self.policy['debug_log'], 'okx_recovered')
        self.ensure_history_scope()
        # Legacy combined fixtures remain supported; public ticks never clear
        # the most recent independently observed account risk.
        if snapshot.private_enabled or snapshot.permission_warning:
            self.apply_account_snapshot(snapshot)
        self.last_price = snapshot.price
        self.record_price(snapshot.price)
        update = self.receive_price(snapshot.price, time.monotonic())
        self.log_sample(context, price=snapshot.price)
        self.publish(update)

    def capture_account(self):
        if (not self.active or self.source != 'okx' or self.account_busy
                or self.pet.menu.isVisible() or QApplication.activeModalWidget() is not None):
            return
        try:
            if self.account_client is None:
                credentials = load_credentials(self.pet.external)
                if not credentials.present:
                    return
                self.account_client = OKXClient(self.pet.config.okx, credentials)
        except ValueError:
            self.account_status = '本地凭据无法读取，请检查 FX设置'
            return
        self.account_context = (self.price_scope(), self.engine)
        self.account_busy = True
        self.account_job = OKXAccountJob(self.generation, self.account_client)
        self.account_job.signals.done.connect(self.on_account_result)
        self.account_pool.start(self.account_job)

    @Slot(int, object)
    def on_account_result(self, generation, snapshot):
        scope, self.account_context = self.account_context, None
        self.account_busy, self.account_job = False, None
        if (not self.active or generation != self.generation or self.source != 'okx'
                or scope != (self.price_scope(), self.engine)):
            return
        if snapshot is None:
            self.account_status = '账户暂时未更新，下次重试（行情独立读取）'
            debug_record(self.policy['debug_log'], 'account', skipped=True, reason='account_error')
            return
        self.apply_account_snapshot(snapshot)
        debug_record(self.policy['debug_log'], 'account', skipped=False, reason='accepted')
        self.publish(self.engine.advance(time.monotonic(), urgent=bool(self.engine.account_notice)))

    def apply_account_snapshot(self, snapshot):
        if snapshot.permission_warning and not self.warned_permissions:
            self.warned_permissions = True
            self.permission_warning.emit(snapshot.permission_warning)
        risk, text = None, None
        self.account_status = ''
        if snapshot.private_enabled:
            self.engine.stop_loss = None  # Account risk replaces the hand-entered price.
            options = self.pet.config.okx
            daily, announce_daily = self.equity_tracker.observe(snapshot.account_scope, snapshot.equity,
                                                                options['daily_loss_percent'])
            breached = snapshot.worst_upl_percent is not None and snapshot.worst_upl_percent <= -options['position_stop_loss_percent']
            if daily is not None and daily <= -options['daily_loss_percent']:
                risk = 'daily'
                if announce_daily:
                    text = '今天观测到的权益亏损超线了……今天先停手，休息一下吧。'
            elif breached:
                risk = 'position'
                if not self.position_breached:
                    text = '该止损了……真的要止损吗？'
            self.position_breached = breached
            parts = []
            if snapshot.worst_upl_percent is not None:
                parts.append(f'最低持仓浮盈亏 {snapshot.worst_upl_percent:+.2f}%')
            if snapshot.account_upl_usd is not None:
                parts.append(f'跨仓浮盈亏 {snapshot.account_upl_usd:+.2f} USD')
            if daily is not None:
                parts.append(f'今日观测权益 {daily:+.2f}%')
            self.account_status = ' · '.join(parts)
        self.engine.set_account_risk(risk, text)
        if risk:
            self.engine.mood = 'panic'
        else:
            self.engine.stop_loss = self.stop_loss() if not snapshot.private_enabled else None

    def fallback_to_ocr(self):
        self.change_source('ocr')
        self.account_status = ''
        self.okx_retry_at = time.monotonic()+OKX_RETRY_SECONDS
        self.engine.stop_loss = self.stop_loss()
        self.engine.set_account_risk(None)
        self.engine.account_notice = None
        self.notice.emit('欸？行情连不上了……')
        self.set_status('OKX 连续读取失败，已回退截图识别；重新积累窗口，每30秒重试连接')
        # Keep polling even without a saved rectangle; opening a blocking region
        # selector here would prevent automatic API recovery until user input.
        QTimer.singleShot(250, self.capture)

    def receive_price(self, price, now):
        self.ensure_history_scope()
        preserve = self.preserve_next_history and self.source == 'okx'
        self.preserve_next_history = False
        return self.engine.receive(price, now, preserve_gap=preserve)

    def ensure_okx_client(self):
        if self.client is None:
            self.client = OKXClient(self.pet.config.okx)

    def sampling_interval(self, source=None):
        key = 'okx_interval_ms' if (source or self.source) == 'okx' else 'interval_ms'
        return round(self.policy[key])

    def reset_price_window(self, preserve_session=False):
        previous = self.engine
        policy = dict(self.policy, interval_ms=self.sampling_interval())
        self.engine = PriceMoodEngine(policy, self.pet.current, time.monotonic(), self.stop_loss())
        self.history_scope = self.price_scope()
        self.last_price = None
        self.preserve_next_history = False
        if preserve_session and previous:
            self.engine.started = previous.started
            self.engine.last_pose_at = previous.last_pose_at
            self.engine.rest_done = previous.rest_done

    def price_scope(self, source=None):
        if (source or self.source) == 'okx':
            options = self.pet.config.okx
            return ('okx', options['inst_id'], options['simulated'])
        return ('ocr', self.ocr_day_key())

    def ensure_history_scope(self):
        if self.history_scope != self.price_scope():
            self.reset_price_window(preserve_session=True)

    def change_source(self, source):
        if source != self.source:
            self.source = source
            self.timer.setInterval(self.sampling_interval())
            self.reset_price_window(preserve_session=True)
            self.source_changed.emit(source)

    def refresh_region(self):
        # QSettings caches registry values; sync also notices edits outside the UI.
        self.pet.settings.sync()
        stored = load_region(self.pet.settings)
        if stored != self.stored_region:
            self.region, self.stored_region = stored, stored
            if self.engine and self.source == 'ocr':
                self.reset_price_window(preserve_session=True)

    def set_region(self, data):
        # Explicit re-selection (even the same rectangle) starts a fresh OCR day.
        self.pet.settings.remove(self.ocr_day_key())
        self.region, self.stored_region = data, data
        self.pet.settings.setValue('fx/region', json.dumps(data))
        self.pet.settings.remove(self.ocr_day_key())
        self.pet.settings.remove('fx/day')  # Retire the old all-instruments baseline.
        self.pet.settings.sync()

    def publish(self, update):
        span = update.span_seconds if update.warming else self.policy['fast_window_seconds']
        percent = (f'{"预热·实际" if update.warming else "近"}{span:g}秒 {update.percent:+.2f}%'
                   if update.percent is not None else '积累窗口数据')
        if update.long_percent is not None:
            percent += f' · 近{self.policy["window_seconds"]:g}秒 {update.long_percent:+.2f}%'
        price = f'{self.last_price:g}' if self.last_price is not None else '—'
        source = 'OKX '+self.pet.config.okx['inst_id'] if self.source == 'okx' else '截图'
        suffix = ' · '+self.account_status if self.account_status else ''
        self.set_status(f'{source} · {price} · {percent} · {LABELS[update.mood]} · 情绪{update.emotion:+d}'+suffix)
        if update.pose is not None or update.text is not None:
            self.reading.emit(update)

    def check_rest(self):
        if not self.active:
            return
        now = time.monotonic()
        if not self.engine.rest_done and now-self.engine.started >= self.policy['rest_after_seconds']:
            self.publish(self.engine.advance(now))

    def manual(self):
        if self.active:
            self.publish(self.engine.manual(time.monotonic()))

    def record_price(self, price):
        if self.source == 'ocr':
            self.refresh_region()
        today = date.today().isoformat()
        try:
            stats = json.loads(self.pet.settings.value(self.day_key(), '{}'))
        except (TypeError, ValueError):
            stats = {}
        valid = (isinstance(stats, dict) and stats.get('date') == today
                 and self.valid_price(stats.get('first')) and self.valid_price(stats.get('last')))
        # The same on-screen rectangle can show a different symbol without
        # moving. Never carry a 151-priced test fixture into an 82650 BTC day.
        references = [stats['first'], stats['last']] if valid else []
        if self.engine and self.engine.history:
            references.append(self.engine.history[-1][1])
        discontinuity = (self.source == 'ocr'
                         and any(abs(price/previous-1) > .5 for previous in references))
        if not valid or discontinuity:
            stats = {'date': today, 'first': price}
        stats['last'] = price
        self.pet.settings.setValue(self.day_key(), json.dumps(stats))
        self.pet.settings.sync()
        if discontinuity:
            debug_record(self.policy['debug_log'], 'day_baseline_reset', reason='price_scale_changed', price=price)
        return discontinuity

    def summary(self):
        if self.source == 'ocr':
            self.refresh_region()
        try:
            stats = json.loads(self.pet.settings.value(self.day_key(), '{}'))
            if (stats['date'] == date.today().isoformat()
                    and self.valid_price(stats['first']) and self.valid_price(stats['last'])):
                change = (stats['last']/stats['first']-1)*100
                direction = '上涨' if change > 0 else '下跌' if change < 0 else '持平'
                return f'今天盯盘{direction}{abs(change):.2f}%，以首次读到的价格为基准～'
        except (TypeError, ValueError, KeyError):
            pass
        return '今天还没有读到有效价格，先休息一下吧～'

    def day_key(self):
        if self.source == 'okx':
            mode = 'demo' if self.pet.config.okx['simulated'] else 'live'
            return 'fx/okx_price/'+mode+'/'+self.pet.config.okx['inst_id']
        return self.ocr_day_key()

    def ocr_day_key(self):
        region = json.dumps(self.region, sort_keys=True, separators=(',', ':'))
        return 'fx/ocr_price/'+hashlib.sha256(region.encode()).hexdigest()[:20]

    @staticmethod
    def valid_price(value):
        return type(value) in (int, float) and math.isfinite(value) and value > 0
