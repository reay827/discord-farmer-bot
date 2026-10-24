import asyncio
import logging
import os
import time
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

from game import CONFIG, CROPS, Game, GameError, grade

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
logging.basicConfig(level=logging.INFO)
db_path = Path(os.getenv('DATABASE_PATH', 'data/farm.db'))
game = Game(db_path if db_path.is_absolute() else ROOT / db_path)


def farm_embed(s, notice=''):
    grid = []
    for row in range(5):
        cells = []
        for col in range(5):
            p = s['plots'][row*5+col]
            cells.append('🔒' if row >= s['rows'] else '🟫' if p is None else
                         CROPS[p['crop']]['emoji'] if p['ready'] <= time.time() else '🌱')
        grid.append(('1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣')[row] + '　' + ' '.join(cells))
    embed = discord.Embed(title='🌿 나의 작은 농장', description=notice+'\n\n'+'\n'.join(grid), color=0x78AD70)
    embed.add_field(name='지갑', value=f"{s['gold']:,}골드")
    embed.add_field(name='누적 판매', value=f"{s['revenue']:,}골드")
    embed.add_field(name='밭 / 허수아비', value=f"{s['rows']*5}/25칸 · {'설치됨' if s['scarecrow'] else '없음'}")
    growing = [f"{i//5+1}번 밭·{i%5+1}칸 {CROPS[p['crop']]['name']} · <t:{int(p['ready'])}:R>" for i,p in enumerate(s['plots']) if p and p['ready'] > time.time()]
    if growing:
        embed.add_field(name='성장 완료 예정', value='\n'.join(growing), inline=False)
    embed.set_footer(text='🟫 빈 밭 · 🌱 성장 중 · 작물 그림은 수확 가능 | 성장이 끝나면 자동으로 바뀝니다.')
    return embed


class FarmView(discord.ui.View):
    def __init__(self, user, state):
        super().__init__(timeout=7200)
        self._growth_task = None
        self._edit_lock = asyncio.Lock()
        self._deadlines = sorted({p['ready'] for p in state['plots'] if p and p['ready'] > time.time()})
        self.user, self.version = user, state['version']
        self.crop, self.row = 'carrot', 0
        self.open_rows = state['rows']
        self.crop_menu.options = [discord.SelectOption(label=f"{c['name']} · 씨앗 {state['seeds'][k]}개", value=k, emoji=c['emoji'], default=k=='carrot') for k,c in CROPS.items()]
        self.plot_menu.options = [discord.SelectOption(label='열린 밭 전체', value='all', emoji='🌾', description='열린 모든 줄의 빈 칸에 심기')]
        for i in range(5):
            opened = i < state['rows']
            empty = sum(p is None for p in state['plots'][i*5:i*5+5])
            description = f'가로 한 줄 5칸 · 빈 칸 {empty}개' if opened else '잠김 · 위에서부터 줄 확장 구매로 개방'
            self.plot_menu.options.append(discord.SelectOption(label=f"{i+1}번 밭{' · 잠김' if not opened else ''}", value=str(i), emoji='🟫' if opened else '🔒', description=description, default=i==0))
        self.expand.label = '최대 확장 완료' if state['rows'] == 5 else f"다음 밭 확장 · {CONFIG['expansion_prices'][state['rows']-1]}골드"
        self.expand.disabled = state['rows'] == 5

    def watch(self, message):
        # 일반 채널 메시지로 수정하므로 15분짜리 interaction 토큰에 의존하지 않습니다.
        if self._deadlines and self._growth_task is None:
            self._growth_task = asyncio.create_task(self._update_growth(message))

    async def _update_growth(self, message):
        try:
            for deadline in self._deadlines:
                await asyncio.sleep(max(0, deadline - time.time()) + 0.1)
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
            view.watch(interaction.channel.get_partial_message(interaction.message.id))
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

    @discord.ui.select(placeholder='심을 작물 선택', row=0)
    async def crop_menu(self, interaction, select):
        self.crop = select.values[0]
        for option in select.options:
            option.default = option.value == self.crop
        await interaction.response.edit_message(view=self)

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

    @discord.ui.button(label='상점', emoji='🏪', style=discord.ButtonStyle.primary, row=4)
    async def go_shop(self, interaction, button):
        view = ShopView(self.user, game.read(self.user))
        async with self._edit_lock:
            await interaction.response.edit_message(content=None, embed=view.embed(), view=view)
            self.stop()


class ShopView(discord.ui.View):
    def __init__(self, user, state, category='seeds', crop='carrot'):
        super().__init__(timeout=180)
        self.user, self.version = user, state['version']
        self.state, self.category, self.crop = state, category, crop
        for option in self.category_menu.options:
            option.default = option.value == category
        if category == 'seeds':
            menu = discord.ui.Select(placeholder='🌱 구매할 씨앗 선택', row=1, options=[
                discord.SelectOption(label=c['name'], value=k, emoji=c['emoji'], default=k==crop,
                                     description=f"씨앗 {c['seed_price']}G · 성장 {c['seconds']}초") for k,c in CROPS.items()])
            async def choose_crop(interaction):
                await self.navigate(interaction, 'seeds', menu.values[0])
            menu.callback = choose_crop
            self.add_item(menu)
            c = CROPS[crop]
            for quantity in (1, 5, 25):
                button = discord.ui.Button(label=f"{quantity}개 구매 · {c['seed_price']*quantity}G", emoji=c['emoji'], row=2, style=discord.ButtonStyle.green)
                async def callback(interaction, quantity=quantity):
                    await self.buy(interaction, 'buy', crop=self.crop, quantity=quantity)
                button.callback = callback
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
            duration = f'{seconds}초' if seconds < 60 else f'{seconds//60}분'
            embed.add_field(name=f"{c['emoji']} {c['name']} 씨앗", value=f"가격 **{c['seed_price']}G / 개**\n성장 시간 **{duration}**\n보유 씨앗 **{self.state['seeds'][self.crop]}개**\n아래 버튼으로 수량을 선택해 구매하세요.", inline=False)
        else:
            embed.add_field(name='🛡️ 허수아비', value=f"가격 **{CONFIG['scarecrow_price']}G** · 영구 적용\n구매 후 새로 심는 작물부터 새 피해를 막아요.\n상태: {'✅ 설치됨' if self.state['scarecrow'] else '미설치'}", inline=False)
        return embed

    @discord.ui.select(placeholder='🗂️ 카테고리 선택', row=0, options=[
        discord.SelectOption(label='씨앗', value='seeds', emoji='🌱', description='당근 · 토마토 · 호박'),
        discord.SelectOption(label='농장 용품', value='supplies', emoji='🧰', description='농장을 지켜 주는 허수아비')])
    async def category_menu(self, interaction, select):
        await self.navigate(interaction, select.values[0], self.crop)

    @discord.ui.button(label='농장으로', emoji='🌿', style=discord.ButtonStyle.primary, row=4)
    async def go_farm(self, interaction, button):
        state = game.read(self.user)
        view = FarmView(self.user, state)
        if interaction.message.flags.ephemeral:
            # /상점은 비공개이므로 장시간 자동 갱신이 가능한 공개 농장 화면을 엽니다.
            await interaction.response.send_message(embed=farm_embed(state), view=view)
            message = await interaction.original_response()
            await interaction.message.edit(view=None)
        else:
            await interaction.response.edit_message(content=None, embed=farm_embed(state), view=view)
            message = interaction.message
        view.watch(interaction.channel.get_partial_message(message.id))
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


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild_id = os.getenv('TEST_GUILD_ID')
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = Bot()


def visit(interaction):
    return game.visit(interaction.user.id, interaction.user.display_name, interaction.guild_id)


@bot.tree.command(name='농장', description='농장을 열고 심기·수확·판매·확장을 해요')
async def farm(interaction: discord.Interaction):
    s, fresh = visit(interaction)
    intro = '오래 비어 있던 작은 농장을 맡게 되었어요.\n우선 흙을 고른 첫 번째 밭에 씨앗을 심어볼까요?\n당근 씨앗 5개와 100골드를 받았어요. 심기 → 30초 후 새로고침 → 수확 → 판매 순서로 해보세요.' if fresh else ''
    view = FarmView(interaction.user.id, s)
    await interaction.response.send_message(embed=farm_embed(s, intro), view=view)
    message = await interaction.original_response()
    view.watch(interaction.channel.get_partial_message(message.id))


@bot.tree.command(name='상점', description='씨앗과 영구 허수아비를 구매해요')
async def shop(interaction: discord.Interaction):
    s, _ = visit(interaction)
    view = ShopView(interaction.user.id, s)
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


@bot.tree.command(name='가방', description='씨앗, 수확물, 개인 최고 기록을 확인해요')
async def bag(interaction: discord.Interaction):
    s, _ = visit(interaction)
    embed = discord.Embed(title='🎒 나의 가방', color=0x78AD70)
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


@bot.tree.command(name='순위', description='서버·전체의 작물별 최고 무게 또는 누적 판매 순위')
@app_commands.choices(scope=[app_commands.Choice(name='현재 서버',value='server'),app_commands.Choice(name='봇 전체',value='global')], crop=CROP_CHOICES)
async def ranking(interaction: discord.Interaction, scope: str = 'server', crop: str = None):
    visit(interaction)
    if scope == 'server' and interaction.guild_id is None:
        await interaction.response.send_message('서버에서 실행하거나 scope에서 봇 전체를 선택해 주세요.', ephemeral=True)
        return
    rows = game.ranking(interaction.guild_id if scope == 'server' else None, crop)
    label = CROPS[crop]['name']+' 최고 무게' if crop else '누적 판매 수익'
    lines = [f"{i}. {discord.utils.escape_markdown(name)} · {score:,}{'g' if crop else 'G'}" for i,(_,name,score) in enumerate(rows, 1)]
    embed = discord.Embed(title=f"🏆 {'현재 서버' if scope == 'server' else '봇 전체'} · {label}", description='\n'.join(lines) or '아직 기록이 없어요.', color=0xEBCB78)
    embed.set_footer(text='서버 순위는 이 서버에서 봇을 사용한 사람의 모든 서버 합산 기록입니다. 동점은 계정 ID 순서로 표시합니다.')
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name='도움말', description='농장 게임의 규칙과 사용법')
async def help_command(interaction: discord.Interaction):
    visit(interaction)
    await interaction.response.send_message('🌱 /농장 → 작물과 빈 밭 선택 → 심기 → 성장 후 새로고침 → 모두 수확 → 판매\n'
        '1번 밭은 첫 가로줄 5칸이에요. 2~5번 밭은 줄 확장 구매 후 열려요.\n'
        '밭을 선택하면 해당 줄의 빈 칸에만 심어요. 열린 밭 전체도 선택할 수 있어요.\n'
        '/상점: 씨앗·허수아비 구매 · /가방: 씨앗·수확물·최고 기록\n'
        '/판매: 작물별 또는 전체 판매 · /순위: 범위 선택, crop 생략 시 누적 판매 수익\n'
        '당근 30초 · 토마토 5분 · 호박 30분. 작물은 시들지 않아요.\n'
        '전체 심기는 빈 밭 수만큼 씨앗이 있어야 해요. 한 줄씩 최대 25칸까지 확장해요.\n'
        '농장은 모든 서버에서 공유해요. 기록은 판매해도 유지돼요.\n'
        '새 피해 확률 5%, 판매가 10% 감소. 크기 기록은 그대로예요. 허수아비는 영구적이에요.\n'
        '농장은 채널에 표시되고 성장 완료 시 자동 갱신돼요. 농장 버튼은 2시간, 상점 버튼은 3분 동안 사용할 수 있어요.\n'
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


if __name__ == '__main__':
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        raise SystemExit('.env.example을 .env로 복사하고 DISCORD_TOKEN을 설정해 주세요.')
    bot.run(token, log_handler=None)
