import os
import asyncio
import tempfile
import unittest
from datetime import datetime, date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from pathlib import Path

# Never bind tests to the operational database, even at import time.
_import_tmp = tempfile.TemporaryDirectory()
os.environ['DATABASE_URL'] = 'sqlite+aiosqlite:///' + (Path(_import_tmp.name) / 'unused.sqlite').as_posix()
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select, func, text
from sqlalchemy.exc import SQLAlchemyError
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.context import FSMContext
import db.models as models
import db.request as db
import router as handlers
from charts import render_chart
from states import Add, Get, Settings, Period


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine('sqlite+aiosqlite:///' + (Path(self.tmp.name) / 'test.sqlite').as_posix())
        self.sessions = async_sessionmaker(self.engine)
        self.patch = patch.object(db, 'async_session', self.sessions)
        self.patch.start()
        async with self.engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)

    async def asyncTearDown(self):
        self.patch.stop()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_month_edges_and_user_isolation(self):
        async with self.sessions() as session:
            for user, date, label in [
                (1, datetime(2024, 2, 29, 23, 59, 59, 999999), 'leap-edge'),
                (1, datetime(2024, 3, 1), 'next-month'),
                (2, datetime(2024, 2, 1), 'other-user'),
                (1, datetime(2024, 12, 31, 23, 59, 59, 999999), 'dec-edge'),
                (1, datetime(2025, 1, 1), 'next-year'),
                (1, datetime.max, 'max-date'),
            ]:
                session.add(models.Expense(user_id=user, sum=10, description=label, date=date))
            await session.commit()
        text = await db.getAll(1, {'year': 2024, 'month': 2})
        self.assertIn('leap-edge', text)
        self.assertNotIn('next-month', text)
        self.assertNotIn('other-user', text)
        self.assertIn('Всего: 10', text)
        december = await db.getAll(1, {'year': 2024, 'month': 12})
        self.assertIn('dec-edge', december)
        self.assertNotIn('next-year', december)
        self.assertIn('max-date', await db.getAll(1, {'year': 9999, 'month': 12}))
        self.assertEqual(await db.getYears(2), ['2024'])
        self.assertEqual(await db.getYears(3), [])

    async def test_validation_and_successful_save(self):
        for amount in [0, -1, 2**63, None, True, '1']:
            with self.assertRaises(ValueError):
                await db.add(1, {'sum': amount, 'description': 'ok'})
        for description in [None, '', '  ', 'x' * 256]:
            with self.assertRaises(ValueError):
                await db.add(1, {'sum': 1, 'description': description})
        await db.add(1, {'sum': db.MAX_AMOUNT, 'description': 'x' * 255})
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(models.Expense)), 1)

    async def test_settings_upgrade_isolation_upsert_and_persistence(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(models.UserSettings.__table__.drop)
            await conn.execute(text("INSERT INTO expenses (user_id, sum, description, date) VALUES (1, 25000, 'legacy', '2026-11-11 00:00:00.000000')"))
            before = (await conn.execute(text('PRAGMA table_info(expenses)'))).all()
            await conn.run_sync(models.Base.metadata.create_all)
            self.assertEqual((await conn.execute(text('PRAGMA table_info(expenses)'))).all(), before)
        self.assertEqual(await db.get_currency(1), 'RUB')
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(models.UserSettings)), 0)
        await asyncio.gather(db.set_currency(1, 'USD'), db.set_currency(1, 'USD'))
        await db.set_currency(1, 'USD')
        self.assertEqual(await db.get_currency(1), 'USD')
        self.assertEqual(await db.get_currency(2), 'RUB')
        with self.assertRaises(ValueError):
            await db.set_currency(1, 'EUR')
        usd = await db.getAll(1, {'year': 2026, 'month': 11})
        self.assertIn('25.000 $', usd)
        await db.set_currency(1, 'RUB')
        self.assertIn('25.000 ₽', await db.getAll(1, {'year': 2026, 'month': 11}))
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(models.UserSettings)), 1)
            self.assertEqual(await session.scalar(select(models.Expense.sum)), 25000)

    async def test_chart_periods_zero_fill_and_integer_totals(self):
        async with self.sessions() as session:
            for user, date, amount in [
                (1, datetime(2024, 2, 23), 1), (1, datetime(2024, 2, 29, 23, 59, 59, 999999), 25000),
                (1, datetime(2024, 3, 1), 10), (2, datetime(2024, 2, 25), 999),
                (1, datetime(2024, 1, 1), db.MAX_AMOUNT), (1, datetime(2024, 1, 1), db.MAX_AMOUNT),
                (3, datetime(2024, 2, 29), 0), (1, datetime(2025, 1, 1), 1),
            ]:
                session.add(models.Expense(user_id=user, sum=amount, description='chart', date=date))
            await session.commit()
        now = datetime(2024, 2, 29, 12)
        week = await db.get_chart_data(1, 'week', now)
        self.assertEqual(week['labels'], ['23.02', '24.02', '25.02', '26.02', '27.02', '28.02', '29.02'])
        self.assertEqual(week['amounts'], [1, 0, 0, 0, 0, 0, 25000])
        month = await db.get_chart_data(1, 'month', now)
        self.assertEqual(len(month['amounts']), 29)
        self.assertEqual(sum(month['amounts']), 25001)
        year = await db.get_chart_data(1, 'year', now)
        self.assertEqual(len(year['amounts']), 12)
        self.assertEqual(year['amounts'][:3], [2 * db.MAX_AMOUNT, 25001, 10])
        self.assertTrue((await db.get_chart_data(3, 'week', now))['has_records'])
        self.assertFalse((await db.get_chart_data(4, 'week', now))['has_records'])
        png = render_chart(week)
        self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertGreater(len(png), 1000)

    async def test_custom_ranges_inclusive_extremes_isolation_and_adaptive_buckets(self):
        async with self.sessions() as session:
            for user, when, amount in [
                (1, datetime(1, 1, 1), 1), (1, datetime(2024, 2, 29), 25000),
                (1, datetime(2024, 2, 29, 23, 59, 59, 999999), 1000),
                (1, datetime(2024, 3, 1), 999), (2, datetime(2024, 2, 29), 1000000),
                (1, datetime.max, 10),
            ]:
                session.add(models.Expense(user_id=user, sum=amount, description='custom', date=when))
            await session.commit()
        start = end = date(2024, 2, 29)
        report = await db.get_custom_report(1, start, end)
        self.assertIn('Всего: 26.000 ₽', report)
        self.assertNotIn('999 ₽', report)
        chart = await db.get_custom_chart_data(1, start, end)
        self.assertEqual(chart['amounts'], [26000])
        self.assertIn('по дням', chart['title'])
        month = await db.get_custom_chart_data(1, date(2024, 1, 1), date(2024, 4, 30))
        self.assertEqual(month['amounts'], [0, 26000, 999, 0])
        self.assertIn('по месяцам', month['title'])
        year = await db.get_custom_chart_data(1, date(2020, 1, 1), date(2026, 12, 31))
        self.assertEqual(len(year['amounts']), 7)
        self.assertIn('по годам', year['title'])
        huge = await db.get_custom_chart_data(1, date.min, date.max)
        self.assertLessEqual(len(huge['amounts']), 24)
        self.assertEqual(sum(huge['amounts']), 27010)
        self.assertIn('01.01.0001', huge['title'])
        self.assertIn('по группам лет', huge['title'])
        self.assertIn('10 ₽', await db.get_custom_report(1, date.max, date.max))
        self.assertFalse((await db.get_custom_chart_data(3, date.min, date.max))['has_records'])


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=123456, chat_id=10, user_id=10))
        self.message = SimpleNamespace(text=None, from_user=SimpleNamespace(id=10, first_name='Test'), answer=AsyncMock())
        self.currency_patch = patch.object(db, 'get_currency', AsyncMock(return_value='RUB'))
        self.currency_patch.start()

    async def asyncTearDown(self):
        self.currency_patch.stop()
        await self.storage.close()

    async def test_amount_display_in_report_and_confirmation_keeps_integer_input(self):
        amounts = [999, 1000, 25000, 1000000, db.MAX_AMOUNT, 0, -1000]
        rows = [SimpleNamespace(date=datetime(2026, 11, 11), sum=amount, description='food') for amount in amounts]
        report = db.answerExpenses(rows)
        for expected in ['999', '1.000', '25.000', '1.000.000', '9.223.372.036.854.775.807', '0', '-1.000']:
            self.assertIn(f'💸 {expected} ₽\n📝 food', report)
        self.assertTrue(report.endswith('Всего: 9.223.372.036.855.801.806 ₽'))
        self.assertEqual(db.answerExpenses([]), '📭 В этом периоде не было трат')
        await self.state.set_state(Add.sum)
        await handlers.add_sum(self.message, self.state)
        self.assertIn('9.223.372.036.854.775.807', self.message.answer.call_args.args[0])
        self.message.text = '25000'
        await handlers.add_sum(self.message, self.state)
        self.message.text = 'food'
        with patch.object(db, 'add', AsyncMock()) as save, patch.object(db, 'get_currency', AsyncMock(return_value='USD')):
            await handlers.add_description(self.message, self.state)
        save.assert_awaited_once_with(user_id=10, data={'sum': 25000, 'description': 'food'})
        self.assertIs(type(save.call_args.kwargs['data']['sum']), int)
        self.assertIn('Сумма: 25.000 $\n', self.message.answer.call_args.args[0])

    async def test_invalid_amount_and_description(self):
        await self.state.set_state(Add.sum)
        for text in [None, 'no', '-1', '0', str(2**63)]:
            self.message.text = text
            await handlers.add_sum(self.message, self.state)
            self.assertEqual(await self.state.get_state(), Add.sum.state)
        self.message.text = '10'
        await handlers.add_sum(self.message, self.state)
        self.assertEqual(await self.state.get_state(), Add.description.state)
        for text in [None, '', 'x' * 256]:
            self.message.text = text
            with patch.object(db, 'add', AsyncMock()) as save:
                await handlers.add_description(self.message, self.state)
                save.assert_not_awaited()

    async def test_save_failure_preserves_state_and_network_failure_finishes_state(self):
        await self.state.set_state(Add.description)
        await self.state.update_data(sum=10)
        self.message.text = 'food'
        with patch.object(db, 'add', AsyncMock(side_effect=SQLAlchemyError('test failure'))):
            await handlers.add_description(self.message, self.state)
        self.assertEqual(await self.state.get_state(), Add.description.state)
        self.assertNotIn('успешно', self.message.answer.call_args.args[0])
        self.message.answer.side_effect = RuntimeError('network down')
        with patch.object(db, 'add', AsyncMock()) as save:
            with self.assertRaises(RuntimeError):
                await handlers.add_description(self.message, self.state)
            save.assert_awaited_once()
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})

    async def test_start_resets_and_bad_callbacks_do_not_advance(self):
        await self.state.set_state(Add.description)
        await self.state.update_data(sum=1)
        await handlers.cmd_start(self.message, self.state)
        self.assertIsNone(await self.state.get_state())
        callback = SimpleNamespace(data=None, answer=AsyncMock(), message=self.message)
        await self.state.set_state(Get.year)
        for data in ['year:0', 'year:10000', 'year:no']:
            callback.data = data
            await handlers.year(callback, self.state)
            self.assertEqual(await self.state.get_state(), Get.year.state)
        callback.data = 'year:2024'
        await handlers.year(callback, self.state)
        for data in ['month:0', 'month:13', 'month:no']:
            callback.data = data
            with patch.object(db, 'getAll', AsyncMock()) as query:
                await handlers.month(callback, self.state)
                query.assert_not_awaited()

    async def test_report_chunks_preserve_text_and_utf16_limit(self):
        report = ('2024 food 😀\n' * 1500)
        chunks = list(handlers.split_report(report))
        self.assertEqual(''.join(chunks), report)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c.encode('utf-16-le')) // 2 <= 4000 for c in chunks))
        await self.state.update_data(year=2024)
        callback = SimpleNamespace(data='month:1', answer=AsyncMock(), message=self.message, from_user=SimpleNamespace(id=10))
        with patch.object(db, 'getAll', AsyncMock(return_value=report)):
            await handlers.month(callback, self.state)
        self.assertEqual(''.join(c.kwargs['text'] for c in self.message.answer.call_args_list), '📊 Январь 2024\n\n' + report)
        self.assertIs(self.message.answer.call_args_list[-1].kwargs['reply_markup'], handlers.kb.report)
        self.assertTrue(all(c.kwargs['reply_markup'] is None for c in self.message.answer.call_args_list[:-1]))
        self.assertIsNone(await self.state.get_state())

    async def test_settings_failure_retry_and_current_month_navigation(self):
        await self.state.set_state(Add.description)
        await self.state.update_data(sum=10)
        await handlers.settings(self.message, self.state)
        self.assertEqual(await self.state.get_data(), {})
        callback = SimpleNamespace(data='settings:currency:EUR', answer=AsyncMock(), message=self.message, from_user=SimpleNamespace(id=10))
        with patch.object(db, 'set_currency', AsyncMock()) as save:
            await handlers.save_currency(callback, self.state)
            save.assert_not_awaited()
        callback.data = 'settings:currency:USD'
        with patch.object(db, 'set_currency', AsyncMock(side_effect=SQLAlchemyError('test'))):
            await handlers.save_currency(callback, self.state)
        self.assertEqual(await self.state.get_state(), Settings.currency.state)
        self.assertNotIn('сохранена', self.message.answer.call_args.args[0])
        with patch.object(db, 'set_currency', AsyncMock()) as save:
            await handlers.save_currency(callback, self.state)
            save.assert_awaited_once_with(10, 'USD')
        self.assertIsNone(await self.state.get_state())
        with patch.object(handlers, 'datetime') as clock, patch.object(db, 'getAll', AsyncMock(return_value='📭 В этом месяце не было трат')) as query, patch.object(db, 'getYears', AsyncMock(return_value=['2024'])) as years:
            clock.now.return_value = datetime(2026, 11, 11)
            await handlers.show(callback, self.state)
            query.assert_awaited_once_with(user_id=10, data={'year': 2026, 'month': 11})
            years.assert_not_awaited()
            self.assertIs(self.message.answer.call_args.kwargs['reply_markup'], handlers.kb.report)
            await handlers.choose_period(callback, self.state)
            self.assertEqual(await self.state.get_state(), Get.year.state)
            callback.data = 'year:2024'
            await handlers.year(callback, self.state)
            self.assertEqual(await self.state.get_state(), Get.month.state)
            callback.data = 'month:2'
            await handlers.month(callback, self.state)
            self.assertIsNone(await self.state.get_state())

    async def test_chart_route_empty_query_failure_render_failure_and_photo(self):
        callback = SimpleNamespace(data='chart:week', answer=AsyncMock(), message=self.message, from_user=SimpleNamespace(id=10))
        self.message.answer_photo = AsyncMock()
        data = {'labels': ['01.01'], 'amounts': [25000], 'has_records': False, 'currency': 'USD', 'title': 'Test'}
        with patch.object(db, 'get_chart_data', AsyncMock(return_value=data)), patch.object(handlers, 'render_chart') as render:
            await handlers.chart(callback)
            render.assert_not_called()
            self.message.answer_photo.assert_not_awaited()
        with patch.object(db, 'get_chart_data', AsyncMock(side_effect=SQLAlchemyError('test'))):
            await handlers.chart(callback)
            self.assertIn('Не удалось загрузить', self.message.answer.call_args.args[0])
        data['has_records'] = True
        with patch.object(db, 'get_chart_data', AsyncMock(return_value=data)), patch.object(handlers, 'render_chart', side_effect=RuntimeError('test')):
            await handlers.chart(callback)
            self.assertIn('Не удалось построить', self.message.answer.call_args.args[0])
        with patch.object(db, 'get_chart_data', AsyncMock(return_value=data)), patch.object(handlers, 'render_chart', return_value=b'png'):
            await handlers.chart(callback)
            self.message.answer_photo.assert_awaited_once()
            self.assertEqual(self.message.answer_photo.call_args.args[0].data, b'png')

    async def test_custom_range_validation_retry_and_success_clears_before_answer(self):
        await self.state.set_state(Period.range)
        await self.state.update_data(target='report')
        for value in [None, '1.01.2024 - 02.01.2024', '29.02.2023 - 01.03.2023', '02.01.2024 - 01.01.2024', '01.01.0000 - 01.01.2024']:
            self.message.text = value
            with patch.object(db, 'get_custom_report', AsyncMock()) as query:
                await handlers.custom_period(self.message, self.state)
                query.assert_not_awaited()
            self.assertEqual(await self.state.get_state(), Period.range.state)
        self.message.text = '01.01.0001 - 31.12.9999'
        await self.state.update_data(target='invalid')
        with patch.object(db, 'get_custom_report', AsyncMock()) as query:
            await handlers.custom_period(self.message, self.state)
            query.assert_not_awaited()
        await self.state.update_data(target='report')
        with patch.object(db, 'get_custom_report', AsyncMock(side_effect=SQLAlchemyError('test'))):
            await handlers.custom_period(self.message, self.state)
        self.assertEqual(await self.state.get_state(), Period.range.state)
        with patch.object(db, 'get_custom_report', AsyncMock(return_value='📭 Пусто')) as query:
            self.message.answer.side_effect = RuntimeError('network')
            with self.assertRaises(RuntimeError):
                await handlers.custom_period(self.message, self.state)
            query.assert_awaited_once_with(10, date.min, date.max)
        self.assertIsNone(await self.state.get_state())
        self.message.answer.side_effect = None
        callback = SimpleNamespace(data='chart:custom', answer=AsyncMock(), message=self.message)
        await handlers.begin_custom_period(callback, self.state)
        self.assertEqual(await self.state.get_data(), {'target': 'chart'})
        self.message.text = '29.02.2024 - 29.02.2024'
        with patch.object(db, 'get_custom_chart_data', AsyncMock(return_value={'has_records': False})) as query:
            await handlers.custom_period(self.message, self.state)
            query.assert_awaited_once_with(10, date(2024, 2, 29), date(2024, 2, 29))
        self.assertIsNone(await self.state.get_state())

    async def test_real_dispatcher_blocks_groups_and_accepts_private_start(self):
        dispatcher = Dispatcher(storage=self.storage, events_isolation=SimpleEventIsolation())
        dispatcher.include_router(handlers.router)
        bot = Bot('123456:TEST_TOKEN_FOR_OFFLINE_TESTS')
        user = {'id': 10, 'is_bot': False, 'first_name': 'Test'}
        def message(chat_type, text='/start'):
            return {'message_id': 1, 'date': 1700000000, 'chat': {'id': 10, 'type': chat_type}, 'from': user, 'text': text,
                    'entities': [{'type': 'bot_command', 'offset': 0, 'length': 6}] if text == '/start' else []}
        try:
            with patch.object(Bot, '__call__', AsyncMock()) as outgoing, patch.object(db, 'getYears', AsyncMock()) as query, patch.object(db, 'add', AsyncMock()) as save:
                for kind in ['group', 'supergroup']:
                    await dispatcher.feed_update(bot, Update.model_validate({'update_id': 1, 'message': message(kind)}))
                    await self.state.set_state(Add.description)
                    await self.state.update_data(sum=10)
                    await dispatcher.feed_update(bot, Update.model_validate({'update_id': 2, 'message': message(kind, 'secret expense')}))
                    for data in ['add', 'show', 'choose_period', 'report:custom', 'chart:custom', 'charts', 'chart:week', 'exit', 'year:2024', 'month:1', 'settings:currency:USD']:
                        await dispatcher.feed_update(bot, Update.model_validate({'update_id': 3, 'callback_query': {'id': 'c', 'from': user, 'chat_instance': 'test', 'data': data, 'message': message(kind)}}))
                outgoing.assert_not_awaited()
                query.assert_not_awaited()
                save.assert_not_awaited()
                await dispatcher.feed_update(bot, Update.model_validate({'update_id': 4, 'message': message('private')}))
                outgoing.assert_awaited_once()
                self.assertIsNone(await self.state.get_state())
                for add_state in [Add.sum, Add.description, Period.range]:
                    await self.state.set_state(add_state)
                    update_message = message('private', '/settings')
                    update_message['entities'] = [{'type': 'bot_command', 'offset': 0, 'length': 9}]
                    await dispatcher.feed_update(bot, Update.model_validate({'update_id': 5, 'message': update_message}))
                    self.assertEqual(await self.state.get_state(), Settings.currency.state)
                save.assert_not_awaited()
                await self.state.clear()
                with patch.object(db, 'set_currency', AsyncMock()) as settings_save:
                    await dispatcher.feed_update(bot, Update.model_validate({'update_id': 6, 'callback_query': {'id': 'stale', 'from': user, 'chat_instance': 'test', 'data': 'settings:currency:USD', 'message': message('private')}}))
                    settings_save.assert_not_awaited()
        finally:
            await bot.session.close()
            await dispatcher.fsm.close()


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_token_fails_before_database(self):
        import bot
        with patch.dict(os.environ, {'BOT_TOKEN': ''}), patch.object(bot, 'async_main', AsyncMock()) as initialize:
            with self.assertRaisesRegex(RuntimeError, 'BOT_TOKEN'):
                await bot.main()
            initialize.assert_not_awaited()

    def test_default_database_is_anchored_to_project(self):
        self.assertIn(Path(models.__file__).resolve().parent.parent.as_posix(), models.DEFAULT_DATABASE_URL)


if __name__ == '__main__':
    unittest.main()
