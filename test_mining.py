import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path
from game import Game, GameError, CONFIG
from mining import MINING, MINERALS, kg


class FixedRng:
    key='stone'
    ratio=1.0
    def choices(self,keys,weights,k):
        return [self.key if weights[keys.index(self.key)] else keys[next(i for i,p in enumerate(weights) if p)]]
    def uniform(self,a,b): return self.ratio
    def random(self): return 0


class MiningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.now=1000.0
        self.rng=FixedRng()
        self.game=Game(Path(self.tmp.name)/'farm.db',clock=lambda:self.now,rng=self.rng)
        self.s,_=self.game.visit(1,'농부',10)
        self.s.update(rows=2,gold=1000000)
        self.op=0
        self.save()

    def tearDown(self): self.tmp.cleanup()

    def save(self):
        with self.game.transaction() as db:
            db.execute('UPDATE players SET state=? WHERE id=?',(json.dumps(self.s),'1'))

    def act(self,action,**kwargs):
        self.op+=1
        self.s,msg=self.game.act(1,self.s['version'],f'mine-test-{self.op}',action,**kwargs)
        return msg

    def top(self,capacity_level=4):
        self.act('mine_open')
        self.s['mine'].update(workers=5,pickaxe=3,bag_level=capacity_level)
        self.save()

    def test_locked_first_visit_and_freebies_once(self):
        self.s['rows']=1; self.save()
        with self.assertRaises(GameError): self.act('mine_open')
        self.assertIsNone(self.game.read(1)['mine'])
        notice=self.act('expand')
        self.assertIn('이제 탄광을 갈 수 있습니다',notice)
        self.act('mine_open')
        m=self.s['mine']
        self.assertEqual((m['workers'],m['pickaxe'],m['bag_level']),(1,0,0))
        before=self.s['gold']
        self.act('mine_hire'); self.act('mine_open')
        self.assertEqual(self.s['mine']['workers'],2)
        self.assertEqual(self.s['gold'],before-1000)

    def test_offline_capacity_stop_and_resume_without_catchup(self):
        self.act('mine_open')
        self.now+=86400
        self.game=Game(self.game.path,clock=lambda:self.now,rng=self.rng)
        self.act('mine_open')
        self.assertEqual(len(self.s['mine']['bag']),60)
        self.act('mine_collect')
        self.assertEqual(self.s['mine']['inventory']['stone']['count'],60)
        self.now+=119; self.act('mine_open')
        self.assertEqual(len(self.s['mine']['bag']),0)
        self.now+=1; self.act('mine_open')
        self.assertEqual(len(self.s['mine']['bag']),1)

    def test_hiring_preserves_fraction_and_old_production(self):
        self.act('mine_open')
        self.now+=60
        self.act('mine_hire')
        self.assertEqual(self.s['mine']['progress'],.5)
        self.now+=44; self.act('mine_open')
        self.assertEqual(len(self.s['mine']['bag']),0)
        self.now+=1; self.act('mine_open')
        self.assertEqual(len(self.s['mine']['bag']),1)

    def test_pickaxe_upgrade_settles_old_table_first(self):
        self.rng.key='coal'
        self.act('mine_open')
        self.now+=240
        self.act('mine_pickaxe')
        self.assertEqual([x['mineral'] for x in self.s['mine']['bag']],['stone','stone'])
        self.now+=120; self.act('mine_open')
        self.assertEqual(self.s['mine']['bag'][-1]['mineral'],'coal')

    def test_pity_guarantees_at_eight_productive_hours_and_persists(self):
        self.top()
        self.now+=28799
        self.act('mine_open')
        self.assertFalse(any(x['mineral']=='diamond' for x in self.s['mine']['bag']))
        self.game=Game(self.game.path,clock=lambda:self.now,rng=self.rng)
        self.now+=1
        self.act('mine_open')
        self.assertEqual(len(self.s['mine']['bag']),576)
        self.assertEqual(self.s['mine']['bag'][-1]['mineral'],'diamond')
        self.assertAlmostEqual(self.s['mine']['pity_seconds'],0)
        self.act('mine_collect')
        self.assertEqual(self.s['mine']['inventory']['diamond']['count'],1)
        self.assertAlmostEqual(self.s['mine']['pity_seconds'],0)

    def test_full_bag_pauses_pity_and_upgrade_cannot_backfill(self):
        self.top(0)
        self.now+=3000; self.act('mine_open')
        self.assertEqual(self.s['mine']['pity_seconds'],3000)
        self.now+=86400
        for _ in range(4): self.act('mine_bag_upgrade')
        self.assertEqual(len(self.s['mine']['bag']),60)
        self.assertEqual(self.s['mine']['pity_seconds'],3000)
        self.act('mine_collect')
        self.now+=25800; self.act('mine_open')
        self.assertEqual(self.s['mine']['bag'][-1]['mineral'],'diamond')

    def test_natural_diamond_resets_pity_at_production(self):
        self.top()
        self.rng.key='diamond'
        self.now+=70; self.act('mine_open')
        self.assertEqual(self.s['mine']['bag'][0]['mineral'],'diamond')
        self.assertAlmostEqual(self.s['mine']['pity_seconds'],20)
        self.act('mine_collect')
        self.assertAlmostEqual(self.s['mine']['pity_seconds'],20)
        self.act('mine_sell')
        self.assertAlmostEqual(self.s['mine']['pity_seconds'],20)

    def test_fixed_prices_gem_weight_records_and_shared_wallet(self):
        self.top()
        self.rng.ratio=1.2
        before=self.s['gold']
        for key,c in MINERALS.items():
            self.rng.key=key
            self.now+=50; self.act('mine_open')
            x=self.s['mine']['bag'][-1]
            self.assertEqual(x['value'],c['base_sale'] if c['sale_mode']=='fixed_per_item' else round(c['base_sale']*1.2))
            self.assertEqual(self.s['mine']['records'][key],round(c['base_weight_kg']*1000000*1.2))
        expected=sum(x['value'] for x in self.s['mine']['bag'])
        records=self.s['mine']['records'].copy()
        self.act('mine_collect')
        self.assertEqual(self.s['gold'],before)
        self.act('mine_sell',mineral='stone')
        self.assertEqual(self.s['gold'],before+10)
        self.act('mine_sell')
        self.assertEqual(self.s['gold'],before+expected)
        self.assertEqual(self.s['mine']['records'],records)
        self.assertEqual(self.game.ranking(10)[0][2],before+expected)
        self.assertEqual(self.game.ranking(10,mineral='diamond')[0][2],1200)
        self.assertEqual(kg(1200),'0.0012kg')

    def test_repeat_refresh_does_not_reroll_and_failed_payment_rolls_back(self):
        self.act('mine_open'); self.now+=120; self.act('mine_open')
        item=self.s['mine']['bag'][0].copy()
        self.rng.ratio=.8
        self.act('mine_open')
        self.assertEqual(self.s['mine']['bag'][0],item)
        self.s['gold']=0; self.save()
        before=self.game.read(1)
        with self.assertRaises(GameError): self.act('mine_hire')
        self.assertEqual(self.game.read(1),before)

    def test_concurrent_collection_only_once(self):
        self.act('mine_open'); self.now+=120; self.act('mine_open')
        version=self.s['version']
        def collect(n):
            try:
                self.game.act(1,version,f'parallel-{n}','mine_collect')
                return True
            except GameError: return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(collect,range(8))),1)
        self.assertEqual(self.game.read(1)['mine']['inventory']['stone']['count'],1)

    def test_clock_rollback_and_upgrade_limits(self):
        self.act('mine_open')
        last=self.s['mine']['last']
        self.now-=100; self.act('mine_open')
        self.assertEqual(self.s['mine']['last'],last)
        for action,times in [('mine_hire',4),('mine_bag_upgrade',4),('mine_pickaxe',3)]:
            for _ in range(times): self.act(action)
            with self.assertRaises(GameError): self.act(action)

    def test_bird_exempt_and_existing_scarecrow_preserved(self):
        self.act('buy',crop='potato',quantity=5)
        self.act('plant',crop='potato',row=0)
        self.assertTrue(all(not p['bird'] for p in self.s['plots'][:5]))
        self.assertEqual(CONFIG['scarecrow_price'],2000)
        self.assertEqual(CONFIG['bird_probability'],.1)
        self.s['scarecrow']=True; self.save()
        self.assertTrue(self.game.read(1)['scarecrow'])

    def test_probability_tables_and_expected_offline_batch(self):
        for table in MINING['probabilities'].values(): self.assertAlmostEqual(sum(table),1)
        self.assertEqual(MINING['probabilities']['diamond'][-1],.002)
        self.act('mine_open')
        self.now+=121; self.act('mine_open')
        self.assertEqual(len(self.s['mine']['bag']),1)
        self.assertAlmostEqual(self.s['mine']['progress'],1/120)

    def test_cumulative_combines_farm_and_mine_without_wallet_or_rewards(self):
        self.s['revenue']=300
        self.save()
        self.act('mine_open'); self.now+=120; self.act('mine_open')
        self.act('mine_collect'); self.act('mine_sell')
        self.assertEqual(self.game.ranking(10,cumulative=True)[0][2],310)
        self.act('checkin')
        self.act('buy',crop='carrot',quantity=5)
        self.assertEqual(self.game.ranking(10,cumulative=True)[0][2],310)
        self.assertEqual(self.game.ranking(10)[0][2],self.s['gold'])

    def test_rebalance_refunds_once_reprices_unsold_and_preserves_records(self):
        self.top()
        m=self.s['mine']
        m['bag']=[dict(mineral='stone',weight_mg=1000000,value=2),dict(mineral='ruby',weight_mg=12000,value=360)]
        m['inventory']={'coal':dict(count=3,value=24,best_mg=500000),'emerald':dict(count=2,value=300,best_mg=5000)}
        m['records']={'ruby':12000}
        m['revenue']=77; self.s['revenue']=123
        self.save(); before=self.s['gold']
        result=self.game.rebalance_mining_once()
        state=self.game.read(1)
        self.assertEqual(result['refund'],92000)
        self.assertEqual(state['gold'],before+92000)
        self.assertEqual(state['mine']['inventory']['coal']['value'],90)
        self.assertEqual(state['mine']['inventory']['emerald']['value'],1000)
        self.assertEqual(state['mine']['bag'][1]['value'],1200)
        self.assertEqual(state['mine']['records'],{'ruby':12000})
        self.assertEqual(self.game.ranking(10,cumulative=True)[0][2],200)
        self.assertTrue(self.game.rebalance_mining_once()['already_applied'])
        self.assertEqual(self.game.read(1),state)

    def test_rebalance_first_upgrade_exact_refund_and_no_mine_no_reward(self):
        self.act('mine_open')
        self.s['mine'].update(workers=2,pickaxe=1)
        self.save()
        self.game.visit(2,'다른 농부',10)
        before=self.s['gold']
        result=self.game.rebalance_mining_once()
        self.assertEqual(result['players'],1)
        self.assertEqual(self.game.read(1)['gold'],before+4000)
        self.assertEqual(self.game.read(2)['gold'],100)
