"""Sound event rules and Windows decoding; no audible playback during tests."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import threading
import time
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pet_audio import (DEFAULT_VOLUME, AsyncMP3Player, PetAudio, SOUND_GROUPS, SOUND_NAMES,
                       WindowsMP3Player, sound_candidates)
from pet_core import load_config

ROOT = Path(__file__).resolve().parents[1]


class Settings:
    def __init__(self):
        self.data = {}
    def value(self, key, default=None, type=None):
        return self.data.get(key, default)
    def setValue(self, key, value):
        self.data[key] = value
    def sync(self):
        pass


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.player = Mock()
        self.player.play.return_value = True
        self.settings = Settings()
        self.audio = PetAudio(ROOT, ROOT, self.settings, .35, self.player)

    def test_only_requested_files_and_group_randomization(self):
        self.assertEqual(set(p.stem for p in (ROOT/'assets'/'sfx').iterdir()), set(SOUND_NAMES))
        self.assertTrue((ROOT/'LICENSES.txt').is_file())
        for group, names in SOUND_GROUPS.items():
            for name in names:
                self.player.reset_mock()
                with patch('pet_audio.random.choice', return_value=name) as choose:
                    self.assertTrue(self.audio.play(group))
                    choose.assert_called_once_with(names)
                self.player.play.assert_called_once_with(ROOT/'assets'/'sfx'/f'{name}.mp3', .35)

    def test_mute_zero_volume_and_immediate_stop(self):
        self.audio.set_muted(True)
        self.player.close.assert_called_once()
        self.assertFalse(self.audio.play('click'))
        self.player.play.assert_not_called()
        other = PetAudio(ROOT, ROOT, self.settings, .35, self.player)
        self.assertTrue(other.muted)
        self.audio.set_muted(False)
        self.audio.volume = 0
        self.assertFalse(self.audio.play('bad'))
        self.player.play.assert_not_called()

    def test_live_volume_updates_backend_and_preserves_mute(self):
        self.audio.set_muted(True)
        self.audio.set_volume(.2)
        self.assertTrue(self.audio.muted)
        self.player.set_volume.assert_called_with(.2)
        self.assertFalse(self.audio.play('good'))
        self.audio.set_muted(False)
        self.assertTrue(self.audio.play('good'))
        self.assertEqual(self.player.play.call_args.args[1], .2)
        for value in (-1, 2, True, float('nan'), 'loud'):
            with self.assertRaises(ValueError):
                self.audio.set_volume(value)
        self.assertEqual(self.audio.volume, .2)

    def test_background_volume_changes_current_clip_without_restarting(self):
        playing, changed, stopped = threading.Event(), threading.Event(), threading.Event()
        commands, volumes = [], []
        class Player:
            alias = 'fake'
            def open(self, path): return True
            def command(self, command):
                commands.append(command)
                if command.startswith('play'): playing.set()
                if command.startswith('stop'): stopped.set()
                return True, ''
            def set_volume(self, volume): volumes.append(volume); changed.set()
            def close(self): pass
        player = AsyncMP3Player(Player)
        try:
            player.enqueue([ROOT/'assets'/'sfx'/'good_1.mp3'], .6)
            self.assertTrue(playing.wait(2))
            player.set_volume(.15)
            self.assertTrue(changed.wait(2))
            self.assertEqual(volumes[-1], .15)
            self.assertEqual(len([c for c in commands if c.startswith('play')]), 1)
            self.assertFalse(any(c.startswith('stop') for c in commands))
            player.close()
            self.assertTrue(stopped.wait(2))
            player.set_volume(.4)
        finally:
            player.shutdown()
            player.thread.join(3)
        self.assertEqual(len([c for c in commands if c.startswith('play')]), 1)

    def test_volume_change_while_decoder_opens_uses_latest_value(self):
        opening, release, playing = threading.Event(), threading.Event(), threading.Event()
        commands = []
        class Player:
            alias = 'fake'
            def open(self, path): opening.set(); release.wait(3); return True
            def command(self, command):
                commands.append(command)
                if command.startswith('play'): playing.set()
                return True, ''
            def set_volume(self, volume): pass
            def close(self): pass
        player = AsyncMP3Player(Player)
        try:
            player.enqueue([ROOT/'assets'/'sfx'/'bad_1.mp3'], .6)
            self.assertTrue(opening.wait(2))
            player.set_volume(.1)
            release.set()
            self.assertTrue(playing.wait(2))
            self.assertIn('setaudio fake volume to 100', commands)
            self.assertNotIn('setaudio fake volume to 600', commands)
        finally:
            release.set()
            player.shutdown()
            player.thread.join(3)
    def test_background_decoder_never_blocks_or_replays_stale_clicks(self):
        opening, release, played = threading.Event(), threading.Event(), threading.Event()
        played_paths = []
        class SlowPlayer:
            alias = 'fake'
            def open(self, path):
                self.path = path
                if path.stem == 'click_1':
                    opening.set()
                    release.wait(3)
                return True
            def command(self, command):
                if command.startswith('play'):
                    played_paths.append(self.path.stem)
                    played.set()
                return True, ''
            def close(self): pass
        player = AsyncMP3Player(SlowPlayer)
        try:
            player.enqueue([ROOT/'assets'/'sfx'/'click_1.mp3'], .6)
            self.assertTrue(opening.wait(2))
            begin = time.perf_counter()
            player.enqueue([ROOT/'assets'/'sfx'/'bad_1.mp3'], .6)
            player.enqueue([ROOT/'assets'/'sfx'/'good_1.mp3'], .6)
            self.assertLess(time.perf_counter()-begin, .1)
            release.set()
            self.assertTrue(played.wait(2))
            self.assertEqual(played_paths, ['good_1'])
        finally:
            release.set()
            player.shutdown()
            player.thread.join(3)

    def test_mute_cancels_sound_that_is_still_opening(self):
        opening, release = threading.Event(), threading.Event()
        played = []
        class SlowPlayer:
            alias = 'fake'
            def open(self, path): opening.set(); release.wait(3); return True
            def command(self, command):
                if command.startswith('play'): played.append(True)
                return True, ''
            def close(self): pass
        player = AsyncMP3Player(SlowPlayer)
        try:
            player.enqueue([ROOT/'assets'/'sfx'/'click_1.mp3'], .6)
            self.assertTrue(opening.wait(2))
            player.close()
            release.set()
        finally:
            player.shutdown()
            player.thread.join(3)
        self.assertEqual(played, [])

    def test_fx_destination_and_no_repeated_sound(self):
        with patch.object(self.audio, 'play') as play:
            for n in (1, 2, 5):
                self.audio.fx_pose_changed(10, n)
                play.assert_called_with('good')
            for n in (3, 7, 8, 9, 11, 13, 14):
                self.audio.fx_pose_changed(10, n)
                play.assert_called_with('bad')
            play.reset_mock()
            for n in range(1, 15):
                self.audio.fx_pose_changed(n, n)
            for n in (4, 6, 10, 12):
                self.audio.fx_pose_changed(1, n)
            play.assert_not_called()

    def test_external_missing_or_corrupt_sound_falls_back(self):
        paths = sound_candidates('click_1', ROOT/'build'/'external', ROOT)
        self.assertEqual(paths[0], ROOT/'build'/'external'/'assets'/'sfx'/'click_1.mp3')
        self.assertEqual(sound_candidates('click_2', ROOT, ROOT), ())
        self.audio.external = ROOT/'build'/'external'
        self.player.play.side_effect = [False, True]
        with patch.object(Path, 'is_file', return_value=True):
            self.assertTrue(self.audio.play('click'))
        self.assertEqual([call.args[0] for call in self.player.play.call_args_list], list(paths))

    def test_volume_config_valid_invalid_and_old_json(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'build') as name:
            external = Path(name)
            data = json.loads((ROOT/'kurumi.json').read_text(encoding='utf-8'))
            for value, expected in [(0, 0.), (.25, .25), (.6, .6), (1, 1.), (-1, DEFAULT_VOLUME),
                                    (2, DEFAULT_VOLUME), ('loud', DEFAULT_VOLUME),
                                    (True, DEFAULT_VOLUME), (None, DEFAULT_VOLUME)]:
                data['sound'] = {'volume': value}
                (external/'kurumi.json').write_text(json.dumps(data), encoding='utf-8')
                self.assertEqual(load_config(external, ROOT).volume, expected)
            del data['sound']
            (external/'kurumi.json').write_text(json.dumps(data), encoding='utf-8')
            self.assertEqual(load_config(external, ROOT).volume, DEFAULT_VOLUME)
            self.assertEqual(DEFAULT_VOLUME, .3)
            data['sound'] = {'custom_sound_option': 'keep'}
            (external/'kurumi.json').write_text(json.dumps(data), encoding='utf-8')
            self.assertEqual(load_config(external, ROOT).volume, DEFAULT_VOLUME)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows native MP3 backend')
    def test_native_mp3_decode_and_silent_play(self):
        player = WindowsMP3Player()
        try:
            for name in SOUND_NAMES:
                path = ROOT/'assets'/'sfx'/f'{name}.mp3'
                self.assertGreater(player.duration(path), 0, name)
                self.assertTrue(player.play(path, 0), name)
                ok, mode = player.command(f'status {player.alias} mode')
                self.assertTrue(ok)
                self.assertIn(mode, ('playing', 'stopped'))
                player.close()
        finally:
            player.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
