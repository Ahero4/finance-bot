from aiogram.types import ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton


months = {
    1: "Январь",
    2: "Февраль",
    3: "Март",
    4: "Апрель",
    5: "Май",
    6: "Июнь",
    7: "Июль",
    8: "Август",
    9: "Сентябрь",
    10: "Октябрь",
    11: "Ноябрь",
    12: "Декабрь",
}

section = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Добавить трату", callback_data="add"), InlineKeyboardButton(text="Показать траты", callback_data="show")]
    ]
)

exit = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Выйти", callback_data="exit")]])


def getYearsButton(years: list):
    YearsButton = [[InlineKeyboardButton(text=f"{year}", callback_data=f"year:{year}")] for year in years]
    YearsButton.append([InlineKeyboardButton(text="Отменить", callback_data="exit")])
    return InlineKeyboardMarkup(inline_keyboard=YearsButton)


def getMonths():
    monthsButton = []
    row = []
    for num, name in months.items():
        row.append(InlineKeyboardButton(text=name, callback_data=f"month:{num}"))
        if len(row) == 3:
            monthsButton.append(row)
            row = []
    if row:
        monthsButton.append(row)

    monthsButton.append([InlineKeyboardButton(text="Отменить", callback_data="exit")])
    return InlineKeyboardMarkup(inline_keyboard=monthsButton)
