import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch,PropertyMock
import bot
from mining import open_mine


class FeatureUITests(unittest.IsolatedAsyncioTestCase):
    def state(self):
        s=dict(rows=2,gold=1000,version=0,seeds={k:0 for k in bot.CROPS},plots=[None]*25,records={},revenue=0,scarecrow=False)
        open_mine(s,1000)
        return s

    def interaction(self):
        return SimpleNamespace(user=SimpleNamespace(id=1,display_name='농부'),guild_id=10,id=987,
            response=SimpleNamespace(send_message=AsyncMock(),edit_message=AsyncMock()),original_response=AsyncMock(return_value=AsyncMock()))

    async def test_mining_open_is_private_and_has_no_growth_task(self):
        state=self.state(); i=self.interaction()
        with patch.object(bot.game,'visit',return_value=(state,False)),patch.object(bot.game,'act',return_value=(state,'개방')):
            self.assertTrue(await bot.open_mining(i))
        args=i.response.send_message.call_args.kwargs
        self.assertTrue(args['ephemeral'])
        self.assertIsInstance(args['view'],bot.MineView)
        self.assertFalse(hasattr(args['view'],'_growth_task'))
        self.assertIn('탄광',args['embed'].title)
        args['view'].stop()

    async def test_mine_failed_action_does_not_disable_existing_view(self):
        from game import GameError
        state=self.state(); view=bot.MineView(1,state); i=self.interaction()
        with patch.object(bot.game,'visit',return_value=(state,False)),patch.object(bot.game,'act',side_effect=GameError('골드 부족')):
            await view.hire.callback(i)
        self.assertFalse(view.is_finished())
        i.response.send_message.assert_awaited_once_with('골드 부족',ephemeral=True)
        i.response.edit_message.assert_not_awaited()
        view.stop()

    async def test_rank_command_is_public_with_gold_default(self):
        i=self.interaction(); state=self.state()
        command=bot.bot.tree.get_command('순위')
        with patch.object(bot.game,'visit',return_value=(state,False)),patch.object(bot.game,'ranking',return_value=[('1','농부',1000)]):
            await command.callback(i)
        args=i.response.send_message.call_args.kwargs
        self.assertNotIn('ephemeral',args)
        self.assertIn('보유 골드',args['embed'].title)
        self.assertEqual(args['view'].kind,'gold')
        args['view'].stop()

    async def test_public_rank_switches_to_mineral_and_global(self):
        i=self.interaction(); view=bot.RankView(10)
        with patch.object(bot.game,'ranking',return_value=[]),patch.object(type(view.kind_menu),'values',new_callable=PropertyMock,return_value=['mineral']):
            await view.kind_menu.callback(i)
        mineral=i.response.edit_message.call_args.kwargs['view']
        self.assertEqual(mineral.kind,'mineral')
        self.assertEqual(len(mineral.children[-1].options),7)
        with patch.object(bot.game,'ranking',return_value=[('1','농부',1200)]) as query:
            await mineral.show(i,scope='global',item='diamond')
            query.assert_called_once_with(None,crop=None,mineral='diamond')
        returned=i.response.edit_message.call_args.kwargs['view']
        embed=i.response.edit_message.call_args.kwargs['embed']
        self.assertIn('0.0012kg',embed.description)
        returned.stop()

    async def test_pickaxe_shop_locks_before_second_farm(self):
        state=self.state(); state['rows']=1; state['mine']=None
        view=bot.ShopView(1,state,'pickaxes')
        purchase=next(b for b in view.children if isinstance(b,bot.discord.ui.Button) and '곡괭이 ·' in b.label)
        self.assertTrue(purchase.disabled)
        self.assertIn('2번 밭',view.embed().fields[-1].value)
        view.stop()

    async def test_mine_owner_check(self):
        view=bot.MineView(1,self.state()); i=self.interaction(); i.user.id=2
        self.assertFalse(await view.interaction_check(i))
        i.response.send_message.assert_awaited_once()
        view.stop()

    async def test_mine_embeds_fit_discord_limits_and_jackpot_receipt(self):
        from mining import MINERALS
        state=self.state(); m=state['mine']; m['pickaxe']=3
        for key,c in MINERALS.items():
            m['inventory'][key]=dict(count=100,value=1000000,best_mg=1000)
            m['records'][key]=1000
        embed=bot.mine_embed(state,'💎 잭팟!',inventory=True)
        self.assertLess(len(embed),6000)
        self.assertTrue(all(len(f.value)<=1024 for f in embed.fields))
        self.assertIn('잭팟',embed.description)
