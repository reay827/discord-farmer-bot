"""Discord에 의존하지 않는 게임 규칙과 원자적 SQLite 저장."""
import json
import random
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from mining import apply_mining, MiningError

CONFIG = json.loads(Path(__file__).with_name('balance.json').read_text(encoding='utf-8'))
CROPS = CONFIG['crops']
KST = timezone(timedelta(hours=9))


def attendance_status(state, now):
    today = datetime.fromtimestamp(now, KST).date()
    last = state.get('attendance_last_date')
    streak = state.get('attendance_streak', 0)
    claimed = last is not None and last >= today.isoformat()
    next_streak = streak + 1 if last == (today-timedelta(days=1)).isoformat() else 1
    return today.isoformat(), claimed, streak if claimed else next_streak


class GameError(Exception):
    pass


def grade(weight, base):
    ratio = weight / base
    return '전설' if ratio >= 2 else '희귀' if ratio >= 1.5 else '큼' if ratio >= 1.1 else '보통'


def growth_seconds(crop, fertilized=False):
    return CROPS[crop]['seconds'] * (CONFIG['fertilizer_growth_multiplier'] if fertilized else 1)


def prepare_state(state):
    # 기존 농장의 재화와 작물은 유지하고 새 필드만 기본값으로 보완합니다.
    state.setdefault('fertilizer', 0)
    state.setdefault('fertilizer_rows', [False]*5)
    state.setdefault('quality_fertilizer', 0)
    state.setdefault('quality_fertilizer_rows', [False]*5)
    state.setdefault('attendance_last_date', None)
    state.setdefault('attendance_streak', 0)
    state.setdefault('mine', None)
    for crop in CROPS:
        state['seeds'].setdefault(crop, 0)
    return state


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
            prepare_state(state)
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
        return prepare_state(json.loads(row[0]))

    def round_seed_packs_once(self):
        """기존 씨앗을 5개 단위로 올림하는 일회성 보정. 보상 재지급을 방지합니다."""
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS migrations (id TEXT PRIMARY KEY)')
            key = 'seed-packs-of-five-v1'
            if db.execute('SELECT 1 FROM migrations WHERE id=?', (key,)).fetchone():
                return 0, 0
            changed, added = 0, 0
            for uid, raw in db.execute('SELECT id,state FROM players').fetchall():
                state = prepare_state(json.loads(raw))
                extra = 0
                for crop in CROPS:
                    count = state['seeds'][crop]
                    rounded = ((count+4)//5)*5
                    extra += rounded-count
                    state['seeds'][crop] = rounded
                if extra:
                    state['version'] += 1
                    db.execute('UPDATE players SET state=? WHERE id=?', (json.dumps(state), uid))
                    changed += 1
                    added += extra
            db.execute('INSERT INTO migrations VALUES(?)', (key,))
            return changed, added

    def act(self, user, version, operation, action, **args):
        with self.transaction() as db:
            row = db.execute('SELECT state FROM players WHERE id=?', (str(user),)).fetchone()
            if not row:
                raise GameError('먼저 /농장으로 시작해 주세요.')
            s = prepare_state(json.loads(row[0]))
            if db.execute('SELECT 1 FROM operations WHERE id=?', (str(operation),)).fetchone():
                raise GameError('이미 처리된 요청이에요. /농장에서 확인해 주세요.')
            if s['version'] != version:
                raise GameError('농장이 변경되었어요. /농장을 다시 열어 주세요.')
            message = self._apply(s, action, **args)
            s['version'] += 1
            db.execute('UPDATE players SET state=? WHERE id=?', (json.dumps(s), str(user)))
            db.execute('INSERT INTO operations VALUES(?,?)', (str(operation), self.clock()))
            return s, message

    def _apply(self, s, action, crop=None, quantity=1, plot=None, row=None, fertilizer_kind='growth', mineral=None):
        if action.startswith('mine_'):
            try:
                return apply_mining(s, action, self.clock(), self.rng, mineral=mineral)
            except MiningError as error:
                raise GameError(str(error)) from error
        if crop is not None and crop not in CROPS:
            raise GameError('알 수 없는 작물이에요.')
        if action == 'checkin':
            today, claimed, streak = attendance_status(s, self.clock())
            if claimed:
                raise GameError('오늘 출석 보상은 이미 받았어요. 한국 시간 내일 0시 이후 다시 받아 주세요.')
            daily = CONFIG['attendance_daily_gold']
            bonus = CONFIG['attendance_bonus_gold'] if streak % CONFIG['attendance_bonus_days'] == 0 else 0
            s['attendance_last_date'] = today
            s['attendance_streak'] = streak
            s['gold'] += daily + bonus
            lines = [f'📅 출석 완료! 연속 {streak}일째 · +{daily}골드']
            if bonus:
                lines.append(f"🎁 연속 {CONFIG['attendance_bonus_days']}일 달성 보너스! +{bonus}골드")
            lines.append(f"💰 현재 {s['gold']:,}골드 · 다음 출석은 한국 시간 내일 0시부터")
            return '\n'.join(lines)
        if action in ('buy_fertilizer', 'buy_quality_fertilizer'):
            if type(quantity) is not int or not 1 <= quantity <= 999:
                raise GameError('비료는 1~999개까지 구매할 수 있어요.')
            quality = action == 'buy_quality_fertilizer'
            inventory = 'quality_fertilizer' if quality else 'fertilizer'
            price = CONFIG[inventory+'_price'] * quantity
            self.pay(s, price)
            s[inventory] += quantity
            return f"{'✨ 품질' if quality else '🌿 성장'} 비료 {quantity}개 구매 · {price}골드"
        if action in ('reserve_fertilizer', 'cancel_fertilizer'):
            if type(row) is not int or not 0 <= row < s['rows']:
                raise GameError('🔒 열린 밭 한 줄을 선택해 주세요.')
            if action == 'cancel_fertilizer':
                if not (s['fertilizer_rows'][row] or s['quality_fertilizer_rows'][row]):
                    raise GameError('이 밭에는 예약한 비료가 없어요.')
                s['fertilizer_rows'][row] = False
                s['quality_fertilizer_rows'][row] = False
                return f'🌿 {row+1}번 밭 비료 자동 사용을 껐어요. 이미 심은 작물의 효과는 유지돼요.'
            if fertilizer_kind not in ('growth', 'quality'):
                raise GameError('비료 종류를 선택해 주세요.')
            inventory = 'quality_fertilizer' if fertilizer_kind == 'quality' else 'fertilizer'
            reservations = inventory+'_rows'
            if s[reservations][row]:
                raise GameError('이미 이 비료를 자동 사용 중이에요.')
            s['fertilizer_rows'][row] = False
            s['quality_fertilizer_rows'][row] = False
            s[reservations][row] = True
            label = '✨ 품질' if fertilizer_kind == 'quality' else '🌿 성장'
            return f'{label} 비료 사용 중 ({s[inventory]}개 남음) · {row+1}번 밭\n매번 심기에 성공하면 1개 소모해요. 재고가 없으면 비료 없이 심고, 재구매하면 자동 재개해요.'
        if action == 'buy':
            if crop is None or type(quantity) is not int or not 1 <= quantity <= 999:
                raise GameError('씨앗은 1~999개까지 구매할 수 있어요.')
            if s['rows'] < CROPS[crop].get('required_rows', 1):
                raise GameError(f"🔒 {CROPS[crop]['name']}은 {CROPS[crop]['required_rows']}번 밭 개방 후 구매할 수 있어요.")
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
            unlocked = ' · '.join(c['emoji']+' '+c['name'] for c in CROPS.values() if c.get('required_rows',1) == s['rows'])
            notice = f"{s['rows']}번 밭 개방! · {price}골드\n🌱 새 작물 개방: {unlocked}"
            if s['rows'] == 2:
                notice += '\n⛏️ 이제 탄광을 갈 수 있습니다. /탄광으로 방문해 보세요!'
            return notice
        if action == 'scarecrow':
            if s['scarecrow']:
                raise GameError('이미 허수아비가 있어요.')
            self.pay(s, CONFIG['scarecrow_price'])
            s['scarecrow'] = True
            return '허수아비 설치 완료! 이제 새로 심는 작물을 보호해요.'
        if action == 'plant':
            if crop is None:
                raise GameError('작물을 선택해 주세요.')
            if s['rows'] < CROPS[crop].get('required_rows', 1):
                raise GameError(f"🔒 {CROPS[crop]['name']}은 {CROPS[crop]['required_rows']}번 밭 개방 후 심을 수 있어요.")
            if plot is not None and (type(plot) is not int or not 0 <= plot < s['rows']*5):
                raise GameError('열린 밭을 선택해 주세요.')
            targets = [plot] if plot is not None else list(range(s['rows']*5))
            if row is not None:
                if type(row) is not int or not 0 <= row < s['rows']:
                    raise GameError('🔒 잠긴 밭이에요. 다음 줄 확장으로 먼저 열어 주세요.')
                targets = list(range(row*5, row*5+5))
            targets = [i for i in targets if s['plots'][i] is None]
            if not targets:
                raise GameError('밭에 이미 작물이 심어져있어요.')
            if s['seeds'][crop] < len(targets):
                raise GameError(f"씨앗 {len(targets)}개가 필요해요. 현재 {s['seeds'][crop]}개예요.")
            s['seeds'][crop] -= len(targets)
            now = self.clock()
            fertilized_rows, quality_rows, skipped = set(), set(), []
            growth_left, quality_left = s['fertilizer'], s['quality_fertilizer']
            for r in sorted({i//5 for i in targets}):
                if s['quality_fertilizer_rows'][r]:
                    if quality_left:
                        quality_rows.add(r)
                        quality_left -= 1
                    else:
                        skipped.append(r+1)
                elif s['fertilizer_rows'][r]:
                    if growth_left:
                        fertilized_rows.add(r)
                        growth_left -= 1
                    else:
                        skipped.append(r+1)
            for i in targets:
                fertilized = i//5 in fertilized_rows
                ready = now + growth_seconds(crop, fertilized)
                s['plots'][i] = dict(crop=crop, ready=ready,
                                     planted_at=now, growth_seconds=growth_seconds(crop, fertilized),
                                     fertilized=fertilized,
                                     sale_multiplier=CONFIG['quality_fertilizer_sale_multiplier'] if i//5 in quality_rows else 1,
                                     bird=(crop not in CONFIG.get('bird_exempt_crops',[]) and not s['scarecrow'] and self.rng.random() < CONFIG['bird_probability']))
            s['fertilizer'] -= len(fertilized_rows)
            s['quality_fertilizer'] -= len(quality_rows)
            message = f"{CROPS[crop]['name']} {len(targets)}칸에 심었어요."
            if fertilized_rows:
                message += f'\n🌿 비료 {len(fertilized_rows)}개 사용 · 해당 밭 성장 시간 20% 감소'
            if quality_rows:
                message += f'\n✨ 품질 비료 {len(quality_rows)}개 사용 · 해당 밭 작물 판매가 10% 증가'
            if skipped:
                message += '\n비료 부족: '+', '.join(f'{r}번 밭' for r in skipped)+'은 비료 없이 심었어요. 자동 사용 설정은 유지돼요.'
            return message
        if action == 'harvest':
            count = 0
            previous_records = s['records'].copy()
            harvested = {}
            for i, p in enumerate(s['plots']):
                if p is None or p['ready'] > self.clock():
                    continue
                c = CROPS[p['crop']]
                # 0.70~1.30배 90%, 1.30~2.00배 9%, 2.00~3.00배 1%.
                roll = self.rng.random()
                lo, hi = (0.7, 1.3) if roll < .9 else (1.3, 2) if roll < .99 else (2, 3)
                weight = round(c['base_weight'] * self.rng.uniform(lo, hi))
                value = max(1, round(c['base_sale'] * weight / c['base_weight'] *
                                     (CONFIG['bird_value_multiplier'] if p['bird'] else 1) * p.get('sale_multiplier', 1)))
                s['bag'].append(dict(crop=p['crop'], weight=weight, value=value, bird=p['bird']))
                s['records'][p['crop']] = max(s['records'].get(p['crop'], 0), weight)
                harvested[p['crop']] = harvested.get(p['crop'], 0) + 1
                s['plots'][i] = None
                count += 1
            if not count:
                raise GameError('아직 수확할 작물이 없어요.')
            summary = ' · '.join(f"{CROPS[key]['emoji']} {CROPS[key]['name']} {amount}개" for key, amount in harvested.items())
            lines = [f'🧺 수확 완료! {summary}']
            for key in harvested:
                c = CROPS[key]
                old, best = previous_records.get(key, 0), s['records'][key]
                if old == 0:
                    lines.append(f"🌱 첫 기록! {c['emoji']} {c['name']} **{best:,}g**")
                elif best > old:
                    lines.append(f"✨ 개인 신기록! {c['emoji']} {c['name']} **{best:,}g** · 이전 {old:,}g")
            return '\n'.join(lines)
        if action == 'sell':
            items = [x for x in s['bag'] if crop is None or x['crop'] == crop]
            if not items:
                raise GameError('판매할 수확물이 없어요.')
            value = sum(x['value'] for x in items)
            s['bag'] = [x for x in s['bag'] if crop is not None and x['crop'] != crop]
            s['gold'] += value
            s['revenue'] += value
            lines = ['🧺 판매 완료']
            for key, c in CROPS.items():
                sold = [x for x in items if x['crop'] == key]
                if sold:
                    lines.append(f"{c['emoji']} {c['name']} {len(sold)}개 · +{sum(x['value'] for x in sold):,}골드")
            lines.append(f'총 {len(items)}개 · +{value:,}골드')
            return '\n'.join(lines)
        raise GameError('알 수 없는 작업이에요.')

    @staticmethod
    def pay(s, price):
        if s['gold'] < price:
            raise GameError(f"{price}골드가 필요해요. 현재 {s['gold']}골드예요.")
        s['gold'] -= price

    def ranking(self, guild=None, crop=None, mineral=None, cumulative=False):
        with self.connect() as db:
            rows = db.execute('SELECT p.id,p.name,p.state FROM players p' +
                (' JOIN members m ON p.id=m.user WHERE m.guild=?' if guild is not None else ''),
                (str(guild),) if guild is not None else ()).fetchall()
        result = []
        for uid, name, raw in rows:
            s = json.loads(raw)
            score = (s.get('mine') or {}).get('records',{}).get(mineral,0) if mineral else s['records'].get(crop, 0) if crop else s.get('revenue',0)+(s.get('mine') or {}).get('revenue',0) if cumulative else s['gold']
            if score > 0 or (not crop and not mineral):
                result.append((uid, name, score))
        return sorted(result, key=lambda x: (-x[2], int(x[0])))[:10]
