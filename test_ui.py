import asyncio
import time
import unittest
from unittest.mock import AsyncMock, PropertyMock, patch
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
        self.assertEqual(len(supplies.children), 3)
        self.assertIn('허수아비', supplies.embed().fields[1].name)
        with patch.object(bot.game, 'read', return_value=state):
            await supplies.navigate(interaction, 'seeds', 'tomato')
        seeds = interaction.response.edit_message.call_args.kwargs['view']
        self.assertEqual(len(seeds.children), 6)
        self.assertIn('토마토', seeds.embed().fields[1].name)
        with patch.object(bot.game, 'act', return_value=(state, '구매 완료')) as act:
            button = next(item for item in seeds.children if isinstance(item, bot.discord.ui.Button) and item.label.startswith('5개 구매'))
            await button.callback(interaction)
            act.assert_called_once_with(1, 0, 123, 'buy', crop='tomato', quantity=5)
        updated = interaction.response.edit_message.call_args.kwargs['view']
        self.assertEqual(updated.crop, 'tomato')
        updated.stop()

    async def test_five_rows_visible_with_locks_and_unlock(self):
        state = self.state(1800)
        view = bot.FarmView(1, state)
        self.assertEqual(len(view.plot_menu.options), 6)
        self.assertEqual(view.row, 0)
        self.assertEqual(sum(str(o.emoji) == '🔒' for o in view.plot_menu.options), 4)
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()))
        with patch.object(type(view.plot_menu), 'values', new_callable=PropertyMock, return_value=['1']):
            await view.plot_menu.callback(interaction)
        interaction.response.send_message.assert_awaited_once()
        self.assertEqual(view.row, 0)
        state['rows'] = 2
        unlocked = bot.FarmView(1, state)
        self.assertEqual(str(unlocked.plot_menu.options[2].emoji), '🟫')
        self.assertNotIn('잠김', unlocked.plot_menu.options[2].label)
        view.stop()
        unlocked.stop()

    async def test_farm_shop_round_trip_uses_latest_state(self):
        state = self.state(1800)
        farm = bot.FarmView(1, state)
        message = AsyncMock()
        message.id = 123
        message.flags = SimpleNamespace(ephemeral=False)
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()), message=message,
                                      original_response=AsyncMock(return_value=message),
                                      channel=SimpleNamespace(get_partial_message=lambda mid: message))
        with patch.object(bot.game, 'read', return_value=state):
            await farm.go_shop.callback(interaction)
        shop = interaction.response.edit_message.call_args.kwargs['view']
        self.assertIsInstance(shop, bot.ShopView)
        self.assertTrue(farm.is_finished())
        state['gold'] = 75
        state['version'] = 1
        with patch.object(bot.game, 'read', return_value=state):
            await shop.go_farm.callback(interaction)
        returned = interaction.response.edit_message.call_args.kwargs['view']
        self.assertEqual(returned.version, 1)
        self.assertIsNotNone(returned._growth_task)
        self.assertTrue(shop.is_finished())
        returned.stop()

    async def test_private_shop_returns_to_same_private_farm(self):
        state = self.state(1800)
        shop = bot.ShopView(1, state)
        old = AsyncMock()
        old.flags = SimpleNamespace(ephemeral=True)
        interaction = SimpleNamespace(message=old, response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
            original_response=AsyncMock(return_value=old),
            channel=SimpleNamespace(get_partial_message=lambda mid: AsyncMock()))
        with patch.object(bot.game, 'read', return_value=state):
            await shop.go_farm.callback(interaction)
        interaction.response.send_message.assert_not_awaited()
        interaction.response.edit_message.assert_awaited_once()
        returned = interaction.response.edit_message.call_args.kwargs['view']
        returned.stop()

    async def test_fertilizer_shop_and_reservation_return_to_farm(self):
        state = self.state(1800)
        state['fertilizer'] = 2
        state['fertilizer_rows'] = [False]*5
        shop = bot.ShopView(1, state, 'fertilizer')
        self.assertIn('성장 비료', shop.embed().fields[1].name)
        interaction = SimpleNamespace(id=456, response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
            original_response=AsyncMock(return_value=AsyncMock()),
            message=SimpleNamespace(id=789), channel=SimpleNamespace(get_partial_message=lambda mid: AsyncMock()))
        with patch.object(bot.game, 'act', return_value=(state, '구매 완료')) as act:
            purchase = next(b for b in shop.children if isinstance(b, bot.discord.ui.Button) and b.label.startswith('1개 구매'))
            await purchase.callback(interaction)
            act.assert_called_once_with(1, 0, 456, 'buy_fertilizer', quantity=1)
        interaction.response.edit_message.call_args.kwargs['view'].stop()
        fertilizer = bot.FertilizerView(1, state)
        with patch.object(bot.game, 'act', return_value=(state, '예약 완료')) as act:
            await fertilizer.reserve.callback(interaction)
            act.assert_called_once_with(1, 0, 456, 'reserve_fertilizer', row=0)
        farm = interaction.response.edit_message.call_args.kwargs['view']
        self.assertIsInstance(farm, bot.FarmView)
        self.assertTrue(fertilizer.is_finished())
        farm.stop()

    async def test_public_button_opens_clickers_own_private_farm(self):
        state = self.state(1800)
        panel = bot.PublicFarmView(owner=1, name='구경할 농부')
        message = AsyncMock()
        interaction = SimpleNamespace(user=SimpleNamespace(id=2),
            response=SimpleNamespace(send_message=AsyncMock()), original_response=AsyncMock(return_value=message))
        with patch.object(bot, 'visit', return_value=(state, False)):
            await panel.open_farm.callback(interaction)
        args = interaction.response.send_message.call_args.kwargs
        self.assertTrue(args['ephemeral'])
        self.assertEqual(args['view'].user, 2)
        self.assertEqual(len(panel.children), 1)
        self.assertIsInstance(panel.children[0], bot.discord.ui.Button)
        args['view'].stop()
        panel.stop()

    async def test_public_growth_finishes_without_private_screen(self):
        state = self.state(.02)
        message = AsyncMock()
        panel = bot.PublicFarmView(owner=1, name='농부', message=message)
        with patch.object(bot.game, 'read', return_value=state):
            await asyncio.wait_for(panel.growth_loop(), timeout=1)
        message.edit.assert_awaited_once()
        embed = message.edit.call_args.kwargs['embed']
        self.assertIn('농부님의 농장', embed.title)
        self.assertIn('🥕', embed.description)
        panel.stop()

    async def test_action_updates_public_panel_but_keeps_private_controls(self):
        state = self.state(1800)
        private_message = AsyncMock()
        public_message = AsyncMock()
        panel = bot.PublicFarmView(owner=1, name='농부', message=public_message)
        bot.public_farms[(1, 100)] = panel
        farm = bot.FarmView(1, state)
        interaction = SimpleNamespace(id=789, response=SimpleNamespace(edit_message=AsyncMock()),
            original_response=AsyncMock(return_value=private_message))
        try:
            with patch.object(bot.game, 'act', return_value=(state, '심기 완료')), patch.object(bot.game, 'read', return_value=state):
                await farm.action(interaction, 'plant', crop='carrot', row=0)
            public_message.edit.assert_awaited_once()
            self.assertIsInstance(public_message.edit.call_args.kwargs['view'], bot.PublicFarmView)
            returned = interaction.response.edit_message.call_args.kwargs['view']
            self.assertIsInstance(returned, bot.FarmView)
            returned.stop()
        finally:
            panel.stop()
            bot.public_farms.clear()
