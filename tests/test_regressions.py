import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from pathlib import Path

# Never bind tests to the operational database, even at import time.
_import_tmp = tempfile.TemporaryDirectory()
os.environ['DATABASE_URL'] = 'sqlite+aiosqlite:///' + (Path(_import_tmp.name) / 'unused.sqlite').as_posix()
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select, func
from sqlalchemy.exc import SQLAlchemyError
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.context import FSMContext
import db.models as models
import db.request as db
import router as handlers
from states import Add, Get


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


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=123456, chat_id=10, user_id=10))
        self.message = SimpleNamespace(text=None, from_user=SimpleNamespace(id=10, first_name='Test'), answer=AsyncMock())

    async def asyncTearDown(self):
        await self.storage.close()

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
        self.assertEqual(''.join(c.kwargs['text'] for c in self.message.answer.call_args_list), report)

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
                    for data in ['add', 'show', 'exit', 'year:2024', 'month:1']:
                        await dispatcher.feed_update(bot, Update.model_validate({'update_id': 3, 'callback_query': {'id': 'c', 'from': user, 'chat_instance': 'test', 'data': data, 'message': message(kind)}}))
                outgoing.assert_not_awaited()
                query.assert_not_awaited()
                save.assert_not_awaited()
                await dispatcher.feed_update(bot, Update.model_validate({'update_id': 4, 'message': message('private')}))
                outgoing.assert_awaited_once()
                self.assertIsNone(await self.state.get_state())
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
