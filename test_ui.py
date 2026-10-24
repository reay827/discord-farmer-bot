import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import bot


class AutoGrowthTests(unittest.IsolatedAsyncioTestCase):
    def state(self, delay):
        return dict(version=0, rows=1, gold=100, revenue=0, scarecrow=False,
                    seeds={k: 0 for k in bot.CROPS},
                    plots=[dict(crop='carrot', ready=time.time()+delay, bird=False)]+[None]*24)

    async def test_changes_seedling_to_carrot_without_click(self):
        state = self.state(.03)
        self.assertIn('🌱', bot.farm_embed(state).description)
        message = AsyncMock()
        view = bot.FarmView(1, state)
        with patch.object(bot.game, 'read', return_value=state):
            view.watch(message)
            await asyncio.wait_for(view._growth_task, timeout=1)
        message.edit.assert_awaited_once()
        embed = message.edit.call_args.kwargs['embed']
        self.assertIn('🥕', embed.description)
        self.assertNotIn('🌱', embed.description)
        self.assertFalse(any(f.name == '성장 완료 예정' for f in embed.fields))
        view.stop()

    async def test_replaced_view_cancels_timer(self):
        view = bot.FarmView(1, self.state(1800))
        message = AsyncMock()
        view.watch(message)
        view.stop()
        await asyncio.sleep(0)
        self.assertTrue(view._growth_task.cancelled())
        message.edit.assert_not_awaited()

    async def test_each_distinct_ready_time_updates(self):
        state = self.state(.01)
        state['plots'][1] = dict(crop='tomato', ready=time.time()+.05, bird=False)
        view = bot.FarmView(1, state)
        message = AsyncMock()
        with patch.object(bot.game, 'read', return_value=state):
            view.watch(message)
            await asyncio.wait_for(view._growth_task, timeout=1)
        self.assertEqual(message.edit.await_count, 2)
        view.stop()

    async def test_shop_category_navigation_and_purchase(self):
        state = self.state(1800)
        view = bot.ShopView(1, state)
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()), id=123)
        with patch.object(bot.game, 'read', return_value=state):
            await view.navigate(interaction, 'supplies', 'carrot')
        supplies = interaction.response.edit_message.call_args.kwargs['view']
        self.assertEqual(len(supplies.children), 2)
        self.assertIn('허수아비', supplies.embed().fields[1].name)
        with patch.object(bot.game, 'read', return_value=state):
            await supplies.navigate(interaction, 'seeds', 'tomato')
        seeds = interaction.response.edit_message.call_args.kwargs['view']
        self.assertEqual(len(seeds.children), 5)
        self.assertIn('토마토', seeds.embed().fields[1].name)
        with patch.object(bot.game, 'act', return_value=(state, '구매 완료')) as act:
            await seeds.children[3].callback(interaction)
            act.assert_called_once_with(1, 0, 123, 'buy', crop='tomato', quantity=5)
        updated = interaction.response.edit_message.call_args.kwargs['view']
        self.assertEqual(updated.crop, 'tomato')
        updated.stop()
