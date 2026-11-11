from datetime import datetime, timedelta
from math import ceil
from calendar import monthrange
from db.models import async_session, Expense, UserSettings
from sqlalchemy import select, func
from sqlalchemy.dialects.sqlite import insert

MAX_AMOUNT = 2**63 - 1
MAX_DESCRIPTION = 255
CURRENCIES = {"RUB": "₽", "USD": "$"}


def format_amount(amount: int) -> str:
    return f"{amount:,}".replace(",", ".")


def format_money(amount: int, currency: str = "RUB") -> str:
    return f"{format_amount(amount)} {CURRENCIES[currency]}"


async def get_currency(user_id):
    async with async_session() as session:
        return await session.scalar(select(UserSettings.currency).where(UserSettings.user_id == user_id)) or "RUB"


async def set_currency(user_id, currency):
    if currency not in CURRENCIES:
        raise ValueError("Unsupported currency")
    async with async_session() as session:
        statement = insert(UserSettings).values(user_id=user_id, currency=currency)
        await session.execute(statement.on_conflict_do_update(index_elements=[UserSettings.user_id], set_={"currency": currency}))
        await session.commit()


def validate_expense(data):
    amount = data.get("sum")
    description = data.get("description")
    if type(amount) is not int or not 1 <= amount <= MAX_AMOUNT:
        raise ValueError("⚠️ Сумма должна быть целым положительным числом в допустимом диапазоне")
    if not isinstance(description, str) or not description.strip() or len(description) > MAX_DESCRIPTION:
        raise ValueError("⚠️ Введите текстовое описание от 1 до 255 символов")


async def add(user_id, data):
    validate_expense(data)
    async with async_session() as session:
        session.add(Expense(user_id=user_id, sum=data["sum"], description=data["description"], date=datetime.now()))
        await session.commit()


async def getYears(user_id):
    async with async_session() as session:
        years = await session.scalars(
            select(func.strftime("%Y", Expense.date).label("year")).where(Expense.user_id == user_id).distinct().order_by("year")
        )
        return years.all()


async def getAll(user_id, data):
    year, month = data['year'], data['month']
    if type(year) is not int or type(month) is not int:
        raise ValueError("Invalid year or month")
    start_date = datetime(year, month, 1)
    if (year, month) == (9999, 12):
        end_date = None
    else:
        end_date = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1)
    expenses, currency = await get_range_expenses(user_id, start_date, end_date)
    return answerExpenses(expenses, currency)


async def get_range_expenses(user_id, start, end):
    currency = await get_currency(user_id)
    query = select(Expense).where(Expense.user_id == user_id, Expense.date >= start)
    query = query.where(Expense.date < end) if end is not None else query.where(Expense.date <= datetime.max)
    async with async_session() as session:
        expenses = await session.scalars(query.order_by(Expense.date, Expense.id))
        return expenses.all(), currency


def custom_bounds(start, end):
    if start > end:
        raise ValueError("Invalid range")
    first = datetime(start.year, start.month, start.day)
    last = None if end == datetime.max.date() else datetime(end.year, end.month, end.day) + timedelta(days=1)
    return first, last


async def get_custom_report(user_id, start, end):
    expenses, currency = await get_range_expenses(user_id, *custom_bounds(start, end))
    return answerExpenses(expenses, currency)


async def get_chart_data(user_id, period, now):
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start, end = today - timedelta(days=6), today + timedelta(days=1)
        dates = [start + timedelta(days=i) for i in range(7)]
    elif period == "month":
        start = today.replace(day=1)
        end = start + timedelta(days=monthrange(start.year, start.month)[1])
        dates = [start + timedelta(days=i) for i in range((end - start).days)]
    elif period == "year":
        start, end = today.replace(month=1, day=1), datetime(today.year + 1, 1, 1)
        dates = [datetime(today.year, month, 1) for month in range(1, 13)]
    else:
        raise ValueError("Unsupported chart period")
    rows, currency = await get_range_expenses(user_id, start, end)
    totals = {date: 0 for date in dates}
    for expense in rows:
        date, amount = expense.date, expense.sum
        bucket = date.replace(day=1, hour=0, minute=0, second=0, microsecond=0) if period == "year" else date.replace(hour=0, minute=0, second=0, microsecond=0)
        totals[bucket] += amount
    return {
        "labels": [date.strftime("%m" if period == "year" else "%d.%m") for date in dates],
        "amounts": list(totals.values()), "has_records": bool(rows), "currency": currency,
        "title": {"week": "Последние 7 дней", "month": "Текущий месяц", "year": "Текущий год"}[period],
    }


async def get_custom_chart_data(user_id, start, end):
    rows, currency = await get_range_expenses(user_id, *custom_bounds(start, end))
    days = (end - start).days + 1
    months = (end.year - start.year) * 12 + end.month - start.month + 1
    if days <= 62:
        keys = [start + timedelta(days=i) for i in range(days)]
        labels = [f"{day.day:02}.{day.month:02}" for day in keys]
        bucket = lambda value: value.date()
        aggregation = "по дням"
    elif months <= 24:
        first = start.year * 12 + start.month - 1
        keys = list(range(first, first + months))
        labels = [f"{value % 12 + 1:02}.{value // 12:04}" for value in keys]
        bucket = lambda value: value.year * 12 + value.month - 1
        aggregation = "по месяцам"
    else:
        step = ceil((end.year - start.year + 1) / 24)
        keys = list(range(start.year, end.year + 1, step))
        labels = [f"{year:04}" if step == 1 else f"{year:04}–{min(year + step - 1, end.year):04}" for year in keys]
        bucket = lambda value: start.year + (value.year - start.year) // step * step
        aggregation = "по годам" if step == 1 else "по группам лет"
    totals = dict.fromkeys(keys, 0)
    for expense in rows:
        totals[bucket(expense.date)] += expense.sum
    return {"labels": labels, "amounts": list(totals.values()), "currency": currency,
            "has_records": bool(rows), "title": f"{start.day:02}.{start.month:02}.{start.year:04} — {end.day:02}.{end.month:02}.{end.year:04} ({aggregation})"}


def answerExpenses(expenses, currency="RUB"):
    if not expenses:
        return "📭 В этом периоде не было трат"
    total = 0
    answer = []
    for exp in expenses:
        date = f"{exp.date.day:02}.{exp.date.month:02}.{exp.date.year:04}"
        answer.append(f"📅 {date}\n💸 {format_money(exp.sum, currency)}\n📝 {exp.description}")
        total += exp.sum
    answer.append("===============")
    answer.append(f"💰 Всего: {format_money(total, currency)}")
    return "\n\n".join(answer)
