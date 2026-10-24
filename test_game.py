import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from datetime import datetime

from game import CONFIG, CROPS, Game, GameError, KST


class GameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.path = str(Path(self.temp.name) / 'farm.db')
        self.game = Game(self.path, clock=lambda: self.now)
        self.s, _ = self.game.visit(1, '농부', 10)
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def act(self, action, **args):
        self.counter += 1
        self.s, message = self.game.act(1, self.s['version'], self.counter, action, **args)
        return message

    def test_daily_reward_once_across_servers_and_restart(self):
        self.now = datetime(2026, 10, 24, 12, tzinfo=KST).timestamp()
        self.act('checkin')
        self.assertEqual(self.s['gold'], 130)
        self.assertEqual(self.s['revenue'], 0)
        self.assertEqual(self.game.ranking(), [])
        self.game.visit(1, '농부', 20)
        self.game = Game(self.path, clock=lambda: self.now)
        before = self.game.read(1)
        with self.assertRaises(GameError):
            self.act('checkin')
        self.assertEqual(self.game.read(1), before)

    def test_korean_midnight_not_24_hour_wait(self):
        self.now = datetime(2026, 10, 24, 23, 59, 59, tzinfo=KST).timestamp()
        self.act('checkin')
        self.now += 1
        self.act('checkin')
        self.assertEqual(self.s['attendance_streak'], 2)
        self.assertEqual(self.s['gold'], 160)

    def test_seven_day_bonus_repeats_every_seven_days(self):
        self.now = datetime(2026, 10, 24, 12, tzinfo=KST).timestamp()
        for day in range(1, 15):
            message = self.act('checkin')
            self.assertEqual('보너스!' in message, day % 7 == 0)
            self.now += 86400
        self.assertEqual(self.s['attendance_streak'], 14)
        self.assertEqual(self.s['gold'], 100+14*30+2*100)
        self.assertEqual(self.s['revenue'], 0)

    def test_missing_day_resets_streak_without_taking_gold(self):
        self.act('checkin')
        self.now += 2*86400
        self.act('checkin')
        self.assertEqual(self.s['attendance_streak'], 1)
        self.assertEqual(self.s['gold'], 160)

    def test_zero_gold_can_buy_five_seeds_after_attendance(self):
        self.act('expand')
        self.assertEqual(self.s['gold'], 0)
        self.act('checkin')
        self.act('buy', crop='carrot', quantity=5)
        self.assertEqual(self.s['gold'], 5)

    def test_concurrent_attendance_pays_once(self):
        def claim(op):
            try:
                state = self.game.read(1)
                self.game.act(1, state['version'], op, 'checkin')
                return True
            except GameError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(100, 108)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.game.read(1)['gold'], 130)

    def test_start_only_once_and_cross_server(self):
        self.act('buy', crop='carrot', quantity=1)
        state, fresh = self.game.visit(1, '새이름', 20)
        self.assertFalse(fresh)
        self.assertEqual(state['gold'], 95)
        self.assertEqual(state['seeds']['carrot'], 6)
        self.assertEqual(state['revenue'], 0)

    def test_restart_growth_and_records_survive_sale(self):
        self.act('plant', crop='carrot')
        with self.assertRaises(GameError):
            self.act('harvest')
        self.now += 30
        self.game = Game(self.path, clock=lambda: self.now)
        self.act('harvest')
        self.assertEqual(len(self.s['bag']), 5)
        records = self.s['records'].copy()
        value = sum(x['value'] for x in self.s['bag'])
        self.act('sell')
        self.assertEqual(self.s['records'], records)
        self.assertEqual(self.s['revenue'], value)
        self.assertEqual(self.s['gold'], 100+value)
        self.assertEqual(self.s['bag'], [])

    def test_insufficient_seeds_rolls_back(self):
        self.act('expand')
        before = self.game.read(1)
        with self.assertRaises(GameError):
            self.act('plant', crop='carrot')
        self.assertEqual(self.game.read(1), before)

    def test_concurrent_clicks_only_one_purchase(self):
        def buy(op):
            try:
                self.game.act(1, 0, op, 'buy', crop='carrot', quantity=5)
                return True
            except GameError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(buy, range(20)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.game.read(1)['gold'], 75)

    def test_duplicate_operation(self):
        self.act('buy', crop='carrot')
        with self.assertRaises(GameError):
            self.game.act(1, self.s['version'], self.counter, 'buy', crop='carrot')
        self.assertEqual(self.game.read(1)['seeds']['carrot'], 6)

    def test_individual_plot_and_locked_plot(self):
        with self.assertRaises(GameError):
            self.act('plant', crop='carrot', plot=5)
        self.act('plant', crop='carrot', plot=3)
        self.assertIsNotNone(self.s['plots'][3])
        self.assertEqual(self.s['seeds']['carrot'], 4)

    def test_row_planting_locks_and_purchase_unlock(self):
        before = self.game.read(1)
        with self.assertRaises(GameError):
            self.act('plant', crop='carrot', row=1)
        self.assertEqual(self.game.read(1), before)
        self.act('plant', crop='carrot', row=0)
        self.assertTrue(all(self.s['plots'][i] for i in range(5)))
        self.assertTrue(all(p is None for p in self.s['plots'][5:]))
        self.assertEqual(self.s['seeds']['carrot'], 0)
        self.now += 30
        self.act('harvest')
        self.act('sell')
        self.act('expand')
        self.act('buy', crop='carrot', quantity=5)
        self.act('plant', crop='carrot', row=1)
        self.assertTrue(all(self.s['plots'][i] for i in range(5,10)))
        self.assertTrue(all(p is None for p in self.s['plots'][:5]))

    def test_row_only_fills_empty_cells(self):
        self.act('plant', crop='carrot', plot=2)
        original = self.s['plots'][2].copy()
        self.act('plant', crop='carrot', row=0)
        self.assertEqual(self.s['plots'][2], original)
        self.assertEqual(self.s['seeds']['carrot'], 0)
        self.assertTrue(all(self.s['plots'][i] for i in range(5)))

    def test_harvest_record_notices_first_tie_and_new_best(self):
        class FixedWeights:
            ratio = 1
            def random(self): return .5
            def uniform(self, a, b): return self.ratio
        rng = FixedWeights()
        self.game.rng = rng
        self.act('plant', crop='carrot', row=0)
        self.now += 30
        message = self.act('harvest')
        self.assertEqual(message.count('첫 기록!'), 1)
        self.assertIn('100g', message)
        self.assertIn('당근 5개', message)
        self.act('buy', crop='carrot', quantity=5)
        self.act('plant', crop='carrot', row=0)
        self.now += 30
        message = self.act('harvest')
        self.assertNotIn('신기록', message)
        self.assertNotIn('첫 기록', message)
        self.act('buy', crop='carrot', quantity=5)
        self.act('plant', crop='carrot', row=0)
        rng.ratio = 1.2
        self.now += 30
        message = self.act('harvest')
        self.assertEqual(message.count('개인 신기록!'), 1)
        self.assertIn('120g', message)
        self.assertIn('이전 100g', message)

    def test_multi_crop_records_reported_separately(self):
        self.act('buy', crop='tomato')
        self.act('plant', crop='carrot', plot=0)
        self.act('plant', crop='tomato', plot=1)
        self.now += 900
        message = self.act('harvest')
        self.assertEqual(message.count('첫 기록!'), 2)
        self.assertIn('🥕 당근', message)
        self.assertIn('🍅 토마토', message)

    def test_fertilizer_reservation_failure_restart_and_one_use(self):
        self.act('buy_fertilizer', quantity=1)
        self.assertEqual(self.s['gold'], 95)
        self.act('reserve_fertilizer', row=0)
        self.assertEqual(self.s['fertilizer'], 1)
        with self.assertRaises(GameError):
            self.act('reserve_fertilizer', row=0)
        with self.assertRaises(GameError):
            self.act('plant', crop='tomato', row=0)
        self.assertTrue(self.game.read(1)['fertilizer_rows'][0])
        self.game = Game(self.path, clock=lambda: self.now)
        self.act('plant', crop='carrot', row=0)
        self.assertEqual(self.s['fertilizer'], 0)
        self.assertFalse(self.s['fertilizer_rows'][0])
        self.assertTrue(all(p['ready'] == self.now+24 for p in self.s['plots'][:5]))
        self.assertTrue(all(p['fertilized'] for p in self.s['plots'][:5]))
        self.now += 24
        self.act('harvest')
        self.act('sell')
        self.act('buy', crop='carrot', quantity=5)
        self.act('plant', crop='carrot', row=0)
        self.assertEqual(self.s['plots'][0]['ready'], self.now+30)

    def test_fertilizer_cancel_locked_occupied_and_no_overbooking(self):
        self.act('buy_fertilizer')
        with self.assertRaises(GameError):
            self.act('reserve_fertilizer', row=1)
        self.act('reserve_fertilizer', row=0)
        self.act('cancel_fertilizer', row=0)
        self.assertEqual(self.s['fertilizer'], 1)
        self.act('plant', crop='carrot', row=0)
        with self.assertRaises(GameError):
            self.act('reserve_fertilizer', row=0)
        self.now += 30
        self.act('harvest')
        self.act('sell')
        self.act('expand')
        self.act('reserve_fertilizer', row=0)
        with self.assertRaises(GameError):
            self.act('reserve_fertilizer', row=1)
        self.assertEqual(sum(self.game.read(1)['fertilizer_rows']), 1)

    def test_whole_farm_fertilizes_only_reserved_row(self):
        self.act('expand')
        self.act('plant', crop='carrot', row=0)
        self.now += 30
        self.act('harvest')
        self.act('sell')
        # 테스트 준비용 골드, 실제 비료·씨앗 구매 경로는 그대로 사용.
        with self.game.transaction() as db:
            import json
            self.s['gold'] = 1000
            db.execute('UPDATE players SET state=? WHERE id=?', (json.dumps(self.s), '1'))
        self.act('buy', crop='tomato', quantity=10)
        self.act('buy_fertilizer')
        self.act('reserve_fertilizer', row=1)
        self.act('plant', crop='tomato')
        self.assertEqual(self.s['plots'][0]['ready'], self.now+900)
        self.assertEqual(self.s['plots'][5]['ready'], self.now+720)
        self.assertEqual(self.s['fertilizer'], 0)

    def test_existing_save_without_fertilizer_fields(self):
        import json
        with self.game.transaction() as db:
            old = self.s.copy()
            old.pop('fertilizer', None)
            old.pop('fertilizer_rows', None)
            db.execute('UPDATE players SET state=? WHERE id=?', (json.dumps(old), '1'))
        state = self.game.read(1)
        self.assertEqual(state['fertilizer'], 0)
        self.assertEqual(state['gold'], 100)
        self.act('buy_fertilizer')
        self.assertEqual(self.s['fertilizer'], 1)

    def test_all_growth_times_and_crop_sale(self):
        self.act('buy', crop='strawberry')
        self.act('buy', crop='tomato')
        self.act('buy', crop='pumpkin')
        for i, key in enumerate(CROPS):
            self.act('plant', crop=key, plot=i)
            self.assertEqual(self.s['plots'][i]['ready'], self.now+CROPS[key]['seconds'])
        self.now += 1800
        self.act('harvest')
        receipt = self.act('sell', crop='tomato')
        self.assertIn('🍅 토마토 1개', receipt)
        self.assertNotIn('🥕 당근', receipt)
        self.assertEqual({x['crop'] for x in self.s['bag']}, {'strawberry','carrot','pumpkin'})
        total = sum(x['value'] for x in self.s['bag'])
        receipt = self.act('sell')
        self.assertIn('🥕 당근 1개', receipt)
        self.assertIn('🎃 호박 1개', receipt)
        self.assertIn(f'총 3개 · +{total:,}골드', receipt)

    def test_crop_profit_progression_and_new_crop_migration(self):
        profit = {k: c['base_sale']-c['seed_price'] for k,c in CROPS.items()}
        self.assertGreater(profit['carrot'], profit['strawberry']*3)
        self.assertGreater(profit['tomato'], profit['carrot']*30)
        self.assertGreater(profit['pumpkin'], profit['tomato']*2)
        import json
        with self.game.transaction() as db:
            old = self.s.copy()
            old['seeds'] = dict(old['seeds'])
            del old['seeds']['strawberry']
            db.execute('UPDATE players SET state=? WHERE id=?', (json.dumps(old), '1'))
        restored = self.game.read(1)
        self.assertEqual(restored['seeds']['strawberry'], 0)
        self.assertEqual(restored['seeds']['carrot'], 5)

    def test_expansion_max_and_revenue_not_decreased(self):
        # 초기 자금으로 첫 확장, 반복 농사로 나머지 확장 비용 획득.
        self.act('expand')
        for _ in range(200):
            if self.s['gold'] >= 3900:
                break
            self.act('plant', crop='carrot', plot=0)
            self.now += 30
            self.act('harvest')
            self.act('sell')
            # 1개 농사 반복에는 남은 씨앗을 사용하고 소진 시 재구매.
            if not self.s['seeds']['carrot']:
                self.act('buy', crop='carrot')
        # 대량 수익이 필요한 상한 검사는 별도 정상 상태 fixture로 설정.
        with self.game.transaction() as db:
            import json
            self.s['gold'] = 10000
            db.execute('UPDATE players SET state=? WHERE id=?', (json.dumps(self.s), '1'))
        revenue = self.s['revenue']
        for _ in range(3):
            self.act('expand')
        self.assertEqual(self.s['rows'], 5)
        self.assertEqual(self.s['revenue'], revenue)
        with self.assertRaises(GameError):
            self.act('expand')

    def test_server_and_global_ranking(self):
        self.act('plant', crop='carrot')
        self.now += 30
        self.act('harvest')
        self.act('sell')
        self.assertEqual(self.game.ranking(10)[0][0], '1')
        self.assertEqual(self.game.ranking(20), [])
        self.game.visit(1, '농부', 20)
        self.assertEqual(self.game.ranking(20), self.game.ranking())
        self.assertTrue(self.game.ranking(10, 'carrot'))
        self.assertEqual(self.game.ranking(10, 'pumpkin'), [])

    def test_scarecrow_not_retroactive(self):
        class Birds:
            def random(self): return 0
            def uniform(self, a, b): return 1
        self.game.rng = Birds()
        with self.game.transaction() as db:
            import json
            self.s['gold'] = 1000
            db.execute('UPDATE players SET state=? WHERE id=?', (json.dumps(self.s), '1'))
        self.act('plant', crop='carrot', plot=0)
        self.act('scarecrow')
        self.act('plant', crop='carrot', plot=1)
        self.assertTrue(self.s['plots'][0]['bird'])
        self.assertFalse(self.s['plots'][1]['bird'])
        self.now += 30
        self.act('harvest')
        self.assertEqual(self.s['bag'][0]['weight'], self.s['bag'][1]['weight'])
        self.assertLess(self.s['bag'][0]['value'], self.s['bag'][1]['value'])


if __name__ == '__main__':
    unittest.main()
