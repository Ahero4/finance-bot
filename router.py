import logging
from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.filters.state import StateFilter
from sqlalchemy.exc import SQLAlchemyError
from states import Add, Get
import kb
import db.request as db

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
        text=f"Привет, {message.from_user.first_name}\nЯ бот-помощник для ведения учета личных финансов. Что ты хочешь сделать?",
        reply_markup=kb.section,
    )


@router.callback_query(F.data == "exit")
async def back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await callback.message.answer(text="Что ты хочешь сделать?", reply_markup=kb.section)


@router.callback_query(StateFilter(None), F.data == "add")
async def begin_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Add.sum)
    await callback.answer()
    await callback.message.answer("Введите сумму", reply_markup=kb.exit)


@router.message(Add.sum)
async def add_sum(message: Message, state: FSMContext):
    try:
        amount = int(message.text)
        if not 1 <= amount <= db.MAX_AMOUNT:
            raise ValueError
    except (TypeError, ValueError):
        await message.answer("Введите целую положительную сумму рублей (не больше 9223372036854775807)")
        return
    await state.update_data(sum=amount)
    await state.set_state(Add.description)
    await message.answer("Введите описание", reply_markup=kb.exit)


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
        await db.add(user_id=message.from_user.id, data=data)
    except (SQLAlchemyError, OSError, OverflowError):
        logger.exception("Expense save failed")
        await message.answer("Не удалось сохранить трату. Повторите описание позже или отмените действие.")
        return
    # Persistence succeeded: finish before any network acknowledgement.
    await state.clear()
    await message.answer(
        f"Трата успешно добавлена\nСумма: {data['sum']}\nОписание: {data['description']}",
        reply_markup=kb.section,
    )


@router.callback_query(StateFilter(None), F.data == "show")
async def show(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    years = await db.getYears(callback.from_user.id)
    if not years:
        await callback.message.answer("У вас нет ни одной траты")
    else:
        await state.set_state(Get.year)
        await callback.message.answer("Выбери год", reply_markup=kb.getYearsButton(years=years))


def callback_number(data, prefix, maximum):
    try:
        value = int(data.removeprefix(prefix))
        if not data.startswith(prefix) or not 1 <= value <= maximum:
            raise ValueError
        return value
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Я вас не понял") from None


@router.callback_query(Get.year, F.data.startswith("year:"))
async def year(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        value = callback_number(callback.data, "year:", 9999)
    except ValueError:
        await callback.message.answer("Выберите корректный год")
        return
    await state.update_data(year=value)
    await state.set_state(Get.month)
    await callback.message.answer("Выберите месяц", reply_markup=kb.getMonths())


@router.callback_query(Get.month, F.data.startswith("month:"))
async def month(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        value = callback_number(callback.data, "month:", 12)
    except ValueError:
        await callback.message.answer("Выберите корректный месяц", reply_markup=kb.exit)
        return
    await state.update_data(month=value)
    data = await state.get_data()
    expenses = await db.getAll(user_id=callback.from_user.id, data=data)
    for chunk in split_report(expenses):
        await callback.message.answer(text=chunk, reply_markup=kb.exit)
