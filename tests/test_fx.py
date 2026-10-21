import sys
import math
from pathlib import Path
import unittest
from decimal import Decimal
from unittest.mock import patch
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fx_logic import PriceMoodEngine, validated_policy
from fx_ocr import parse_price, read_price


class PriceTests(unittest.TestCase):
    def engine(self, **overrides):
        return PriceMoodEngine(validated_policy(overrides))

    def window(self, final, **overrides):
        engine = self.engine(**overrides)
        for t in range(0, 32, 2): engine.receive(100., t)
        engine.receive(final, 32.)
        return engine, engine.receive(final, 34.)

    def test_thresholds_and_exact_boundaries(self):
        for price, mood, pool in [(100.3, 'up_strong', [1, 5]), (100.08, 'up', [2]),
                (100.079, 'flat', [6]), (99.921, 'flat', [6]), (99.92, 'down', [11, 7]),
                (99.7, 'down_strong', [3, 9]), (97., 'down_strong', [3, 9]),
                (96.99, 'panic', [13, 8, 14])]:
            with self.subTest(price=price):
                engine, update = self.window(price)
                self.assertEqual(update.mood, mood)
                self.assertIn(engine.current, pool)

    def test_warmup_ten_seconds_four_quotes_and_two_confirmations(self):
        engine = self.engine()
        for t in range(0, 10, 2):
            self.assertIsNone(engine.receive(100+t*.1, t).percent)
        first = engine.receive(101., 10.)
        self.assertEqual(first.percent, 1.)
        self.assertEqual(first.span_seconds, 10.)
        self.assertEqual(first.mood, 'waiting')
        second = engine.receive(101., 12.)
        self.assertEqual(second.mood, 'up')  # No strong pose before full short window.
        self.assertEqual(second.pose, 2)
        self.assertTrue(second.warming)
        self.assertIsNone(second.long_percent)
        sparse = self.engine()
        for t in (0., 5., 10.): self.assertIsNone(sparse.receive(100, t).percent)
        self.assertIsNotNone(sparse.receive(100, 15.).percent)

    def test_full_short_window_unlocks_strong_after_confirmation(self):
        engine = self.engine()
        for t in range(0, 31, 2): update = engine.receive(100+t*.02, t)
        self.assertEqual(update.mood, 'up')
        update = engine.receive(100.64, 32.)
        self.assertFalse(update.warming)
        self.assertEqual(update.mood, 'up_strong')
        self.assertIn(engine.current, [1, 5])

    def test_short_reversal_dominates_long_trend(self):
        engine = self.engine(interval_ms=2000)
        for t in range(0, 121, 2): engine.receive(100+t*.01, t)
        for t in range(122, 153, 2): update = engine.receive(101.2-(t-120)*.012, t)
        self.assertGreater(update.long_percent, 0.)
        self.assertLess(update.percent, -.3)
        self.assertEqual(update.mood, 'down_strong')
        self.assertEqual(update.tier, 0)

    def test_long_window_protects_against_cumulative_drop(self):
        engine = self.engine(interval_ms=2000)
        for t in range(0, 121, 2): update = engine.receive(100-t*.03, t)
        self.assertGreater(update.percent, -3.)
        self.assertLess(update.long_percent, -3.)
        self.assertEqual(update.mood, 'panic')
        self.assertIn(engine.current, [13, 8, 14])

    def test_two_consecutive_valid_samples_and_hysteresis(self):
        engine, _ = self.window(100.1)
        self.assertEqual(engine.mood, 'up')
        for t, price in [(36,100.06),(38,100.05)]:
            self.assertEqual(engine.receive(price,t).mood,'up')
        self.assertEqual(engine.receive(100.03,40).mood,'up')
        self.assertEqual(engine.receive(100.06,42).mood,'up')  # cancels pending flat
        self.assertEqual(engine.receive(100.03,44).mood,'up')
        self.assertEqual(engine.receive(100.03,46).mood,'flat')

    def test_strong_hysteresis_exit_uses_point_two(self):
        engine, _ = self.window(100.4)
        for t in (36,38): self.assertEqual(engine.receive(100.25,t).mood,'up_strong')
        self.assertEqual(engine.receive(100.19,40).mood,'up_strong')
        self.assertEqual(engine.receive(100.19,42).mood,'up')

    def test_hold_ten_seconds_for_ordinary_moves_and_panic_bypass(self):
        engine, _ = self.window(100.1)
        self.assertEqual(engine.current,2)
        self.assertIsNone(engine.receive(98,36).pose)
        self.assertIsNone(engine.receive(98,38).pose)
        self.assertEqual(engine.mood,'down_strong')
        self.assertIsNone(engine.receive(98,42).pose)
        self.assertIn(engine.receive(98,44).pose,[3,9])
        self.assertIn(engine.receive(96,45).pose,[13,8,14])

    def test_same_mood_does_not_rotate_or_speak(self):
        engine, _ = self.window(99.6)
        first=engine.current
        for t in range(36,58,2):
            update=engine.receive(99.6,t)
            self.assertIsNone(update.pose)
            self.assertIsNone(update.text)
        manual=engine.manual(59)
        self.assertIn(manual.pose,[3,9])
        self.assertNotEqual(manual.pose,first)

    def test_stop_loss_immediate_once_and_rearmed(self):
        engine = self.engine()
        engine.stop_loss=99
        engine.receive(100,0)
        update=engine.receive(98,2)
        self.assertEqual(update.text,'该止损了……真的要止损吗？')
        self.assertIn(update.pose,[13,8,14])
        self.assertIsNone(engine.receive(98,4).text)
        engine.receive(100,6)
        self.assertEqual(engine.receive(98,8).text,update.text)

    def test_flat_five_minutes_and_gap_resets(self):
        engine=self.engine()
        for t in range(0,314,2): engine.receive(100,t)
        self.assertEqual(engine.current,6)
        self.assertEqual(engine.receive(100,314).pose,4)
        self.assertIsNone(engine.receive(100,316).pose)
        engine.receive(100,350)
        self.assertIsNone(engine.flat_since)
        self.assertEqual(engine.mood,'waiting')
        self.assertIsNone(engine.percent)

    def test_emotion_is_time_based_and_requires_long_agreement(self):
        for interval in (2,3):
            engine=self.engine(interval_ms=interval*1000)
            for t in range(0,121,interval):
                update=engine.receive(100+t*.01,t)
                if t<118:self.assertEqual(update.tier,0)
            self.assertEqual(engine.emotion,10)
            self.assertEqual(engine.tier(),1)
            since=engine.direction_since
            self.assertGreaterEqual(120-since,20)
            for t in (122,124):engine.receive(99.,t)
            self.assertLessEqual(engine.emotion,0)
            self.assertEqual(engine.tier(),0)

    def test_new_account_risk_bypasses_hold_without_new_ticker(self):
        engine,_=self.window(100.1)
        engine.set_account_risk('position','该止损了……真的要止损吗？')
        update=engine.advance(35)
        self.assertEqual(update.mood,'panic')
        self.assertIn(update.pose,[13,8,14])
        self.assertIn('止损',update.text)
        engine.set_account_risk('daily','今天先停手')
        update=engine.advance(36)
        self.assertIn(engine.current,[8,14])
        self.assertEqual(update.text,'今天先停手')
        self.assertIsNone(engine.advance(37).text)

    def test_hour_rest_once_and_hold(self):
        engine,_=self.window(100.1,rest_after_seconds=35)
        self.assertIsNone(engine.advance(35).text)
        update=engine.advance(44)
        self.assertEqual((update.pose,update.text),(4,'休息一下吧'))
        self.assertTrue(engine.rest_done)
        self.assertIsNone(engine.advance(45).text)
        self.assertIsNone(engine.receive(100.1,46).pose)

    def test_hour_rest_is_not_lost_in_panic(self):
        engine,_=self.window(96,rest_after_seconds=40)
        self.assertEqual(engine.advance(44).text,'休息一下吧')
        self.assertIsNone(engine.receive(96,46).pose)

    def test_both_window_anchors_stay_nominal_with_timer_jitter(self):
        jitter=[-.01,.01,0.,.005,-.005]
        for interval in (2,3,5,10):
            engine=self.engine(interval_ms=interval*1000)
            samples=[]
            for i in range(120//interval+100):
                now=i*interval+jitter[i%len(jitter)]
                price=100+math.sin(i*.6)*.7+i*.001
                samples.append((now,price));update=engine.receive(price,now)
                for seconds,actual in ((30,update.percent),(120,update.long_percent)):
                    steps=seconds//interval
                    if i>=steps:
                        anchor=samples[i-steps]
                        self.assertAlmostEqual(now-anchor[0],seconds,delta=.021)
                        self.assertAlmostEqual(actual,(price/anchor[1]-1)*100,places=8)
                        if seconds==120:self.assertEqual(engine.history[0],anchor)

    def test_jitter_does_not_reverse_drop_or_trigger_panic_early(self):
        for interval in (2,3,5,10):
            engine=self.engine(interval_ms=interval*1000)
            for i in range(0,120//interval+1):
                t=i*interval
                engine.receive(104. if t==0 else 100.,t+.01 if t==0 else float(t))
            update=engine.receive(98.,120+interval-.01)
            self.assertEqual(update.percent,-2.)
            self.assertEqual(update.long_percent,-2.)
            update=engine.receive(98.,120+2*interval-.01)
            self.assertEqual(update.long_percent,-2.)
            self.assertNotEqual(update.mood,'panic')

    def test_duplicate_out_of_order_and_invalid_do_not_confirm(self):
        engine=self.engine()
        for t in range(0,12,2):engine.receive(100+t*.01,t)
        pending=engine.pending_count
        for price,t in [(101,10),(101,8),(float('nan'),12),(-1,12)]:
            engine.receive(price,t)
            self.assertEqual(engine.pending_count,pending)
        self.assertEqual(engine.receive(100.12,12).mood,'up')

    def test_config_validation_and_defaults(self):
        p=validated_policy()
        self.assertEqual((p['interval_ms'],p['okx_interval_ms'],p['account_interval_ms']),(3000,2000,5000))
        self.assertEqual((p['fast_window_seconds'],p['window_seconds'],p['hold_seconds']),(30,120,10))
        for config in [{'hold_seconds':0},{'ocr_confidence':1.1},{'flat_percent':2},
                       {'poses':{'up':[]}},{'stop_loss':float('nan')},
                       {'fast_window_seconds':5},{'warmup_samples':3.5},
                       {'confirm_samples':1},{'flat_exit_percent':.09},{'strong_exit_percent':.31}]:
            with self.subTest(config=config),self.assertRaises(ValueError):validated_policy(config)


class OCRTests(unittest.TestCase):
    def test_strict_price_parser(self):
        for text, number in [('123.45', '123.45'), ('￥ 1,234.50 元', '1234.50'),
                             ('０.００１２', '.0012'), ('99 USD', '99')]:
            self.assertEqual(parse_price(text), Decimal(number))
        for text in ['O.1', '12 34', '12.3 45.6', '现价123', '123%', '0', '-12',
                     '1,23', '1.2.3', 'NaN', '', '12\n34', '1e8']:
            self.assertIsNone(parse_price(text), text)

    def test_real_local_model_synthetic_quotes(self):
        font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 40)
        for text, value, bg, fg in [('123.45', 123.45, 'white', 'black'),
                ('1,234.56', 1234.56, '#141821', '#ed535d'),
                ('0.00125', .00125, '#141821', '#32cc82'),
                ('98765', 98765, 'white', 'black')]:
            image = Image.new('RGB', (max(190, int(font.getlength(text))+24), 64), bg)
            ImageDraw.Draw(image).text((10, 8), text, font=font, fill=fg)
            with self.subTest(text=text):
                self.assertEqual(read_price(np.array(image)), value)
        self.assertIsNone(read_price(np.full((60, 200, 3), 255, dtype=np.uint8)))

    def test_low_confidence_or_disagreement_is_skipped(self):
        image = np.full((60, 200, 3), 255, dtype=np.uint8)
        for result in [[('123.45', .99), ('12345', .99)], [('123', .99), ('123', .5)],
                       [('123 456', .99), ('123 456', .99)]]:
            with patch('fx_ocr._recognizer', return_value=(result, .01)):
                self.assertIsNone(read_price(image))


if __name__ == '__main__':
    unittest.main(verbosity=2)
