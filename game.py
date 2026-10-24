"""Discord에 의존하지 않는 게임 규칙과 원자적 SQLite 저장."""
import json
import random
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

CONFIG = json.loads(Path(__file__).with_name('balance.json').read_text(encoding='utf-8'))
CROPS = CONFIG['crops']


class GameError(Exception):
    pass


def grade(weight, base):
    ratio = weight / base
    return '전설' if ratio >= 2 else '희귀' if ratio >= 1.5 else '큼' if ratio >= 1.1 else '보통'


class Game:
    def __init__(self, path='data/farm.db', clock=time.time, rng=None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path, self.clock, self.rng = str(path), clock, rng or random.Random()
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS players (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS members (
                    guild TEXT NOT NULL, user TEXT NOT NULL,
                    PRIMARY KEY(guild,user));
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, created REAL NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def visit(self, user, name, guild=None):
        with self.transaction() as db:
            row = db.execute('SELECT state FROM players WHERE id=?', (str(user),)).fetchone()
            fresh = row is None
            state = json.loads(row[0]) if row else dict(
                gold=CONFIG['starting_gold'], rows=1, scarecrow=False,
                seeds={k: CONFIG['starting_seeds'] if k == 'carrot' else 0 for k in CROPS},
                plots=[None]*25, bag=[], records={}, revenue=0, version=0)
            db.execute('INSERT INTO players VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name',
                       (str(user), name, json.dumps(state)))
            if guild is not None:
                db.execute('INSERT OR IGNORE INTO members VALUES(?,?)', (str(guild), str(user)))
            return state, fresh

    def read(self, user):
        with self.connect() as db:
            row = db.execute('SELECT state FROM players WHERE id=?', (str(user),)).fetchone()
        if not row:
            raise GameError('먼저 /농장으로 시작해 주세요.')
        return json.loads(row[0])

    def act(self, user, version, operation, action, **args):
        with self.transaction() as db:
            row = db.execute('SELECT state FROM players WHERE id=?', (str(user),)).fetchone()
            if not row:
                raise GameError('먼저 /농장으로 시작해 주세요.')
            s = json.loads(row[0])
            if db.execute('SELECT 1 FROM operations WHERE id=?', (str(operation),)).fetchone():
                raise GameError('이미 처리된 요청이에요. /농장에서 확인해 주세요.')
            if s['version'] != version:
                raise GameError('농장이 변경되었어요. /농장을 다시 열어 주세요.')
            message = self._apply(s, action, **args)
            s['version'] += 1
            db.execute('UPDATE players SET state=? WHERE id=?', (json.dumps(s), str(user)))
            db.execute('INSERT INTO operations VALUES(?,?)', (str(operation), self.clock()))
            return s, message

    def _apply(self, s, action, crop=None, quantity=1, plot=None):
        if crop is not None and crop not in CROPS:
            raise GameError('알 수 없는 작물이에요.')
        if action == 'buy':
            if crop is None or type(quantity) is not int or not 1 <= quantity <= 999:
                raise GameError('씨앗은 1~999개까지 구매할 수 있어요.')
            price = CROPS[crop]['seed_price'] * quantity
            self.pay(s, price)
            s['seeds'][crop] += quantity
            return f"{CROPS[crop]['name']} 씨앗 {quantity}개 구매 · {price}골드"
        if action == 'expand':
            if s['rows'] == 5:
                raise GameError('이미 25칸 모두 열려 있어요.')
            price = CONFIG['expansion_prices'][s['rows']-1]
            self.pay(s, price)
            s['rows'] += 1
            return f"{s['rows']}번째 줄 개방! · {price}골드"
        if action == 'scarecrow':
            if s['scarecrow']:
                raise GameError('이미 허수아비가 있어요.')
            self.pay(s, CONFIG['scarecrow_price'])
            s['scarecrow'] = True
            return '허수아비 설치 완료! 이제 새로 심는 작물을 보호해요.'
        if action == 'plant':
            if crop is None:
                raise GameError('작물을 선택해 주세요.')
            if plot is not None and (type(plot) is not int or not 0 <= plot < s['rows']*5):
                raise GameError('열린 밭을 선택해 주세요.')
            targets = [plot] if plot is not None else list(range(s['rows']*5))
            targets = [i for i in targets if s['plots'][i] is None]
            if not targets:
                raise GameError('빈 밭이 없어요.')
            if s['seeds'][crop] < len(targets):
                raise GameError(f"씨앗 {len(targets)}개가 필요해요. 현재 {s['seeds'][crop]}개예요.")
            s['seeds'][crop] -= len(targets)
            for i in targets:
                s['plots'][i] = dict(crop=crop, ready=self.clock()+CROPS[crop]['seconds'],
                                     bird=(not s['scarecrow'] and self.rng.random() < CONFIG['bird_probability']))
            return f"{CROPS[crop]['name']} {len(targets)}칸에 심었어요."
        if action == 'harvest':
            count = 0
            for i, p in enumerate(s['plots']):
                if p is None or p['ready'] > self.clock():
                    continue
                c = CROPS[p['crop']]
                # 0.70~1.30배 90%, 1.30~2.00배 9%, 2.00~3.00배 1%.
                roll = self.rng.random()
                lo, hi = (0.7, 1.3) if roll < .9 else (1.3, 2) if roll < .99 else (2, 3)
                weight = round(c['base_weight'] * self.rng.uniform(lo, hi))
                value = max(1, round(c['base_sale'] * weight / c['base_weight'] *
                                     (CONFIG['bird_value_multiplier'] if p['bird'] else 1)))
                s['bag'].append(dict(crop=p['crop'], weight=weight, value=value, bird=p['bird']))
                s['records'][p['crop']] = max(s['records'].get(p['crop'], 0), weight)
                s['plots'][i] = None
                count += 1
            if not count:
                raise GameError('아직 수확할 작물이 없어요.')
            return f'{count}개 수확했어요! /가방에서 무게와 등급을 확인하세요.'
        if action == 'sell':
            items = [x for x in s['bag'] if crop is None or x['crop'] == crop]
            if not items:
                raise GameError('판매할 수확물이 없어요.')
            value = sum(x['value'] for x in items)
            s['bag'] = [x for x in s['bag'] if crop is not None and x['crop'] != crop]
            s['gold'] += value
            s['revenue'] += value
            return f'{len(items)}개 판매 · +{value}골드'
        raise GameError('알 수 없는 작업이에요.')

    @staticmethod
    def pay(s, price):
        if s['gold'] < price:
            raise GameError(f"{price}골드가 필요해요. 현재 {s['gold']}골드예요.")
        s['gold'] -= price

    def ranking(self, guild=None, crop=None):
        with self.connect() as db:
            rows = db.execute('SELECT p.id,p.name,p.state FROM players p' +
                (' JOIN members m ON p.id=m.user WHERE m.guild=?' if guild is not None else ''),
                (str(guild),) if guild is not None else ()).fetchall()
        result = []
        for uid, name, raw in rows:
            s = json.loads(raw)
            score = s['records'].get(crop, 0) if crop else s['revenue']
            if score > 0:
                result.append((uid, name, score))
        return sorted(result, key=lambda x: (-x[2], int(x[0])))[:10]
