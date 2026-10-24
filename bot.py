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
        grid.append(' '.join(cells))
    embed = discord.Embed(title='🌿 나의 작은 농장', description=notice+'\n\n'+'\n'.join(grid), color=0x78AD70)
    embed.add_field(name='지갑', value=f"{s['gold']:,}골드")
    embed.add_field(name='누적 판매', value=f"{s['revenue']:,}골드")
    embed.add_field(name='밭 / 허수아비', value=f"{s['rows']*5}/25칸 · {'설치됨' if s['scarecrow'] else '없음'}")
    growing = [f"{i+1}번 {CROPS[p['crop']]['name']} · <t:{int(p['ready'])}:R>" for i,p in enumerate(s['plots']) if p and p['ready'] > time.time()]
    if growing:
        embed.add_field(name='성장 완료 예정', value='\n'.join(growing), inline=False)
    embed.set_footer(text='🟫 빈 밭 · 🌱 성장 중 · 작물 그림은 수확 가능 | 화면은 갱신할 때 바뀝니다.')
    return embed


class FarmView(discord.ui.View):
    def __init__(self, user, state):
        super().__init__(timeout=180)
        self.user, self.version = user, state['version']
        self.crop, self.plot = 'carrot', None
        self.crop_menu.options = [discord.SelectOption(label=f"{c['name']} · 씨앗 {state['seeds'][k]}개", value=k, emoji=c['emoji'], default=k=='carrot') for k,c in CROPS.items()]
        self.plot_menu.options = [discord.SelectOption(label=f'{i+1}번 밭', value=str(i)) for i in range(state['rows']*5) if state['plots'][i] is None]
        if not self.plot_menu.options:
            self.plot_menu.options = [discord.SelectOption(label='빈 밭이 없어요', value='none')]
            self.plot_menu.disabled = True
        self.expand.label = '최대 확장 완료' if state['rows'] == 5 else f"다음 줄 확장 · {CONFIG['expansion_prices'][state['rows']-1]}골드"
        self.expand.disabled = state['rows'] == 5

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
        await interaction.response.edit_message(embed=farm_embed(s, msg), view=FarmView(self.user, s))
        self.stop()

    @discord.ui.select(placeholder='심을 작물 선택', row=0)
    async def crop_menu(self, interaction, select):
        self.crop = select.values[0]
        for option in select.options:
            option.default = option.value == self.crop
        await interaction.response.edit_message(view=self)

    @discord.ui.select(placeholder='기본: 빈 밭 전체 · 개별 칸 선택 가능', row=1)
    async def plot_menu(self, interaction, select):
        self.plot = int(select.values[0])
        for option in select.options:
            option.default = option.value == select.values[0]
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label='선택한 밭에 심기', style=discord.ButtonStyle.green, row=2)
    async def plant(self, interaction, button):
        await self.action(interaction, 'plant', crop=self.crop, plot=self.plot)

    @discord.ui.button(label='다 자란 작물 모두 수확', style=discord.ButtonStyle.green, row=2)
    async def harvest(self, interaction, button):
        await self.action(interaction, 'harvest')

    @discord.ui.button(label='수확물 전체 판매', row=2)
    async def sell(self, interaction, button):
        await self.action(interaction, 'sell')

    @discord.ui.button(label='다음 줄 확장', row=3)
    async def expand(self, interaction, button):
        await self.action(interaction, 'expand')

    @discord.ui.button(label='새로고침', row=3)
    async def refresh(self, interaction, button):
        s = game.read(self.user)
        await interaction.response.edit_message(embed=farm_embed(s), view=FarmView(self.user, s))
        self.stop()

    @discord.ui.button(label='빈 밭 전체 선택', row=3)
    async def select_all(self, interaction, button):
        self.plot = None
        for option in self.plot_menu.options:
            option.default = False
        await interaction.response.edit_message(view=self)


class ShopView(discord.ui.View):
    def __init__(self, user, state):
        super().__init__(timeout=180)
        self.user, self.version = user, state['version']
        for crop, c in CROPS.items():
            for quantity in (1, 5, 25):
                button = discord.ui.Button(label=f"{c['name']} {quantity}개 · {c['seed_price']*quantity}G", row=list(CROPS).index(crop))
                async def callback(interaction, crop=crop, quantity=quantity):
                    await self.buy(interaction, 'buy', crop=crop, quantity=quantity)
                button.callback = callback
                self.add_item(button)
        button = discord.ui.Button(label=f"허수아비 · {CONFIG['scarecrow_price']}G", row=3, disabled=state['scarecrow'])
        async def scarecrow(interaction):
            await self.buy(interaction, 'scarecrow')
        button.callback = scarecrow
        self.add_item(button)

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
        await interaction.response.edit_message(content=f"{msg}\n보유 골드: {s['gold']}G", view=ShopView(self.user, s))
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
    await interaction.response.send_message(embed=farm_embed(s, intro), view=FarmView(interaction.user.id, s), ephemeral=True)


@bot.tree.command(name='상점', description='씨앗과 영구 허수아비를 구매해요')
async def shop(interaction: discord.Interaction):
    s, _ = visit(interaction)
    await interaction.response.send_message(content=f"🌻 씨앗 상점 · 보유 골드 {s['gold']}G\n허수아비는 구입 후 새로 심는 작물부터 보호해요.", view=ShopView(interaction.user.id, s), ephemeral=True)


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
        '/상점: 씨앗·허수아비 구매 · /가방: 씨앗·수확물·최고 기록\n'
        '/판매: 작물별 또는 전체 판매 · /순위: 범위 선택, crop 생략 시 누적 판매 수익\n'
        '당근 30초 · 토마토 5분 · 호박 30분. 작물은 시들지 않아요.\n'
        '전체 심기는 빈 밭 수만큼 씨앗이 있어야 해요. 한 줄씩 최대 25칸까지 확장해요.\n'
        '농장은 모든 서버에서 공유해요. 기록은 판매해도 유지돼요.\n'
        '새 피해 확률 5%, 판매가 10% 감소. 크기 기록은 그대로예요. 허수아비는 영구적이에요.\n'
        '버튼 유효 시간은 3분이에요. 오래된 화면에서는 명령어를 다시 실행하세요.', ephemeral=True)


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
