from datetime import datetime
from db.models import async_session, Expense
from sqlalchemy import select, func

MAX_AMOUNT = 2**63 - 1
MAX_DESCRIPTION = 255


def validate_expense(data):
    amount = data.get("sum")
    description = data.get("description")
    if type(amount) is not int or not 1 <= amount <= MAX_AMOUNT:
        raise ValueError("Сумма должна быть целым положительным числом рублей в диапазоне SQLite")
    if not isinstance(description, str) or not description.strip() or len(description) > MAX_DESCRIPTION:
        raise ValueError("Введите текстовое описание от 1 до 255 символов")


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
    query = select(Expense).where(Expense.user_id == user_id, Expense.date >= start_date)
    if (year, month) == (9999, 12):
        query = query.where(Expense.date <= datetime.max)
    else:
        end_date = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1)
        query = query.where(Expense.date < end_date)
    async with async_session() as session:
        expenses = await session.scalars(query.order_by(Expense.date, Expense.id))
        return answerExpenses(expenses.all())


def answerExpenses(expenses):
    if not expenses:
        return "В этом месяце не было трат"
    total = 0
    answer = []
    for exp in expenses:
        date = exp.date.strftime("%d.%m.%Y")
        answer.append(f"{date}\n{exp.sum}\n{exp.description}")
        total += exp.sum
    answer.append("===============")
    answer.append(f"Всего: {total} руб.")
    return "\n\n".join(answer)
