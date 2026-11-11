from aiogram.fsm.state import State, StatesGroup


class Add(StatesGroup):
    sum = State()
    description = State()


class Get(StatesGroup):
    year = State()
    month = State()
