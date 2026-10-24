import asyncio
import logging
import math
import os
import time
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

from mining import MINING
from features import install_features

from game import CONFIG, CROPS, Game, GameError, grade, attendance_status, growth_seconds, KST

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
logging.basicConfig(level=logging.INFO)
db_path = Path(os.getenv('DATABASE_PATH', 'data/farm.db'))
game = Game(db_path if db_path.is_absolute() else ROOT / db_path)


def duration_text(seconds):
    seconds = int(seconds)
    hours, remaining = divmod(seconds, 3600)
    minutes, seconds = divmod(remaining, 60)
    parts = ([f'{hours}시간'] if hours else []) + ([f'{minutes}분'] if minutes else []) + ([f'{seconds}초'] if seconds else [])
    return ' '.join(parts) or '0초'


def crop_times(fertilizer=False):
    return ' · '.join(f"{c['name']} {duration_text(c['seconds'])}" +
        (f" → {duration_text(c['seconds']*CONFIG['fertilizer_growth_multiplier'])}" if fertilizer else '') for c in CROPS.values())


def countdown_text(seconds):
    hours, rest = divmod(max(0, math.ceil(seconds)), 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours:02d}시간 {minutes:02d}분 {seconds:02d}초 남음'


def growth_update_delay(remaining, public=False):
    interval = (5 if public else 1) if remaining <= 60 else 15 if remaining <= 3600 else 60
    # 구간이 바뀌거나 성장이 끝나는 시각에는 기다리지 않고 바로 다시 계산합니다.
    boundary = remaining-3600 if remaining > 3600 else remaining-60 if remaining > 60 else remaining
    return min(interval, remaining, boundary) + .01


def farm_embed(s, notice='', public_name=None):
    grid = []
    for row in range(5):
        cells = []
        for col in range(5):
            p = s['plots'][row*5+col]
            cells.append('🔒' if row >= s['rows'] else '🟫' if p is None else
                         CROPS[p['crop']]['emoji'] if p['ready'] <= time.time() else '🌱')
        fertilizer_marker = '　🌿' if s.get('fertilizer_rows', [False]*5)[row] else ''
        if s.get('quality_fertilizer_rows', [False]*5)[row]:
            fertilizer_marker = '　✨'
        grid.append(('1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣')[row] + '　' + ' '.join(cells) + fertilizer_marker)
    title = f'🌿 {public_name}님의 농장' if public_name is not None else '🌿 나의 작은 농장'
    embed = discord.Embed(title=title, description=notice+'\n\n'+'\n'.join(grid), color=0x78AD70)
    embed.add_field(name='지갑', value=f"{s['gold']:,}골드")
    embed.add_field(name='농장 누적 판매', value=f"{s['revenue']:,}골드")
    embed.add_field(name='밭 / 허수아비', value=f"{s['rows']*5}/25칸 · {'설치됨' if s['scarecrow'] else '없음'}")
    reserved = [f"{('1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣')[i]} 다음 심기 성장 시간 20% 감소" for i, active in enumerate(s.get('fertilizer_rows', [])) if active]
    if public_name is None or reserved:
        embed.add_field(name=f"🌿 성장 비료 · 보유 {s.get('fertilizer', 0)}개", value='\n'.join(reserved) or '비료 사용 버튼에서 빈 밭에 예약할 수 있어요.', inline=False)
    quality_reserved = [f'{i+1}번 밭 · 다음 심기 판매가 10% 증가' for i,active in enumerate(s.get('quality_fertilizer_rows', [])) if active]
    if public_name is None or quality_reserved:
        embed.add_field(name=f"✨ 품질 비료 · 보유 {s.get('quality_fertilizer', 0)}개", value='\n'.join(quality_reserved) or '호박 한 밭부터 기본 무게 기준 이득 · 성장 비료와 중복 불가', inline=False)
    if public_name is None:
        _, claimed, streak = attendance_status(s, time.time())
        status = f'✅ 오늘 수령 완료 · 연속 {streak}일' if claimed else f"오늘 +{CONFIG['attendance_daily_gold']}골드 받기 · 출석 시 연속 {streak}일"
        embed.add_field(name='📅 출석 보상', value=status, inline=False)
    growing = []
    now = time.time()
    for row in range(5):
        groups = {}
        for p in s['plots'][row*5:row*5+5]:
            if p and p['ready'] > now:
                # 한 번의 심기에서 생긴 미세한 시각 차이는 같은 완료 시각으로 표시합니다.
                key = (p['crop'], p['ready'])
                groups[key] = p.get('growth_seconds', growth_seconds(p['crop'], p.get('fertilized', False)))
        for (crop, ready), seconds in groups.items():
            growing.append(f"{('1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣')[row]} {CROPS[crop]['name']} · {countdown_text(ready-now)}")
    if growing:
        embed.add_field(name='성장 완료까지', value='\n'.join(growing), inline=False)
    embed.set_footer(text='🟫 빈 밭 · 🌱 성장 중 · 작물 그림은 수확 가능 | ' +
                     ('성장 완료 시 자동 갱신 · 버튼으로 각자의 농장을 열어 보세요.' if public_name is not None else
                      '개인 화면은 마지막 조작 후 14분간 유효 · 만료되면 내 농장 열기를 다시 누르세요.'))
    return embed


# 공개 메시지는 일반 봇 연결로, 개인 메시지는 각 상호작용 토큰으로 갱신합니다.
public_farms = {}


async def open_private_farm(interaction):
    state, fresh = visit(interaction)
    intro = '🌱 첫 농장에 오신 것을 환영해요! 당근 씨앗 5개와 100골드를 받았어요.' if fresh else ''
    view = FarmView(interaction.user.id, state)
    await interaction.response.send_message(embed=farm_embed(state, intro), view=view, ephemeral=True)
    view.watch(await interaction.original_response())


async def update_public_farms(user):
    for key, panel in list(public_farms.items()):
        if key[0] == user:
            await panel.refresh()


class PublicFarmView(discord.ui.View):
    def __init__(self, owner=None, name=None, message=None):
        super().__init__(timeout=None)
        self.owner, self.name, self.message = owner, name, message
        self.task = None
        self.lock = asyncio.Lock()

    @discord.ui.button(label='내 농장 열기', emoji='🌿', style=discord.ButtonStyle.green, custom_id='farm:open-private')
    async def open_farm(self, interaction, button):
        # 구경하던 농장의 주인이 아닌, 누른 사람의 농장을 엽니다.
        await open_private_farm(interaction)

    async def refresh(self):
        async with self.lock:
            try:
                state = game.read(self.owner)
                await self.message.edit(embed=farm_embed(state, public_name=self.name), view=self)
            except (discord.NotFound, discord.Forbidden):
                public_farms.pop((self.owner, self.message.channel.id), None)
                self.stop()
                return
            except discord.HTTPException:
                logging.warning('공개 농장 갱신 실패')
                return
            if self.task and self.task is not asyncio.current_task():
                self.task.cancel()
            self.task = asyncio.create_task(self.growth_loop())

    async def growth_loop(self):
        try:
            while True:
                state = game.read(self.owner)
                deadlines = [p['ready'] for p in state['plots'] if p and p['ready'] > time.time()]
                if not deadlines:
                    return
                await asyncio.sleep(growth_update_delay(max(0, min(deadlines)-time.time()), public=True))
                async with self.lock:
                    state = game.read(self.owner)
                    await self.message.edit(embed=farm_embed(state, public_name=self.name))
        except (discord.NotFound, discord.Forbidden):
            public_farms.pop((self.owner, self.message.channel.id), None)
            self.stop()
        except discord.HTTPException:
            logging.warning('공개 농장 성장 갱신 실패')

    def stop(self):
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
        super().stop()


class FarmView(discord.ui.View):
    def __init__(self, user, state):
        super().__init__(timeout=840)
        self._growth_task = None
        self._edit_lock = asyncio.Lock()
        self._deadlines = sorted({p['ready'] for p in state['plots'] if p and p['ready'] > time.time()})
        self.user, self.version = user, state['version']
        self.crop, self.row = 'carrot', 0
        self.open_rows = state['rows']
        self.crop_menu.options = [discord.SelectOption(label=f"{c['name']} · 씨앗 {state['seeds'][k]}개", value=k,
            emoji='🔒' if state['rows'] < c.get('required_rows',1) else c['emoji'],
            description=f"{c.get('required_rows',1)}번 밭 개방 필요" if state['rows'] < c.get('required_rows',1) else f"성장 {duration_text(c['seconds'])}", default=k=='carrot') for k,c in CROPS.items()]
        self.plot_menu.options = [discord.SelectOption(label='열린 밭 전체', value='all', emoji='🌾', description='열린 모든 줄의 빈 칸에 심기')]
        for i in range(5):
            opened = i < state['rows']
            empty = sum(p is None for p in state['plots'][i*5:i*5+5])
            description = f'가로 한 줄 5칸 · 빈 칸 {empty}개' if opened else '잠김 · 위에서부터 줄 확장 구매로 개방'
            self.plot_menu.options.append(discord.SelectOption(label=f"{i+1}번 밭{' · 잠김' if not opened else ''}", value=str(i), emoji='🟫' if opened else '🔒', description=description, default=i==0))
        self.expand.label = '최대 확장 완료' if state['rows'] == 5 else f"다음 밭 확장 · {CONFIG['expansion_prices'][state['rows']-1]}골드"
        self.expand.disabled = state['rows'] == 5
        _, claimed, _ = attendance_status(state, time.time())
        self.checkin.disabled = claimed
        self.checkin.label = '오늘 출석 완료' if claimed else '출석 보상 받기'

    def watch(self, message):
        # 개인 응답 토큰이 유효한 동안만 자동 갱신합니다. 조작 때마다 새 토큰으로 교체합니다.
        if self._growth_task:
            self._growth_task.cancel()
        if self._deadlines:
            self._growth_task = asyncio.create_task(self._update_growth(message))

    async def _update_growth(self, message):
        try:
            while True:
                state = game.read(self.user)
                deadlines = [p['ready'] for p in state['plots'] if p and p['ready'] > time.time()]
                if not deadlines:
                    return
                await asyncio.sleep(growth_update_delay(max(0,min(deadlines)-time.time())))
                if self.is_finished():
                    return
                async with self._edit_lock:
                    if self.is_finished():
                        return
                    state = game.read(self.user)
                    await message.edit(embed=farm_embed(state))
        except (discord.NotFound, discord.Forbidden):
            self.stop()
        except discord.HTTPException:
            logging.warning('농장 자동 갱신 실패: /농장을 다시 열면 재개됩니다.')

    def stop(self):
        if self._growth_task and self._growth_task is not asyncio.current_task():
            self._growth_task.cancel()
        super().stop()

    async def on_timeout(self):
        self.stop()

    async def replace(self, interaction, state, notice=''):
        view = FarmView(self.user, state)
        async with self._edit_lock:
            await interaction.response.edit_message(embed=farm_embed(state, notice), view=view)
            view.watch(await interaction.original_response())
            self.stop()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user:
            await interaction.response.send_message('자신의 /농장을 열어 주세요.', ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error, item):
        logging.error('농장 화면 오류', exc_info=(type(error), error, error.__traceback__))
        if not interaction.response.is_done():
            await interaction.response.send_message('처리 중 오류가 발생했어요. /농장을 다시 열어 주세요.', ephemeral=True)

    async def action(self, interaction, action, **kwargs):
        try:
            s, msg = game.act(self.user, self.version, interaction.id, action, **kwargs)
        except GameError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await self.replace(interaction, s, msg)
        await update_public_farms(self.user)

    @discord.ui.select(placeholder='심을 작물 선택', row=0)
    async def crop_menu(self, interaction, select):
        c = CROPS[select.values[0]]
        if self.open_rows < c.get('required_rows',1):
            await interaction.response.send_message(f"🔒 {c['name']}은 {c['required_rows']}번 밭 개방 후 사용할 수 있어요.", ephemeral=True)
            return
        self.crop = select.values[0]
        for option in select.options:
            option.default = option.value == self.crop
        await interaction.response.edit_message(view=self)
        self.watch(await interaction.original_response())

    @discord.ui.select(placeholder='심을 밭 선택 · 한 밭은 가로 한 줄 5칸', row=1)
    async def plot_menu(self, interaction, select):
        row = None if select.values[0] == 'all' else int(select.values[0])
        if row is not None and row >= self.open_rows:
            await interaction.response.send_message(f'🔒 {row+1}번 밭은 아직 잠겨 있어요. 다음 밭 확장 버튼으로 순서대로 구매해 주세요.', ephemeral=True)
            return
        self.row = row
        for option in select.options:
            option.default = option.value == select.values[0]
        await interaction.response.edit_message(view=self)
        self.watch(await interaction.original_response())

    @discord.ui.button(label='선택한 밭에 심기', style=discord.ButtonStyle.green, row=2)
    async def plant(self, interaction, button):
        await self.action(interaction, 'plant', crop=self.crop, row=self.row)

    @discord.ui.button(label='다 자란 작물 모두 수확', style=discord.ButtonStyle.green, row=2)
    async def harvest(self, interaction, button):
        await self.action(interaction, 'harvest')

    @discord.ui.button(label='수확물 전체 판매', row=2)
    async def sell(self, interaction, button):
        await self.action(interaction, 'sell')

    @discord.ui.button(label='다음 밭 확장', row=3)
    async def expand(self, interaction, button):
        await self.action(interaction, 'expand')

    @discord.ui.button(label='새로고침', row=3)
    async def refresh(self, interaction, button):
        s = game.read(self.user)
        await self.replace(interaction, s)

    @discord.ui.button(label='빈 밭 전체 선택', row=3)
    async def select_all(self, interaction, button):
        self.row = None
        for option in self.plot_menu.options:
            option.default = option.value == 'all'
        await interaction.response.edit_message(view=self)
        self.watch(await interaction.original_response())

    @discord.ui.button(label='상점', emoji='🏪', style=discord.ButtonStyle.primary, row=4)
    async def go_shop(self, interaction, button):
        view = ShopView(self.user, game.read(self.user))
        async with self._edit_lock:
            await interaction.response.edit_message(content=None, embed=view.embed(), view=view)
            self.stop()

    @discord.ui.button(label='비료 사용', emoji='🌿', row=4)
    async def go_fertilizer(self, interaction, button):
        view = FertilizerView(self.user, game.read(self.user))
        async with self._edit_lock:
            await interaction.response.edit_message(embed=view.embed(), view=view)
            self.stop()

    @discord.ui.button(label='출석 보상 받기', emoji='📅', style=discord.ButtonStyle.green, row=4)
    async def checkin(self, interaction, button):
        await self.action(interaction, 'checkin')

    @discord.ui.button(label='탄광', emoji='⛏️', row=4)
    async def go_mine(self, interaction, button):
        async with self._edit_lock:
            if await open_mining(interaction, edit=True):
                self.stop()


class FertilizerView(discord.ui.View):
    def __init__(self, user, state):
        super().__init__(timeout=180)
        self.user, self.version, self.state, self.row = user, state['version'], state, 0
        self.kind = 'growth'
        self.row_menu.options = []
        for i in range(5):
            locked = i >= state['rows']
            reserved = state.get('fertilizer_rows', [False]*5)[i]
            quality = state.get('quality_fertilizer_rows', [False]*5)[i]
            occupied = any(p for p in state['plots'][i*5:i*5+5])
            status = '잠김' if locked else '품질 비료 예약됨' if quality else '성장 비료 예약됨' if reserved else '작물 있음' if occupied else '빈 밭 · 예약 가능'
            self.row_menu.options.append(discord.SelectOption(label=f'{i+1}번 밭 · {status}', value=str(i), emoji='🔒' if locked else '✨' if quality else '🌿' if reserved else '🟫', default=i==0))

    def embed(self):
        if self.kind == 'quality':
            return discord.Embed(title='✨ 품질 비료 사용', description=
                f"보유 **{self.state.get('quality_fertilizer', 0)}개** · 예약 **{sum(self.state.get('quality_fertilizer_rows', []))}개**\n\n"
                f'선택한 밭: **{self.row+1}번 밭 (가로 한 줄 5칸)**\n'
                '다음 심기의 작물 판매가 **10% 증가** · 심기 성공 시 1개 소모\n'
                '빈 밭에만 예약 가능 · 성장 비료와 중복 불가\n'
                '무게·무게 순위·성장 시간은 그대로예요.\n'
                '호박 한 밭은 기본 무게에서 판매가 +30G, 비료값 20G를 빼면 +10G예요.\n'
                '예약 취소·심기 실패 시 비료는 소모되지 않아요.', color=0xEBCB78)
        reserved = sum(self.state.get('fertilizer_rows', []))
        return discord.Embed(title='🌿 성장 비료 사용', description=
            f"보유 **{self.state.get('fertilizer', 0)}개** · 예약 **{reserved}개**\n\n"
            f'선택한 밭: **{self.row+1}번 밭 (가로 한 줄 5칸)**\n'
            '효과: 다음 심기 성장 시간 **20% 감소**\n'
            + crop_times(fertilizer=True) + '\n\n'
            '빈 밭에만 예약할 수 있어요. 심기에 성공하면 1개 소모돼요.\n'
            '예약을 취소하거나 심기에 실패하면 소모되지 않아요. 무게 확률은 그대로예요.', color=0x78AD70)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user:
            await interaction.response.send_message('자신의 /농장을 열어 주세요.', ephemeral=True)
            return False
        return True

    @discord.ui.select(placeholder='🌿 비료를 사용할 밭 선택', row=0)
    async def row_menu(self, interaction, select):
        row = int(select.values[0])
        if row >= self.state['rows']:
            await interaction.response.send_message('🔒 아직 잠긴 밭이에요.', ephemeral=True)
            return
        self.row = row
        for option in select.options:
            option.default = option.value == str(row)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.select(placeholder='사용할 비료 선택', row=1, options=[
        discord.SelectOption(label='성장 비료', value='growth', emoji='🌿', description='성장 시간 20% 감소', default=True),
        discord.SelectOption(label='품질 비료', value='quality', emoji='✨', description='판매가 10% 증가')])
    async def kind_menu(self, interaction, select):
        self.kind = select.values[0]
        for option in select.options:
            option.default = option.value == self.kind
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def return_farm(self, interaction, state, notice=''):
        view = FarmView(self.user, state)
        await interaction.response.edit_message(embed=farm_embed(state, notice), view=view)
        view.watch(await interaction.original_response())
        self.stop()

    async def apply(self, interaction, action):
        try:
            extra = {'fertilizer_kind': 'quality'} if self.kind == 'quality' else {}
            state, notice = game.act(self.user, self.version, interaction.id, action, row=self.row, **extra)
        except GameError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await self.return_farm(interaction, state, notice)
        await update_public_farms(self.user)

    @discord.ui.button(label='다음 심기에 사용', emoji='🌿', style=discord.ButtonStyle.green, row=2)
    async def reserve(self, interaction, button):
        await self.apply(interaction, 'reserve_fertilizer')

    @discord.ui.button(label='예약 취소', row=2)
    async def cancel(self, interaction, button):
        await self.apply(interaction, 'cancel_fertilizer')

    @discord.ui.button(label='농장으로', emoji='🌿', row=3)
    async def back(self, interaction, button):
        await self.return_farm(interaction, game.read(self.user))


class ShopView(discord.ui.View):
    def __init__(self, user, state, category='seeds', crop='carrot'):
        super().__init__(timeout=180)
        self.user, self.version = user, state['version']
        self.state, self.category, self.crop = state, category, crop
        for option in self.category_menu.options:
            option.default = option.value == category
        if category == 'seeds':
            menu = discord.ui.Select(placeholder='🌱 구매할 씨앗 선택', row=1, options=[
                discord.SelectOption(label=c['name'], value=k, emoji='🔒' if state['rows'] < c.get('required_rows',1) else c['emoji'], default=k==crop,
                                     description=f"{c.get('required_rows',1)}번 밭 개방 필요" if state['rows'] < c.get('required_rows',1) else f"씨앗 {c['seed_price']}G · 성장 {duration_text(c['seconds'])}") for k,c in CROPS.items()])
            async def choose_crop(interaction):
                await self.navigate(interaction, 'seeds', menu.values[0])
            menu.callback = choose_crop
            self.add_item(menu)
            c = CROPS[crop]
            for quantity in (5, 25):
                button = discord.ui.Button(label=f"{quantity}개 구매 · {c['seed_price']*quantity}G", emoji=c['emoji'], row=2, style=discord.ButtonStyle.green, disabled=state['rows'] < c.get('required_rows',1))
                async def callback(interaction, quantity=quantity):
                    await self.buy(interaction, 'buy', crop=self.crop, quantity=quantity)
                button.callback = callback
                self.add_item(button)
        elif category in ('fertilizer', 'quality_fertilizer'):
            for quantity in (1, 5, 10):
                quality = category == 'quality_fertilizer'
                price = CONFIG['quality_fertilizer_price'] if quality else CONFIG['fertilizer_price']
                button = discord.ui.Button(label=f"{quantity}개 구매 · {price*quantity}G", emoji='✨' if quality else '🌿', row=2, style=discord.ButtonStyle.green)
                async def buy_fertilizer(interaction, quantity=quantity):
                    await self.buy(interaction, 'buy_quality_fertilizer' if self.category == 'quality_fertilizer' else 'buy_fertilizer', quantity=quantity)
                button.callback = buy_fertilizer
                self.add_item(button)
        elif category == 'pickaxes':
            tier = (state.get('mine') or {}).get('pickaxe',0)
            button = discord.ui.Button(label='최상위 곡괭이' if tier==3 else f"{MINING['pickaxe_names'][tier+1]} · {MINING['pickaxe_upgrade_costs'][MINING['pickaxes'][tier+1]]:,}G",emoji='⛏️',row=2,disabled=state['rows']<2 or tier==3)
            async def upgrade_pickaxe(interaction):
                if await open_mining(interaction,edit=True,action='mine_pickaxe',version=self.version):
                    self.stop()
            button.callback=upgrade_pickaxe
            self.add_item(button)
        else:
            button = discord.ui.Button(label=f"{'설치 완료' if state['scarecrow'] else '허수아비 구매'} · {CONFIG['scarecrow_price']}G", emoji='🛡️', row=2, disabled=state['scarecrow'])
            async def scarecrow(interaction):
                await self.buy(interaction, 'scarecrow')
            button.callback = scarecrow
            self.add_item(button)

    def embed(self, notice=''):
        embed = discord.Embed(title='🏪 농장 상점', description=notice or '카테고리와 상품을 선택해 주세요.', color=0x78AD70)
        embed.add_field(name='💰 보유 골드', value=f"{self.state['gold']:,}G", inline=False)
        if self.category == 'seeds':
            c = CROPS[self.crop]
            seconds = c['seconds']
            duration = duration_text(seconds)
            embed.add_field(name=f"{c['emoji']} {c['name']} 씨앗", value=f"가격 **{c['seed_price']}G / 개**\n성장 시간 **{duration}**\n보유 씨앗 **{self.state['seeds'][self.crop]}개**\n아래 버튼으로 수량을 선택해 구매하세요.", inline=False)
            if self.state['rows'] < c.get('required_rows',1):
                embed.add_field(name='🔒 아직 잠긴 작물', value=f"**{c['required_rows']}번 밭을 개방하면** 구매하고 심을 수 있어요. 농장에서 다음 밭 확장을 구매해 주세요.", inline=False)
        elif self.category == 'quality_fertilizer':
            embed.add_field(name='✨ 품질 비료', value=f"가격 **{CONFIG['quality_fertilizer_price']}G / 개** · 보유 **{self.state.get('quality_fertilizer', 0)}개**\n한 밭 다음 심기 작물 판매가 **10% 증가**\n호박 한 밭부터 기본 무게 기준 이득이에요.\n농장의 비료 사용에서 종류를 선택해 예약하세요. 성장 비료와 중복 불가.", inline=False)
        elif self.category == 'fertilizer':
            embed.add_field(name='🌿 성장 비료', value=f"가격 **{CONFIG['fertilizer_price']}G / 개** · 보유 **{self.state.get('fertilizer', 0)}개**\n빈 밭 한 줄의 다음 심기 성장 시간 **20% 감소**\n농장의 🌿 비료 사용 버튼에서 예약해 주세요.\n심기에 성공할 때 1개 소모 · 중복 적용 불가", inline=False)
        elif self.category == 'pickaxes':
            tier=(self.state.get('mine') or {}).get('pickaxe',0)
            embed.add_field(name='⛏️ 공용 곡괭이',value=f"현재: {MINING['pickaxe_names'][tier]}\n나무 무료 → 돌 3,000G → 철 15,000G → 다이아 80,000G\n순서대로 영구 업그레이드 · 이후 생산부터 새 광물 확률 적용\n{'🔒 2번 밭 개방 필요' if self.state['rows']<2 else '탄광에 가서 현재 광물을 받고 장비를 확인하세요.'}",inline=False)
        else:
            embed.add_field(name='🛡️ 허수아비', value=f"가격 **{CONFIG['scarecrow_price']}G** · 영구 적용\n구매 후 새로 심는 작물부터 새 피해를 막아요.\n상태: {'✅ 설치됨' if self.state['scarecrow'] else '미설치'}", inline=False)
        return embed

    @discord.ui.select(placeholder='🗂️ 카테고리 선택', row=0, options=[
        discord.SelectOption(label='씨앗', value='seeds', emoji='🌱', description='감자부터 목화까지 · 성장 시간순 12종'),
        discord.SelectOption(label='성장 비료', value='fertilizer', emoji='🌿', description='일회용 · 성장 시간 20% 단축'),
        discord.SelectOption(label='품질 비료', value='quality_fertilizer', emoji='✨', description='일회용 · 판매가 10% 증가'),
        discord.SelectOption(label='농장 용품', value='supplies', emoji='🧰', description='농장을 지켜 주는 허수아비'),
        discord.SelectOption(label='곡괭이', value='pickaxes', emoji='⛏️', description='돌 · 철 · 다이아 곡괭이 영구 업그레이드')])
    async def category_menu(self, interaction, select):
        await self.navigate(interaction, select.values[0], self.crop)

    @discord.ui.button(label='농장으로', emoji='🌿', style=discord.ButtonStyle.primary, row=4)
    async def go_farm(self, interaction, button):
        state = game.read(self.user)
        view = FarmView(self.user, state)
        await interaction.response.edit_message(content=None, embed=farm_embed(state), view=view)
        view.watch(await interaction.original_response())
        self.stop()

    async def navigate(self, interaction, category, crop):
        view = ShopView(self.user, game.read(self.user), category, crop)
        await interaction.response.edit_message(content=None, embed=view.embed(), view=view)
        self.stop()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user:
            await interaction.response.send_message('자신의 /상점을 열어 주세요.', ephemeral=True)
            return False
        return True

    async def buy(self, interaction, action, **kwargs):
        try:
            s, msg = game.act(self.user, self.version, interaction.id, action, **kwargs)
        except GameError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        view = ShopView(self.user, s, self.category, self.crop)
        await interaction.response.edit_message(content=None, embed=view.embed(msg), view=view)
        self.stop()
        await update_public_farms(self.user)


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(PublicFarmView())
        guild_ids = os.getenv('TEST_GUILD_ID', '')
        if guild_ids.strip():
            for guild_id in dict.fromkeys(g.strip() for g in guild_ids.split(',') if g.strip()):
                guild = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild)
                commands = await self.tree.sync(guild=guild)
                logging.info('Guild %s: %s commands registered', guild_id, len(commands))
        else:
            await self.tree.sync()


bot = Bot()


def visit(interaction):
    return game.visit(interaction.user.id, interaction.user.display_name, interaction.guild_id)


@bot.tree.command(name='농장', description='농장을 열고 심기·수확·판매·확장을 해요')
async def farm(interaction: discord.Interaction):
    s, fresh = visit(interaction)
    intro = '🌱 첫 농장이에요! 내 농장 열기 버튼을 눌러 첫 씨앗을 심어보세요.' if fresh else ''
    if s['rows']>=2 and not s.get('mine'):
        intro += '\n⛏️ 이제 탄광을 갈 수 있습니다. /탄광 또는 개인 농장의 탄광 버튼을 눌러 주세요.'
    view = PublicFarmView(interaction.user.id, interaction.user.display_name)
    await interaction.response.send_message(embed=farm_embed(s, intro, public_name=interaction.user.display_name), view=view)
    message = await interaction.original_response()
    view.message = interaction.channel.get_partial_message(message.id)
    key = (interaction.user.id, interaction.channel.id)
    previous = public_farms.get(key)
    if previous:
        previous.stop()
    public_farms[key] = view
    view.task = asyncio.create_task(view.growth_loop())


@bot.tree.command(name='상점', description='씨앗과 영구 허수아비를 구매해요')
async def shop(interaction: discord.Interaction):
    s, _ = visit(interaction)
    view = ShopView(interaction.user.id, s)
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


@bot.tree.command(name='가방', description='씨앗, 수확물, 개인 최고 기록을 확인해요')
async def bag(interaction: discord.Interaction):
    s, _ = visit(interaction)
    embed = discord.Embed(title='🎒 나의 가방', color=0x78AD70)
    embed.add_field(name='🌿 성장 비료', value=f"보유 {s['fertilizer']}개 · 예약 {sum(s['fertilizer_rows'])}개\n농장의 비료 사용 버튼에서 예약·취소할 수 있어요.", inline=False)
    embed.add_field(name='✨ 품질 비료', value=f"보유 {s['quality_fertilizer']}개 · 예약 {sum(s['quality_fertilizer_rows'])}개\n다음 심기 판매가 10% 증가 · 성장 비료와 중복 불가", inline=False)
    for key, c in CROPS.items():
        items = [x for x in s['bag'] if x['crop'] == key]
        best = s['records'].get(key, 0)
        largest = max(items, key=lambda x:x['weight']) if items else None
        info = f"씨앗 {s['seeds'][key]}개 · 수확물 {len(items)}개\n판매 예상 {sum(x['value'] for x in items)}G · 최고 기록 {best}g"
        if largest:
            info += f"\n보유 최대 {largest['weight']}g ({grade(largest['weight'], c['base_weight'])}) · 새 피해 {sum(x['bird'] for x in items)}개"
        embed.add_field(name=c['emoji']+' '+c['name'], value=info, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


CROP_CHOICES = [app_commands.Choice(name=c['name'], value=k) for k,c in CROPS.items()]


@bot.tree.command(name='판매', description='선택한 작물 또는 수확물 전체를 판매해요')
@app_commands.choices(crop=CROP_CHOICES)
async def sell(interaction: discord.Interaction, crop: str = None):
    s, _ = visit(interaction)
    _, msg = game.act(interaction.user.id, s['version'], interaction.id, 'sell', crop=crop)
    await interaction.response.send_message(msg, ephemeral=True)
    await update_public_farms(interaction.user.id)


@bot.tree.command(name='출석', description='매일 출석 골드와 연속 7일 추가 보상을 받아요')
async def checkin_command(interaction: discord.Interaction):
    state, _ = visit(interaction)
    _, notice = game.act(interaction.user.id, state['version'], interaction.id, 'checkin')
    await interaction.response.send_message(notice, ephemeral=True)
    await update_public_farms(interaction.user.id)


@bot.tree.command(name='도움말', description='농장 게임의 규칙과 사용법')
async def help_command(interaction: discord.Interaction):
    visit(interaction)
    await interaction.response.send_message('🌱 /농장 → 작물과 빈 밭 선택 → 심기 → 성장 후 새로고침 → 모두 수확 → 판매\n'
        '1번 밭은 첫 가로줄 5칸이에요. 2~5번 밭은 줄 확장 구매 후 열려요.\n'
        '작물 개방: 1번 감자·당근·호박 / 2번 오이·가지·딸기 / 3번 토마토·포도 / 4번 배·귤 / 5번 쌀·목화\n'
        '밭을 선택하면 해당 줄의 빈 칸에만 심어요. 열린 밭 전체도 선택할 수 있어요.\n'
        '/상점: 씨앗·비료·허수아비 구매 · /가방: 씨앗·비료·수확물·최고 기록\n'
        '🌿 성장 비료: 5G, 빈 밭 한 줄의 다음 심기에 1개 소모하고 성장 시간 20% 감소. 농장의 비료 사용 버튼에서 예약해요.\n'
        '✨ 품질 비료: 20G, 다음 심기 판매가 10% 증가. 성장 비료와 둘 중 하나만 예약할 수 있어요. 무게 기록은 그대로예요.\n'
        '/판매: 작물별 또는 전체 판매 · /순위: 공개 메뉴에서 보유 골드·작물·광물 무게 선택\n'
        '/탄광: 2번 밭부터 자동 채굴. 첫 방문에 노동자·나무 곡괭이·가방 60칸 지급. 가득 차면 생산·천장 정지.\n'
        f"📅 /출석: 매일 {CONFIG['attendance_daily_gold']}골드, 연속 {CONFIG['attendance_bonus_days']}일마다 추가 {CONFIG['attendance_bonus_gold']}골드. 한국 시간 0시 기준, 모든 서버에서 하루 한 번이에요.\n"
        + crop_times() + '. 작물은 시들지 않아요.\n'
        '전체 심기는 빈 밭 수만큼 씨앗이 있어야 해요. 한 줄씩 최대 25칸까지 확장해요.\n'
        '농장은 모든 서버에서 공유해요. 기록은 판매해도 유지돼요.\n'
        '새 피해 확률 10%, 판매가 10% 감소. 감자·당근은 피해 제외. 허수아비 2,000G는 전체 밭 영구 보호예요.\n'
        '공개 농장은 구경용이에요. 내 농장 열기를 누르면 본인에게만 조작 화면이 떠요.\n'
        '개인 농장은 14분간, 상점은 3분간 사용할 수 있어요. 만료되면 내 농장 열기를 다시 눌러 주세요.\n'
        '봇 재시작 후에는 /농장을 다시 열어 주세요.', ephemeral=True)


@bot.tree.error
async def on_command_error(interaction: discord.Interaction, error):
    original = getattr(error, 'original', error)
    message = str(original) if isinstance(original, GameError) else '처리 중 오류가 발생했어요. 잠시 후 다시 실행해 주세요.'
    if not isinstance(original, GameError):
        logging.error('명령어 오류', exc_info=(type(original), original, original.__traceback__))
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


open_mining, MineView, RankView, mine_embed = install_features(bot, game, visit, open_private_farm, update_public_farms)


if __name__ == '__main__':
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        raise SystemExit('.env.example을 .env로 복사하고 DISCORD_TOKEN을 설정해 주세요.')
    bot.run(token, log_handler=None)
