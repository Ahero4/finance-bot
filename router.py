import logging
import asyncio
import re
from datetime import datetime
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.filters.state import StateFilter
from sqlalchemy.exc import SQLAlchemyError
from states import Add, Get, Settings, Period
import kb
import db.request as db
from charts import render_chart

router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
logger = logging.getLogger(__name__)


def split_report(text, limit=4000):
    """Telegram limits text in UTF-16 units, including astral characters."""
    chunk, units = [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > limit:
            yield "".join(chunk)
            chunk, units = [], 0
        chunk.append(char)
        units += size
    if chunk:
        yield "".join(chunk)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        text=f"👋 Привет, {message.from_user.first_name}\nЯ помогу вести учёт финансов. Что ты хочешь сделать?\n⚙️ /settings — валюта",
        reply_markup=kb.section,
    )


@router.callback_query(F.data == "exit")
async def back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await callback.message.answer(text="🏠 Что ты хочешь сделать?", reply_markup=kb.section)


@router.message(Command("settings"))
async def settings(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Settings.currency)
    try:
        currency = await db.get_currency(message.from_user.id)
    except (SQLAlchemyError, OSError):
        logger.exception("Settings read failed")
        await message.answer("⚠️ Не удалось загрузить настройки. Повторите /settings.")
        return
    await message.answer(f"⚙️ Валюта: {currency} {db.CURRENCIES[currency]}\nВыберите валюту отображения. Пересчёта сумм не будет.", reply_markup=kb.currency_settings)


@router.callback_query(Settings.currency, F.data.startswith("settings:currency:"))
async def save_currency(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    currency = callback.data.removeprefix("settings:currency:")
    if currency not in db.CURRENCIES:
        await callback.message.answer("⚠️ Выберите RUB или USD", reply_markup=kb.currency_settings)
        return
    try:
        await db.set_currency(callback.from_user.id, currency)
    except (SQLAlchemyError, OSError):
        logger.exception("Settings save failed")
        await callback.message.answer("⚠️ Не удалось сохранить валюту. Повторите выбор.", reply_markup=kb.currency_settings)
        return
    await state.clear()
    await callback.message.answer(f"✅ Валюта сохранена: {currency} {db.CURRENCIES[currency]}", reply_markup=kb.section)


RANGE_PROMPT = "🗓 Введите период: ДД.ММ.ГГГГ - ДД.ММ.ГГГГ\nНапример: 11.09.2026 - 13.10.2026. Последний день включён."


@router.callback_query(StateFilter(None), F.data.in_({"report:custom", "chart:custom"}))
async def begin_custom_period(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await state.update_data(target=callback.data.split(':')[0])
    await state.set_state(Period.range)
    await callback.message.answer(RANGE_PROMPT, reply_markup=kb.exit)


def parse_range(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}\s*-\s*[0-9]{2}\.[0-9]{2}\.[0-9]{4}", value.strip()):
        raise ValueError("Invalid date range")
    first, last = value.strip().split('-')
    def parse(part):
        day, month, year = map(int, part.strip().split('.'))
        return datetime(year, month, day).date()
    start, end = parse(first), parse(last)
    if start > end:
        raise ValueError("Invalid date order")
    return start, end


@router.message(Period.range)
async def custom_period(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        start, end = parse_range(message.text)
        if data.get('target') not in {'report', 'chart'}:
            raise ValueError("Invalid target")
    except ValueError:
        await message.answer("⚠️ Проверьте даты и их порядок.\n" + RANGE_PROMPT, reply_markup=kb.exit)
        return
    try:
        if data['target'] == 'report':
            result = await db.get_custom_report(message.from_user.id, start, end)
        else:
            result = await db.get_custom_chart_data(message.from_user.id, start, end)
    except (SQLAlchemyError, OSError):
        logger.exception("Custom period query failed")
        await message.answer("⚠️ Не удалось загрузить расходы. Повторите период.", reply_markup=kb.exit)
        return
    await state.clear()
    if data['target'] == 'report':
        title = f"📊 {start.day:02}.{start.month:02}.{start.year:04} — {end.day:02}.{end.month:02}.{end.year:04}"
        await send_text_report(message, title + '\n\n' + result)
    else:
        await send_chart_result(message, result)


@router.callback_query(StateFilter(None), F.data == "add")
async def begin_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Add.sum)
    await callback.answer()
    await callback.message.answer("💸 Введите целую положительную сумму без разделителей", reply_markup=kb.exit)


@router.message(Add.sum)
async def add_sum(message: Message, state: FSMContext):
    try:
        amount = int(message.text)
        if not 1 <= amount <= db.MAX_AMOUNT:
            raise ValueError
    except (TypeError, ValueError):
        await message.answer(f"⚠️ Введите целую положительную сумму без разделителей (не больше {db.format_amount(db.MAX_AMOUNT)})")
        return
    await state.update_data(sum=amount)
    await state.set_state(Add.description)
    await message.answer("📝 Введите описание", reply_markup=kb.exit)


@router.message(Add.description)
async def add_description(message: Message, state: FSMContext):
    data = await state.get_data()
    data["description"] = message.text
    try:
        db.validate_expense(data)
    except ValueError as error:
        await message.answer(str(error))
        return
    try:
        currency = await db.get_currency(message.from_user.id)
        await db.add(user_id=message.from_user.id, data=data)
    except (SQLAlchemyError, OSError, OverflowError):
        logger.exception("Expense save failed")
        await message.answer("⚠️ Не удалось сохранить трату. Повторите описание позже или отмените действие.")
        return
    # Persistence succeeded: finish before any network acknowledgement.
    await state.clear()
    await message.answer(
        f"✅ Трата успешно добавлена\n💸 Сумма: {db.format_money(data['sum'], currency)}\n📝 Описание: {data['description']}",
        reply_markup=kb.section,
    )


@router.callback_query(StateFilter(None), F.data == "show")
async def show(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    now = datetime.now()
    await send_report(callback, state, {'year': now.year, 'month': now.month})


async def send_report(callback, state, data):
    try:
        expenses = await db.getAll(user_id=callback.from_user.id, data=data)
    except (SQLAlchemyError, OSError):
        logger.exception("Report read failed")
        await callback.message.answer("⚠️ Не удалось загрузить отчёт. Повторите выбор.")
        return
    await state.clear()
    text = f"📊 {kb.months[data['month']]} {data['year']}\n\n{expenses}"
    await send_text_report(callback.message, text)


async def send_text_report(message, text):
    chunks = list(split_report(text))
    for index, chunk in enumerate(chunks):
        await message.answer(text=chunk, reply_markup=kb.report if index == len(chunks) - 1 else None)


@router.callback_query(StateFilter(None), F.data == "choose_period")
async def choose_period(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    years = await db.getYears(callback.from_user.id)
    if not years:
        await callback.message.answer("📭 У вас нет ни одной траты", reply_markup=kb.report)
    else:
        await state.set_state(Get.year)
        await callback.message.answer("📅 Выберите год", reply_markup=kb.getYearsButton(years=years))


@router.callback_query(StateFilter(None), F.data == "charts")
async def charts_menu(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("📊 Выберите период графика", reply_markup=kb.chart_periods)


@router.callback_query(StateFilter(None), F.data.startswith("chart:"))
async def chart(callback: CallbackQuery):
    await callback.answer()
    period = callback.data.removeprefix("chart:")
    if period not in {"week", "month", "year"}:
        await callback.message.answer("⚠️ Выберите неделю, месяц или год", reply_markup=kb.chart_periods)
        return
    now = datetime.now()
    try:
        data = await db.get_chart_data(callback.from_user.id, period, now)
    except (SQLAlchemyError, OSError):
        logger.exception("Chart query failed")
        await callback.message.answer("⚠️ Не удалось загрузить расходы. Повторите выбор.", reply_markup=kb.chart_periods)
        return
    await send_chart_result(callback.message, data)


async def send_chart_result(message, data):
    if not data['has_records']:
        await message.answer("📭 В этом периоде не было трат", reply_markup=kb.chart_periods)
        return
    try:
        png = await asyncio.to_thread(render_chart, data)
    except (ValueError, RuntimeError, OSError):
        logger.exception("Chart render failed")
        await message.answer("⚠️ Не удалось построить график. Повторите выбор.", reply_markup=kb.chart_periods)
        return
    await message.answer_photo(BufferedInputFile(png, filename="expenses.png"), caption=f"📊 {data['title']}", reply_markup=kb.chart_periods)


def callback_number(data, prefix, maximum):
    try:
        value = int(data.removeprefix(prefix))
        if not data.startswith(prefix) or not 1 <= value <= maximum:
            raise ValueError
        return value
    except (AttributeError, TypeError, ValueError):
        raise ValueError("⚠️ Я вас не понял") from None


@router.callback_query(Get.year, F.data.startswith("year:"))
async def year(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        value = callback_number(callback.data, "year:", 9999)
    except ValueError:
        await callback.message.answer("⚠️ Выберите корректный год")
        return
    await state.update_data(year=value)
    await state.set_state(Get.month)
    await callback.message.answer("📅 Выберите месяц", reply_markup=kb.getMonths())


@router.callback_query(Get.month, F.data.startswith("month:"))
async def month(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        value = callback_number(callback.data, "month:", 12)
    except ValueError:
        await callback.message.answer("⚠️ Выберите корректный месяц", reply_markup=kb.exit)
        return
    await state.update_data(month=value)
    data = await state.get_data()
    await send_report(callback, state, data)
