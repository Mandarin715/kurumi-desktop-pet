"""Small Windows MP3 player; one sound at a time, no extra codec bundle."""
import ctypes
import logging
from pathlib import Path
import random
import sys
import threading

SOUND_GROUPS = {
    'click': ('click_1',),
    'good': ('good_1', 'good_2'),
    'bad': ('bad_1', 'bad_2', 'bad_3'),
}
SOUND_NAMES = tuple(name for group in SOUND_GROUPS.values() for name in group)
UP_POSES = frozenset((1, 2, 5))
DOWN_POSES = frozenset((3, 7, 8, 9, 11, 13, 14))


def sound_candidates(name, external, bundled):
    if name not in SOUND_NAMES:
        return ()
    return tuple(dict.fromkeys((Path(external) / 'assets' / 'sfx' / f'{name}.mp3',
                                Path(bundled) / 'assets' / 'sfx' / f'{name}.mp3')))


class WindowsMP3Player:
    """MCI plays asynchronously; closing the alias interrupts the previous clip."""
    def __init__(self):
        self.alias = f'kurumi_sfx_{id(self):x}'
        self.opened = False
        self.api = None
        if sys.platform == 'win32':
            self.api = ctypes.WinDLL('winmm')
            self.api.mciSendStringW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p,
                                               ctypes.c_uint, ctypes.c_void_p]
            self.api.mciSendStringW.restype = ctypes.c_uint

    def command(self, command):
        if self.api is None:
            return False, ''
        output = ctypes.create_unicode_buffer(256)
        error = self.api.mciSendStringW(command, output, len(output), None)
        return error == 0, output.value

    def close(self):
        if self.opened:
            self.command(f'close {self.alias}')
            self.opened = False

    def open(self, path):
        self.close()
        self.opened, _ = self.command(f'open "{Path(path).resolve()}" type mpegvideo alias {self.alias}')
        return self.opened

    def play(self, path, volume):
        if not self.open(path):
            return False
        ok, _ = self.command(f'setaudio {self.alias} volume to {round(volume*1000)}')
        if ok:
            ok, _ = self.command(f'play {self.alias} from 0')
        if not ok:
            self.close()
        return ok

    def duration(self, path):
        """Decode headers without playing audio, for packaged-asset validation."""
        if not self.open(path):
            return None
        try:
            ok, _ = self.command(f'set {self.alias} time format milliseconds')
            if not ok:
                return None
            ok, value = self.command(f'status {self.alias} length')
            return int(value) if ok and value.isdecimal() else None
        finally:
            self.close()


class PetAudio:
    def __init__(self, external, bundled, settings, volume=.6, player=None):
        self.external, self.bundled, self.settings = external, bundled, settings
        self.volume = volume
        self.muted = settings.value('sound/muted', False, type=bool)
        self.async_mode = player is None
        self.player = player if player is not None else AsyncMP3Player()
        self.warned = set()

    def set_muted(self, muted):
        self.muted = bool(muted)
        if self.muted:
            self.player.close()
        self.settings.setValue('sound/muted', self.muted)
        self.settings.sync()

    def play(self, group, on_result=None):
        def report(played, status, name=None):
            if on_result:
                on_result({'audio_played': played, 'audio_status': status,
                           'audio_group': group, 'sound': name})
        if self.muted or self.volume <= 0 or group not in SOUND_GROUPS:
            report(False, 'muted' if self.muted else 'volume_zero' if self.volume <= 0 else 'no_effect')
            return False
        name = random.choice(SOUND_GROUPS[group])
        if self.async_mode:
            if on_result:
                report(None, 'queued', name)
                return self.player.enqueue(sound_candidates(name, self.external, self.bundled), self.volume,
                                           lambda played, status: report(played, status, name))
            return self.player.enqueue(sound_candidates(name, self.external, self.bundled), self.volume)
        for path in sound_candidates(name, self.external, self.bundled):
            if path.is_file() and self.player.play(path, self.volume):
                report(True, 'played', name)
                return True
        if name not in self.warned:
            logging.warning('无法播放音效 %s，桌宠继续运行', name)
            self.warned.add(name)
        report(False, 'failed', name)
        return False

    def fx_pose_changed(self, before, after, on_result=None):
        if before == after:
            if on_result:
                on_result({'audio_played': False, 'audio_status': 'same_pose', 'audio_group': None, 'sound': None})
            return False
        if after in UP_POSES:
            return self.play('good', on_result) if on_result else self.play('good')
        if after in DOWN_POSES:
            return self.play('bad', on_result) if on_result else self.play('bad')
        if on_result:
            on_result({'audio_played': False, 'audio_status': 'no_effect', 'audio_group': None, 'sound': None})
        return False

    def close(self):
        if self.async_mode:
            self.player.shutdown()
        else:
            self.player.close()


class AsyncMP3Player:
    """All MCI work stays off the GUI thread; latest request wins, aliases cached."""
    def __init__(self, factory=WindowsMP3Player):
        self.factory = factory
        self.condition = threading.Condition()
        self.pending = None
        self.generation = 0
        self.closed = False
        self.thread = None

    @staticmethod
    def report(callback, played, status):
        if callback:
            try:
                callback(played, status)
            except Exception:
                logging.warning('音效状态回报失败')

    def enqueue(self, paths, volume, callback=None):
        with self.condition:
            if self.closed:
                previous, accepted = None, False
            else:
                previous, accepted = self.pending, True
                self.generation += 1
                self.pending = (self.generation, tuple(paths), volume, callback)
                if self.thread is None:
                    self.thread = threading.Thread(target=self._run, name='KurumiAudio', daemon=True)
                    self.thread.start()
                self.condition.notify()
        if previous:
            self.report(previous[3], False, 'cancelled')
        if not accepted:
            self.report(callback, False, 'closed')
        return accepted

    def close(self):
        # A queued stop also invalidates an in-flight decoder open before playback.
        with self.condition:
            previous = self.pending
            self.generation += 1
            self.pending = (self.generation, (), 0, None)
            self.condition.notify()
        if previous:
            self.report(previous[3], False, 'cancelled')

    def shutdown(self):
        with self.condition:
            previous = self.pending
            self.closed = True
            self.generation += 1
            self.pending = None
            self.condition.notify()
        if previous:
            self.report(previous[3], False, 'cancelled')

    def _run(self):
        players, current, warned = {}, None, set()
        callback = None
        try:
            while True:
                with self.condition:
                    while self.pending is None and not self.closed:
                        self.condition.wait()
                    if self.closed:
                        return
                    generation, paths, volume, callback = self.pending
                    self.pending = None
                status = 'failed'
                if current is not None:
                    current.command(f'stop {current.alias}')
                    current = None
                for path in paths:
                    with self.condition:
                        if self.closed or generation != self.generation:
                            status = 'cancelled'
                            break
                    if not path.is_file():
                        continue
                    player = players.get(path)
                    if player is None:
                        player = self.factory()
                        if not player.open(path):
                            continue
                        players[path] = player
                    with self.condition:
                        cancelled = self.closed or generation != self.generation
                    if cancelled:
                        status = 'cancelled'
                        break
                    ok, _ = player.command(f'setaudio {player.alias} volume to {round(volume*1000)}')
                    if ok:
                        ok, _ = player.command(f'play {player.alias} from 0')
                    if ok:
                        current = player
                        status = 'played'
                        break
                else:
                    if paths and paths[0].stem not in warned:
                        logging.warning('音效无法播放，桌宠继续运行')
                        warned.add(paths[0].stem)
                self.report(callback, status == 'played', status)
                callback = None
        except Exception:
            self.report(callback, False, 'failed')
            logging.warning('后台音效不可用，桌宠继续运行')
        finally:
            for player in players.values():
                player.close()
