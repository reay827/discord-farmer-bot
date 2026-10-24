"""Lazy, transactional mining rules. No timers, Discord or database imports."""
import json
from pathlib import Path

MINING = json.loads(Path(__file__).with_name('mining_balance.json').read_text(encoding='utf-8'))
MINERALS = MINING['minerals']


class MiningError(ValueError):
    pass


def kg(weight_mg):
    return f'{weight_mg / 1_000_000:.6f}'.rstrip('0').rstrip('.') + 'kg'


def capacity(m):
    return MINING['bag_capacities'][m['bag_level']]


def interval(m):
    return MINING['worker_interval_seconds'][m['workers']-1]


def open_mine(s, now):
    if s['rows'] < MINING['unlock_farm_rows']:
        raise MiningError('🔒 2번 밭을 개방하면 탄광을 이용할 수 있어요.')
    fresh = s.get('mine') is None
    if fresh:
        s['mine'] = dict(workers=1, pickaxe=0, bag_level=0, bag=[], inventory={},
                         records={}, revenue=0, last=now, progress=0.0, pity_seconds=0.0)
    return fresh


def settle(s, now, rng):
    m = s.get('mine')
    if m is None or now <= m['last']:
        return
    elapsed = now - m['last']
    m['last'] = now
    if len(m['bag']) >= capacity(m):
        m['progress'] = 0.0
        return
    step = interval(m)
    to_next = (1-m['progress'])*step
    top = m['pickaxe'] == 3
    while elapsed + 1e-8 >= to_next and len(m['bag']) < capacity(m):
        elapsed = max(0.0, elapsed-to_next)
        if top:
            m['pity_seconds'] += to_next
        if top and m['pity_seconds'] + 1e-8 >= MINING['pity_seconds']:
            key = 'diamond'
        else:
            keys = MINING['mineral_order']
            probs = MINING['probabilities'][MINING['pickaxes'][m['pickaxe']]]
            key = rng.choices(keys, weights=probs, k=1)[0]
        c = MINERALS[key]
        weight = max(1, round(c['base_weight_kg']*1_000_000*rng.uniform(.8,1.2)))
        value = c['base_sale'] if c['sale_mode'] == 'fixed_per_item' else max(1,round(c['base_sale']*weight/(c['base_weight_kg']*1_000_000)))
        m['bag'].append(dict(mineral=key, weight_mg=weight, value=value))
        m['records'][key] = max(m['records'].get(key,0), weight)
        if key == 'diamond':
            m['pity_seconds'] = 0.0
        m['progress'] = 0.0
        to_next = step
    if len(m['bag']) < capacity(m):
        m['progress'] += elapsed/step
        if top:
            m['pity_seconds'] += elapsed


def pay(s, amount):
    if s['gold'] < amount:
        raise MiningError(f"{amount:,}골드가 필요해요. 현재 {s['gold']:,}골드예요.")
    s['gold'] -= amount


def apply_mining(s, action, now, rng, mineral=None):
    fresh = open_mine(s, now)
    m = s['mine']
    settle(s, now, rng)
    if action == 'mine_open':
        return '⛏️ 탄광 개방! 나무 곡괭이·노동자 1명·가방 60칸을 받았어요. 자동 채굴을 시작해요.' if fresh else '⛏️ 채굴 현황을 갱신했어요.'
    if action == 'mine_collect':
        if not m['bag']:
            raise MiningError('아직 받을 광물이 없어요.')
        lines = ['🎒 광물을 보관함으로 옮겼어요. 채굴은 계속돼요.']
        for key,c in MINERALS.items():
            items = [x for x in m['bag'] if x['mineral'] == key]
            if not items:
                continue
            stock = m['inventory'].setdefault(key,dict(count=0,value=0,best_mg=0))
            stock['count'] += len(items)
            stock['value'] += sum(x['value'] for x in items)
            stock['best_mg'] = max(stock['best_mg'],max(x['weight_mg'] for x in items))
            lines.append(f"{c['emoji']} {c['name']} {len(items)}개 · 최대 {kg(max(x['weight_mg'] for x in items))}")
            if key == 'diamond':
                lines.append(f"💎 잭팟! 다이아몬드 판매 예상 {sum(x['value'] for x in items):,}골드!")
        m['bag'] = []
        return '\n'.join(lines)
    if action == 'mine_sell':
        if mineral is not None and mineral not in MINERALS:
            raise MiningError('광물을 선택해 주세요.')
        keys = [mineral] if mineral else list(MINERALS)
        sold = [(k,m['inventory'][k]) for k in keys if m['inventory'].get(k,{}).get('count',0)]
        if not sold:
            raise MiningError('보관함에 판매할 광물이 없어요. 먼저 광물 받기를 눌러 주세요.')
        total = sum(x['value'] for _,x in sold)
        lines = ['🧺 광물 판매 완료']+[f"{MINERALS[k]['emoji']} {MINERALS[k]['name']} {x['count']}개 · +{x['value']:,}G" for k,x in sold]
        for k,_ in sold:
            del m['inventory'][k]
        s['gold'] += total
        m['revenue'] += total
        return '\n'.join(lines+[f'총 +{total:,}골드'])
    if action == 'mine_hire':
        if m['workers'] >= 5:
            raise MiningError('이미 노동자 5명을 고용했어요.')
        price = MINING['worker_hire_costs'][m['workers']]
        pay(s,price)
        m['workers'] += 1
        return f"👷 노동자 {m['workers']}명 · {price:,}G 사용. 전체 {interval(m)}초마다 광물 1개를 캐요."
    if action == 'mine_bag_upgrade':
        if m['bag_level'] >= len(MINING['bag_capacities'])-1:
            raise MiningError('이미 최대 가방이에요.')
        pay(s,MINING['bag_upgrade_costs'][m['bag_level']+1])
        m['bag_level'] += 1
        return f'🎒 탄광 가방 {capacity(m)}칸으로 확장했어요.'
    if action == 'mine_pickaxe':
        if m['pickaxe'] >= 3:
            raise MiningError('이미 다이아 곡괭이예요.')
        tier = m['pickaxe']+1
        pay(s,MINING['pickaxe_upgrade_costs'][MINING['pickaxes'][tier]])
        m['pickaxe'] = tier
        return f"⛏️ {MINING['pickaxe_names'][tier]}로 업그레이드했어요. 이후 생산부터 적용돼요."
    raise MiningError('알 수 없는 탄광 작업이에요.')
