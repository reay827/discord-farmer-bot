"""Mining and public rankings Discord views; no background mining tasks."""
import logging
import math
from datetime import datetime
import discord
from game import CROPS, GameError, KST
from mining import MINING, MINERALS, capacity, interval, kg, seconds_until_full


def install_features(client, game, visit, open_private_farm, update_public_farms):
    def mine_embed(state, notice='', inventory=False):
        m = state['mine']
        embed = discord.Embed(title='⛏️ 나의 탄광', description=notice or '노동자가 자동 채굴 중이에요. 광물 받기로 보관함에 옮긴 뒤 판매하세요.', color=0x89959C)
        embed.add_field(name='💰 보유 골드', value=f"{state['gold']:,}G")
        embed.add_field(name='⛏️ 곡괭이', value=MINING['pickaxe_names'][m['pickaxe']])
        embed.add_field(name='👷 노동자', value=f"{m['workers']}/5명 · 전체 {interval(m)}초/개")
        embed.add_field(name='🎒 탄광 가방', value=f"{len(m['bag'])}/{capacity(m)}개 · {'가득 참! 채굴 일시 정지' if len(m['bag']) >= capacity(m) else '채굴 중'}", inline=False)
        if len(m['bag'])>=capacity(m):
            embed.add_field(name='🎒 가방 가득 참',value='광물을 받아주세요. 받으면 채굴이 다시 시작돼요.',inline=False)
        else:
            remaining=seconds_until_full(m)
            hours,rest=divmod(math.ceil(remaining),3600)
            minutes,seconds=divmod(rest,60)
            finish=datetime.fromtimestamp(m['last']+remaining,KST).strftime('%m/%d %H:%M:%S')
            embed.add_field(name='⏳ 가방이 가득 차는 시간',value=f'{hours:02d}시간 {minutes:02d}분 {seconds:02d}초 후\n🕒 {finish} 예상 · 한국 시간',inline=False)
        lines = []
        for key,c in MINERALS.items():
            if inventory:
                stock = m['inventory'].get(key,{})
                count, value = stock.get('count',0), stock.get('value',0)
            else:
                items = [x for x in m['bag'] if x['mineral'] == key]
                count, value = len(items), sum(x['value'] for x in items)
            if count:
                lines.append(f"{c['emoji']} {c['name']} {count}개 · {value:,}G")
        embed.add_field(name='📦 판매용 보관함' if inventory else '이번 채굴 광물', value='\n'.join(lines) or '아직 광물이 없어요.', inline=False)
        records = [f"{c['emoji']} {c['name']} {kg(m['records'][key])}" for key,c in MINERALS.items() if key in m['records']]
        if records:
            embed.add_field(name='🏆 개인 최고 무게', value='\n'.join(records), inline=False)
        if m['pickaxe'] == 3:
            remaining = max(0,MINING['pity_seconds']-m['pity_seconds'])
            embed.add_field(name='💎 다이아 천장', value=f"실제 채굴 {int(m['pity_seconds']//3600)}시간 {int(m['pity_seconds']%3600//60)}분 / 8시간\n확정까지 약 {int(remaining//3600)}시간 {int(remaining%3600//60)}분 · 가방 만석 시 정지", inline=False)
        embed.set_footer(text='화면을 열거나 버튼을 누를 때 갱신 · 만석 시간 생산 없음 · 받기와 판매는 별개')
        return embed

    class MineView(discord.ui.View):
        def __init__(self,user,state,inventory=False):
            super().__init__(timeout=840)
            self.user,self.version,self.inventory,self.mineral = user,state['version'],inventory,None
            m=state['mine']
            self.hire.disabled=m['workers']==5
            self.hire.label='노동자 최대 5명' if m['workers']==5 else f"노동자 고용 · {MINING['worker_hire_costs'][m['workers']]:,}G"
            self.expand.disabled=m['bag_level']==4
            self.expand.label='가방 최대 확장' if m['bag_level']==4 else f"가방 확장 · {MINING['bag_upgrade_costs'][m['bag_level']+1]:,}G"
            self.pickaxe.disabled=m['pickaxe']==3
            if m['pickaxe']==3:
                self.pickaxe.label='최상위 곡괭이'
            else:
                tier=m['pickaxe']+1
                self.pickaxe.label=f"{MINING['pickaxe_names'][tier]} · {MINING['pickaxe_upgrade_costs'][MINING['pickaxes'][tier]]:,}G"

        async def interaction_check(self,i):
            if i.user.id!=self.user:
                await i.response.send_message('자신의 /탄광을 열어 주세요.',ephemeral=True)
                return False
            return True

        async def on_error(self,i,error,item):
            logging.error('탄광 화면 오류',exc_info=(type(error),error,error.__traceback__))
            if not i.response.is_done():
                await i.response.send_message('오류가 발생했어요. /탄광을 다시 열어 주세요.',ephemeral=True)

        async def act(self,i,action,inventory=None,**kwargs):
            if await open_mining(i,edit=True,action=action,version=self.version,
                                 inventory=self.inventory if inventory is None else inventory,**kwargs):
                self.stop()

        @discord.ui.select(placeholder='판매할 광물 선택 · 기본은 전체',row=0,options=[discord.SelectOption(label='전체 광물',value='all',emoji='🧺',default=True)]+[discord.SelectOption(label=c['name'],value=k,emoji=c['emoji']) for k,c in MINERALS.items()])
        async def ore_menu(self,i,select):
            self.mineral=None if select.values[0]=='all' else select.values[0]
            for option in select.options:
                option.default=option.value==select.values[0]
            await i.response.edit_message(view=self)

        @discord.ui.button(label='광물 받기',emoji='🎒',style=discord.ButtonStyle.green,row=1)
        async def collect(self,i,b):
            await self.act(i,'mine_collect',inventory=True)

        @discord.ui.button(label='선택 광물 판매',emoji='💰',row=1)
        async def sell(self,i,b):
            await self.act(i,'mine_sell',inventory=True,mineral=self.mineral)

        @discord.ui.button(label='보관함 보기',emoji='📦',row=1)
        async def bag(self,i,b):
            await self.act(i,'mine_open',inventory=not self.inventory)

        @discord.ui.button(label='노동자 고용',emoji='👷',row=2)
        async def hire(self,i,b):
            await self.act(i,'mine_hire')

        @discord.ui.button(label='가방 확장',emoji='🎒',row=2)
        async def expand(self,i,b):
            await self.act(i,'mine_bag_upgrade')

        @discord.ui.button(label='곡괭이 업그레이드',emoji='⛏️',row=3)
        async def pickaxe(self,i,b):
            await self.act(i,'mine_pickaxe')

        @discord.ui.button(label='현황 갱신',emoji='🔄',row=3)
        async def refresh(self,i,b):
            # 갱신은 다른 화면에서 바뀐 최신 버전으로 다시 엽니다.
            await open_mining(i,edit=True,inventory=self.inventory)
            self.stop()

        @discord.ui.button(label='내 농장 열기',emoji='🌿',row=4)
        async def farm(self,i,b):
            await open_private_farm(i)
            self.stop()

    async def open_mining(interaction,edit=False,action='mine_open',version=None,inventory=False,**kwargs):
        state,_=visit(interaction)
        try:
            state,notice=game.act(interaction.user.id,state['version'] if version is None else version,
                                  interaction.id,action,**kwargs)
        except GameError as error:
            await interaction.response.send_message(str(error),ephemeral=True)
            return
        view=MineView(interaction.user.id,state,inventory)
        if edit:
            await interaction.response.edit_message(content=None,embed=mine_embed(state,notice,inventory),view=view)
        else:
            await interaction.response.send_message(embed=mine_embed(state,notice,inventory),view=view,ephemeral=True)
        if action=='mine_sell':
            await update_public_farms(interaction.user.id)
        return True

    @client.tree.command(name='탄광',description='2번 밭부터 자동 채굴·고용·광물 받기·판매')
    async def mine_command(interaction:discord.Interaction):
        await open_mining(interaction)

    class RankView(discord.ui.View):
        def __init__(self,guild,kind='gold',scope='server',item=None):
            super().__init__(timeout=600)
            self.guild,self.kind,self.scope=guild,kind,scope
            self.item=item or (next(iter(CROPS)) if kind=='crop' else next(iter(MINERALS)))
            for option in self.kind_menu.options:
                option.default=option.value==kind
            for option in self.scope_menu.options:
                option.default=option.value==scope
            if kind!='gold':
                choices=CROPS if kind=='crop' else MINERALS
                menu=discord.ui.Select(placeholder='작물 선택' if kind=='crop' else '광물 선택',row=2,options=[discord.SelectOption(label=c['name'],value=k,emoji=c['emoji'],default=k==self.item) for k,c in choices.items()])
                async def choose(i):
                    await self.show(i,item=menu.values[0])
                menu.callback=choose
                self.add_item(menu)

        def embed(self):
            rows=game.ranking(self.guild if self.scope=='server' else None,
                crop=self.item if self.kind=='crop' else None,mineral=self.item if self.kind=='mineral' else None)
            label='보유 골드' if self.kind=='gold' else (CROPS if self.kind=='crop' else MINERALS)[self.item]['name']+' 최고 무게'
            lines=[]
            for rank,(_,name,score) in enumerate(rows,1):
                value=f'{score:,}G' if self.kind=='gold' else f'{score:,}g' if self.kind=='crop' else kg(score)
                lines.append(f'{rank}. {discord.utils.escape_markdown(name)} · **{value}**')
            embed=discord.Embed(title=f"🏆 {'현재 서버' if self.scope=='server' else '봇 전체'} · {label}",description='\n'.join(lines) or '아직 기록이 없어요.',color=0xEBCB78)
            embed.set_footer(text='자산은 현재 보유 골드만 · 미판매 재고 제외 · 구매하면 감소 · 무게 기록은 판매 후 유지')
            return embed

        async def show(self,i,kind=None,scope=None,item=None):
            scope=scope or self.scope
            if scope=='server' and self.guild is None:
                await i.response.send_message('서버에서 열거나 봇 전체를 선택해 주세요.',ephemeral=True)
                return
            view=RankView(self.guild,kind or self.kind,scope,item or (self.item if kind is None else None))
            await i.response.edit_message(embed=view.embed(),view=view)
            self.stop()

        @discord.ui.select(placeholder='순위 종류',row=0,options=[discord.SelectOption(label='보유 골드',value='gold',emoji='💰'),discord.SelectOption(label='작물 무게',value='crop',emoji='🌱'),discord.SelectOption(label='광물 무게',value='mineral',emoji='⛏️')])
        async def kind_menu(self,i,select):
            await self.show(i,kind=select.values[0])

        @discord.ui.select(placeholder='범위 선택',row=1,options=[discord.SelectOption(label='현재 서버',value='server',emoji='🏡'),discord.SelectOption(label='봇 전체',value='global',emoji='🌐')])
        async def scope_menu(self,i,select):
            await self.show(i,scope=select.values[0])

        @discord.ui.button(label='순위 갱신',emoji='🔄',row=3)
        async def refresh(self,i,b):
            await self.show(i)

    @client.tree.command(name='순위',description='공개 보유 골드·작물 무게·광물 무게 순위')
    async def rank_command(interaction:discord.Interaction):
        visit(interaction)
        view=RankView(interaction.guild_id,scope='server' if interaction.guild_id else 'global')
        await interaction.response.send_message(embed=view.embed(),view=view)

    return open_mining, MineView, RankView, mine_embed
