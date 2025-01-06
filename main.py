import logging
import os
import json
import secrets
import string
import subprocess
import uuid
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from throttling import ThrottlingMiddleware  # Импорт кастомного Middleware

# Загрузка конфигурации из .env
load_dotenv()
API_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
SBP_PHONE = os.getenv("SBP_PHONE")
TORR_SERVER_ADDRESS = os.getenv("TORR_SERVER_ADDRESS")
ADMIN_WALLET = os.getenv("ADMIN_WALLET")

# Пути к файлам аккаунтов и сроков действия
ACCS_DB_PATH = os.environ.get("ACCS_DB_PATH", "database/accs.db")
EXPIRY_DB_PATH = os.environ.get("EXPIRY_DB_PATH", "database/expiry.db")
TRIAL_USAGE_DB_PATH = os.environ.get("TRIAL_USAGE_DB_PATH", "database/trial_usage.db")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    filename="bot.log",
)
logger = logging.getLogger("main")

# Создание бота и диспетчера
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(ThrottlingMiddleware(rate_limit=1))  # 1 запрос в секунду
scheduler = AsyncIOScheduler()

# ====== Рестарт торрсервер ======
def restart_torrserver():
    """
    Перезагружает TorrServer.
    """
    try:
        subprocess.run(["systemctl", "restart", "torrserver"], check=True)
        logging.info("TorrServer успешно перезагружен.")
    except subprocess.CalledProcessError as e:
        logging.error(f"Ошибка при перезагрузке TorrServer: {e}")

# ====== Пробный период ======
def load_trial_usage():
    """
    Загружает данные о пользователях, использовавших пробный период.
    """
    if not os.path.exists(TRIAL_USAGE_DB_PATH):
        with open(TRIAL_USAGE_DB_PATH, "w") as file:
            json.dump([], file)
    with open(TRIAL_USAGE_DB_PATH, "r") as file:
        return json.load(file)


def save_trial_usage(trial_users):
    """
    Сохраняет данные о пользователях, использовавших пробный период.
    """
    with open(TRIAL_USAGE_DB_PATH, "w") as file:
        json.dump(trial_users, file, indent=4)

def check_if_trial(user_id):
    """
    Проверяет, является ли подписка пробной.
    """
    trial_users = load_trial_usage()
    return user_id in trial_users

def parse_expiry_date(expiry_str):
    """
    Универсальный парсер строки даты и времени.
    """
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(expiry_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"Неверный формат времени: {expiry_str}")


# ====== Клавиатура бота ======
def inline_main_menu():
    """
    Создаёт клавиатуру с кнопками под сообщением.
    """
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("💳 Оплатить подписку", callback_data="pay"),
        InlineKeyboardButton("📅 Проверить статус подписки", callback_data="status"),
        InlineKeyboardButton("🔑 Получить данные учётной записи", callback_data="get_account"),
        InlineKeyboardButton("🎁 Пробный период", callback_data="trial"),
        InlineKeyboardButton("💬 Чат поддержки", url="https://t.me/RUtils_TorrServer_chat")
    )
    return keyboard



# ====== Кнопка чата поддержки ======
def support_chat_button():
    """
    Создаёт клавиатуру с кнопкой для перехода в чат поддержки.
    """
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("Чат поддержки", url="https://t.me/RUtils_TorrServer_chat"))
    return keyboard

# ====== Кнопка возврата в основное меню ======
def back_to_main_menu():
    """
    Создаёт кнопку для возврата в главное меню.
    """
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu"))
    return keyboard

# ====== Напоминание об истечение подписки ======
def schedule_reminders():
    """
    Планирует уведомления за 3 дня до истечения подписки.
    """
    expiry = load_json(EXPIRY_DB_PATH)
    now = datetime.now()

    for username, expiry_str in expiry.items():
        try:
            expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d")
            reminder_date = expiry_date - timedelta(days=3)

            # Планируем уведомление, если дата напоминания ещё не прошла
            if reminder_date > now:
                user_id = int(username.replace("User", ""))  # Получаем ID пользователя
                scheduler.add_job(
                    send_reminder,
                    DateTrigger(run_date=reminder_date),
                    args=[user_id, expiry_date.strftime("%Y-%m-%d")],
                    id=f"reminder_{user_id}"
                )
        except Exception as e:
            logger.error(f"Ошибка при планировании напоминания для {username}: {e}")


async def send_reminder(user_id, expiry_date):
    """
    Отправляет напоминание пользователю.
    :param user_id: Telegram ID пользователя.
    :param expiry_date: Дата истечения подписки.
    """
    try:
        await bot.send_message(
            user_id,
            f"⚠️ Напоминание: ваша подписка истекает через 3 дня (дата истечения: {expiry_date}).\n"
            f"Продлите подписку, чтобы продолжить пользоваться сервисом."
        )
        logger.info(f"Напоминание отправлено пользователю {user_id}.")
    except Exception as e:
        logger.error(f"Ошибка при отправке напоминания для пользователя {user_id}: {e}")


async def on_startup(dp):
    """
    Инициализация перед запуском бота.
    """
    logger.info("Инициализация перед запуском бота...")

    # Запуск планировщика
    if not scheduler.running:
        scheduler.start()

    # Планирование напоминаний
    schedule_reminders()

# ====== Работа с TorrServer аккаунтами ======
def load_json(file_path):
    """
    Загрузка данных из JSON-файла.
    """
    if os.path.exists(file_path):
        with open(file_path, "r") as f:
            return json.load(f)
    return {}

def save_json(file_path, data):
    """
    Сохранение данных в JSON-файл.
    """
    with open(file_path, "w") as f:
        json.dump(data, f, indent=4)

def generate_password(length=12):
    """
    Генерирует случайный, безопасный пароль.
    """
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def create_or_extend_torr_account(user_id, additional_days):
    """
    Создаёт или продлевает учётную запись пользователя в TorrServer.
    """
    username = f"User{user_id}"
    password = generate_password()
    accs = load_json(ACCS_DB_PATH)
    expiry = load_json(EXPIRY_DB_PATH)

    # Проверяем текущий срок действия подписки
    current_expiry_str = expiry.get(username, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    try:
        current_expiry = parse_expiry_date(current_expiry_str)
    except ValueError as e:
        logging.error(f"Ошибка при разборе даты для {username}: {e}")
        current_expiry = datetime.now()

    # Вычисляем новый срок действия подписки
    new_expiry = max(current_expiry, datetime.now()) + timedelta(days=additional_days)
    expiry[username] = new_expiry.strftime("%Y-%m-%d %H:%M:%S")

    # Создаём или обновляем учётную запись
    accs[username] = accs.get(username, password)

    # Сохраняем изменения
    save_json(ACCS_DB_PATH, accs)
    save_json(EXPIRY_DB_PATH, expiry)

    # Перезагружаем TorrServer
    restart_torrserver()

    return username, accs[username], new_expiry.strftime("%Y-%m-%d %H:%M:%S")

def calculate_subscription_days(amount):
    """
    Возвращает количество дней подписки на основе суммы.
    """
    logging.info(f"Вычисляем дни подписки для суммы: {amount}")
    if amount in [100, 1]:  # 1 месяц
        return 30
    elif amount in [300, 3]:  # 3 месяца
        return 90
    elif amount in [600, 6]:  # 6 месяцев
        return 180
    else:
        logging.error(f"Неверная сумма: {amount}")
        raise ValueError(f"Неверная сумма: {amount}")


def restart_torrserver():
    """
    Перезапускает TorrServer.
    """
    try:
        os.system("systemctl restart torrserver.service")
        logger.info("TorrServer успешно перезапущен.")
    except Exception as e:
        logger.error(f"Ошибка при перезапуске TorrServer: {e}")


@dp.message_handler(commands=["delete_subscription"])
async def delete_subscription_command(message: types.Message):
    """
    Отправляет список пользователей с активными подписками для удаления.
    Доступно только администратору.
    """
    if message.from_user.id != ADMIN_ID:
        await message.reply("У вас нет прав для выполнения этой команды.")
        return

    expiry = load_json(EXPIRY_DB_PATH)
    accs = load_json(ACCS_DB_PATH)

    if not expiry:
        await message.reply("Нет пользователей с активными подписками.")
        return

    # Генерируем список пользователей с кнопками для удаления
    keyboard = InlineKeyboardMarkup(row_width=1)
    for username, expiry_date in expiry.items():
        keyboard.add(InlineKeyboardButton(f"{username} (до {expiry_date})", callback_data=f"delete_{username}"))

    await message.reply(
        "Выберите пользователя для удаления подписки:",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith("delete_"))
async def delete_subscription_callback(callback_query: types.CallbackQuery):
    """
    Обработка удаления подписки.
    """
    username = callback_query.data.split("_")[1]  # Извлекаем логин пользователя
    expiry = load_json(EXPIRY_DB_PATH)
    accs = load_json(ACCS_DB_PATH)

    # Получаем ID пользователя из имени (User<ID>)
    try:
        user_id = int(username.replace("User", ""))
    except ValueError:
        await callback_query.answer("Ошибка в имени пользователя.", show_alert=True)
        return

    # Проверяем, существует ли пользователь в базе
    if username not in expiry:
        await callback_query.message.edit_text(
            f"Пользователь {username} уже удалён или не существует.",
            reply_markup=None
        )
        return

    # Удаляем пользователя из баз
    del expiry[username]
    if username in accs:
        del accs[username]

    save_json(EXPIRY_DB_PATH, expiry)
    save_json(ACCS_DB_PATH, accs)

    # Уведомляем пользователя
    try:
        await bot.send_message(
            user_id,
            "Ваша подписка была удалена администратором. Обратитесь в поддержку, если у вас есть вопросы.",
            reply_markup=support_chat_button()
        )
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение пользователю {username}: {e}")

    # Обновляем сообщение для администратора
    await callback_query.message.edit_text(
        f"Подписка пользователя {username} успешно удалена.",
        reply_markup=None
    )
    await callback_query.answer("Подписка удалена.")
    

@dp.callback_query_handler(lambda c: c.data == "trial")
async def trial_button_callback(callback_query: types.CallbackQuery):
    """
    Обработка нажатия кнопки "Пробный период".
    """
    user_id = callback_query.from_user.id
    username = f"User{user_id}"

    # Проверка активной подписки
    expiry = load_json(EXPIRY_DB_PATH)
    if username in expiry:
        expiry_date = parse_expiry_date(expiry[username])
        if expiry_date > datetime.now():
            await callback_query.message.edit_text(
                "У вас уже есть активная подписка. Пробный период недоступен.\n\n"
                "Продлите подписку через главное меню.",
                reply_markup=back_to_main_menu()
            )
            return

    # Проверка, использовался ли пробный период
    trial_users = load_trial_usage()
    if user_id in trial_users:
        await callback_query.message.edit_text(
            "Вы уже использовали пробный период.\n\n"
            "Если вы хотите продолжить пользоваться сервисом, оформите подписку через главное меню.",
            reply_markup=back_to_main_menu()
        )
        return

    # Активируем пробный период
    password = generate_password()
    trial_end_time = datetime.now() + timedelta(hours=8)

    accs = load_json(ACCS_DB_PATH)
    accs[username] = password
    expiry[username] = trial_end_time.strftime("%Y-%m-%d %H:%M:%S")

    save_json(ACCS_DB_PATH, accs)
    save_json(EXPIRY_DB_PATH, expiry)

    # Сохраняем пользователя как использовавшего пробный период
    trial_users.append(user_id)
    save_trial_usage(trial_users)

    # Перезагружаем TorrServer
    restart_torrserver()

    # Уведомляем пользователя
    await callback_query.message.edit_text(
        f"Ваш пробный период активирован на 8 часов.\n\n"
        f"*Ваши данные для подключения:*\n"
        f"*Адрес:* {TORR_SERVER_ADDRESS}\n"
        f"*Логин:* {username}\n"
        f"*Пароль:* {password}\n"
        f"*Срок действия:* {trial_end_time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "Спасибо, что выбрали наш сервис!",
        reply_markup=back_to_main_menu(),
        parse_mode="Markdown"
    )

    # Уведомляем администратора
    await bot.send_message(
        ADMIN_ID,
        f"Пользователь @{callback_query.from_user.username or 'Без имени'} (ID: {user_id}) активировал пробный период.\n\n"
        f"*Логин:* {username}\n"
        f"*Срок действия:* {trial_end_time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"Пароль: {password}",
        parse_mode="Markdown"
    )

    # Устанавливаем задачу на удаление подписки
    scheduler.add_job(
        delete_trial_account,
        trigger=DateTrigger(run_date=trial_end_time),
        args=[username],
        id=f"trial_{username}"
    )
    await callback_query.answer()



def delete_trial_account(username):
    """
    Удаляет пробный аккаунт после истечения времени.
    """
    accs = load_json(ACCS_DB_PATH)
    expiry = load_json(EXPIRY_DB_PATH)

    if username in accs:
        del accs[username]
    if username in expiry:
        del expiry[username]

    save_json(ACCS_DB_PATH, accs)
    save_json(EXPIRY_DB_PATH, expiry)

    logging.info(f"Пробный аккаунт {username} был удалён.")


@dp.message_handler(commands=["start"])
async def start_command(message: types.Message):
    """
    Приветствие пользователя с кнопками под сообщением.
    """
    await message.reply(
        "Привет! Я бот для подписок TorrServer. Выберите нужное действие из меню ниже:",
        reply_markup=inline_main_menu()
    )

@dp.callback_query_handler(lambda c: c.data.startswith("topup_reject_"))
async def topup_reject_callback(callback_query: types.CallbackQuery):
    """
    Отклонение пополнения баланса администратором.
    """
    user_id = int(callback_query.data.split("_")[2])

    # Уведомляем пользователя
    try:
        await bot.send_message(
            user_id,
            "Ваш запрос на пополнение баланса был отклонён администратором.\n"
            "Пожалуйста, проверьте данные перевода и попробуйте снова."
        )
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

    # Уведомляем администратора
    await callback_query.message.edit_text(
        f"Пополнение баланса пользователя с ID {user_id} отклонено.",
        reply_markup=None
    )
    await callback_query.answer("Пополнение отклонено.")


@dp.callback_query_handler(lambda c: c.data == "main_menu")
async def main_menu_button_callback(callback_query: types.CallbackQuery):
    """
    Обработка нажатия кнопки "Главное меню".
    """
    await callback_query.message.edit_text(
        "Выберите нужное действие:",
        reply_markup=inline_main_menu()
    )
    await callback_query.answer()


@dp.callback_query_handler(lambda c: c.data == "get_account")
async def get_account_button_callback(callback_query: types.CallbackQuery):
    """
    Получение данных учётной записи.
    """
    user_id = callback_query.from_user.id
    username = f"User{user_id}"
    accs = load_json(ACCS_DB_PATH)
    expiry = load_json(EXPIRY_DB_PATH)

    if username in accs and username in expiry:
        try:
            expiry_date = parse_expiry_date(expiry[username])  # Универсальная обработка времени
        except ValueError as e:
            await callback_query.message.edit_text(
                f"Ошибка в данных учётной записи: {e}. Пожалуйста, свяжитесь с поддержкой.",
                reply_markup=support_chat_button()
            )
            return

        if expiry_date > datetime.now():
            is_trial = check_if_trial(user_id)  # Проверяем, активирована ли подписка как пробная
            message = (
                f"*Ваши данные для подключения к TorrServer:*\n\n"
                f"*Адрес:* {TORR_SERVER_ADDRESS}\n"
                f"*Логин:* {username}\n"
                f"*Пароль:* {accs[username]}\n"
                f"*Срок действия подписки:* {expiry_date.strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            if is_trial:
                message += "\n*Тип подписки:* Пробный период (8 часов)\n"
            else:
                message += "\n*Тип подписки:* Обычная\n"

            await callback_query.message.edit_text(
                message,
                reply_markup=back_to_main_menu(),
                parse_mode="Markdown"
            )
            return

    await callback_query.message.edit_text(
        "У вас нет активной подписки. Оформите подписку через главное меню.",
        reply_markup=back_to_main_menu()
    )
    await callback_query.answer()



@dp.message_handler(lambda message: message.text == "🔑 Получить данные учётной записи")
async def get_account_command(message: types.Message):
    """
    Отправляет данные учётной записи пользователя, если подписка активна.
    """
    user_id = message.from_user.id
    username = f"User{user_id}"
    accs = load_json(ACCS_DB_PATH)
    expiry = load_json(EXPIRY_DB_PATH)

    if username in accs and username in expiry:
        expiry_date = datetime.strptime(expiry[username], "%Y-%m-%d")
        if expiry_date > datetime.now():
            await message.reply(
                f"Ваши данные для подключения к TorrServer:\n\n"
                f"**Адрес:** {TORR_SERVER_ADDRESS}\n"
                f"**Логин:** {username}\n"
                f"**Пароль:** {accs[username]}\n"
                f"**Срок действия подписки:** {expiry_date.strftime('%Y-%m-%d')}\n\n"
                f"Спасибо, что пользуетесь нашим сервисом!",
                parse_mode="Markdown"
            )
        else:
            await message.reply("⚠️ Ваша подписка истекла. Продлите её через меню.")
    else:
        await message.reply("У вас нет активной подписки. Оформите подписку через меню.")



@dp.message_handler(commands=["admin_create"])
async def admin_create_account(message: types.Message):
    """
    Создаёт учётную запись вручную (только для администратора).
    """
    if message.from_user.id != ADMIN_ID:
        await message.reply("У вас нет прав для выполнения этой команды.")
        return

    # Парсинг команды
    try:
        args = message.text.split()
        if len(args) < 2:
            raise ValueError("Недостаточно параметров.")

        username = args[1]
        password = args[2] if len(args) > 2 else generate_password()
        days = int(args[3]) if len(args) > 3 else 30

        accs = load_json(ACCS_DB_PATH)
        expiry = load_json(EXPIRY_DB_PATH)

        # Проверка, существует ли уже такой логин
        if username in accs:
            await message.reply(f"Учётная запись с логином `{username}` уже существует.")
            return

        # Добавление новой учётной записи
        accs[username] = password
        expiry_date = datetime.now() + timedelta(days=days)
        expiry[username] = expiry_date.strftime("%Y-%m-%d")

        save_json(ACCS_DB_PATH, accs)
        save_json(EXPIRY_DB_PATH, expiry)

        # Перезапуск TorrServer
        restart_torrserver()

        # Уведомление
        await message.reply(
            f"Учётная запись создана:\n"
            f"*Логин:* {username}\n"
            f"*Пароль:* {password}\n"
            f"*Срок действия:* {expiry_date.strftime('%Y-%m-%d')}\n"
            f"TorrServer перезапущен."
        )
    except ValueError:
        await message.reply("Использование команды:\n`/admin_create логин [пароль] [дней подписки]`", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка при создании учётной записи: {e}")
        await message.reply("Произошла ошибка при создании учётной записи.")

@dp.callback_query_handler(lambda c: c.data == "pay")
async def pay_button_callback(callback_query: types.CallbackQuery):
    """
    Обработка нажатия кнопки "Оплатить подписку".
    """
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("Оплата через СБП Озон Банк", callback_data="pay_sbp"),
        InlineKeyboardButton("Оплата через Telegram-кошелёк", callback_data="pay_tg_wallet"),
        InlineKeyboardButton("🔙 Главное меню", callback_data="main_menu")
    )

    await callback_query.message.edit_text(
        "Выберите способ оплаты подписки:",
        reply_markup=keyboard
    )
    await callback_query.answer()

# СБП ОЗОН БАНК -----------------------------------------------------------------------------------------------------

@dp.callback_query_handler(lambda c: c.data == "pay_sbp")
async def pay_sbp_callback(callback_query: types.CallbackQuery):
    """
    Выбор тарифа для оплаты через СБП Озон Банк.
    """
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("1 месяц - 100 руб", callback_data="topup_sbp_amount_100"),
        InlineKeyboardButton("3 месяца - 300 руб", callback_data="topup_sbp_amount_300"),
        InlineKeyboardButton("6 месяцев - 600 руб", callback_data="topup_sbp_amount_600"),
        InlineKeyboardButton("🔙 Назад", callback_data="pay")
    )

    await callback_query.message.edit_text(
        f"Выберите тариф для оплаты через СБП Озон Банк. Переводите средства на номер:\n\n"
        f"💳 {SBP_PHONE}\n\n"
        "После перевода обязательно укажите уникальный идентификатор, который будет предоставлен на следующем шаге.",
        reply_markup=keyboard
    )
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("topup_sbp_amount_"))
async def handle_sbp_topup(callback_query: types.CallbackQuery):
    """
    Обработка выбора тарифа для оплаты через СБП Озон Банк.
    """
    amount = int(callback_query.data.split("_")[-1])
    unique_id = str(uuid.uuid4())[:8]
    user_id = callback_query.from_user.id  # Получаем ID пользователя

    await callback_query.message.edit_text(
        f"Вы выбрали тариф на сумму *{amount} руб.*\n\n"
        f"Переведите эту сумму через СБП Озон Банк на номер:\n"
        f"💳 {SBP_PHONE}\n\n"
        f"‼️ Укажите уникальный идентификатор в комментарии: `{unique_id}`\n\n"
        f"После перевода нажмите кнопку 'Оплатил'.",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("Оплатил", callback_data=f"topup_sbp_paid_{amount}_{unique_id}")

        ),
        parse_mode="Markdown"
    )
    logging.info(f"Callback data: {callback_query.data}")
    await callback_query.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("topup_sbp_paid_"))
async def topup_sbp_paid_callback(callback_query: types.CallbackQuery):
    """
    Обработка нажатия кнопки "Оплатил" для оплаты через СБП.
    """
    try:
        # Разбираем callback_data
        data = callback_query.data.split("_")
        amount = int(data[3])  # Сумма перевода
        unique_id = data[4]    # Уникальный идентификатор

        user_id = callback_query.from_user.id
        username = callback_query.from_user.username or "Без имени"

        # Уведомляем администратора
        keyboard_admin = InlineKeyboardMarkup(row_width=1)
        keyboard_admin.add(
            InlineKeyboardButton(
                "Подтвердить", callback_data=f"topup_confirm_sbp_{user_id}_{amount}_{unique_id}"
            ),
            InlineKeyboardButton(
                "Отклонить", callback_data=f"topup_reject_sbp_{user_id}"
            )
        )

        await bot.send_message(
            ADMIN_ID,
            f"Пользователь @{username} (ID: {user_id}) сообщил о переводе через СБП.\n\n"
            f"Сумма: *{amount} руб.*\n"
            f"Уникальный идентификатор: `{unique_id}`",
            reply_markup=keyboard_admin,
            parse_mode="Markdown"
        )

        # Уведомляем пользователя
        await callback_query.message.edit_text(
            "Ваш запрос на оплату подписки отправлен на проверку.\nОжидайте подтверждения от администратора.",
            reply_markup=back_to_main_menu()
        )
        await callback_query.answer("Запрос отправлен администратору.")
    except Exception as e:
        logging.error(f"Ошибка при обработке оплаты через СБП: {e}")
        await callback_query.answer("Произошла ошибка. Пожалуйста, повторите попытку.", show_alert=True)

@dp.callback_query_handler(lambda c: c.data.startswith("topup_confirm_sbp_"))
async def topup_confirm_sbp_callback(callback_query: types.CallbackQuery):
    """
    Обработка подтверждения оплаты через СБП администратором.
    """
    try:
        # Разбираем данные из callback_data
        data = callback_query.data.split("_")
        user_id = int(data[3])  # ID пользователя
        amount = int(data[4])  # Сумма
        unique_id = data[5]    # Уникальный идентификатор

        # Вычисляем дни подписки на основе суммы
        days = calculate_subscription_days(amount)

        # Создаём или продлеваем учётную запись
        username, password, expiry_date = create_or_extend_torr_account(user_id, additional_days=days)

        # Уведомляем пользователя
        await bot.send_message(
            user_id,
            f"Ваш платёж на сумму *{amount} руб.* через СБП успешно подтверждён.\n\n"
            f"*Ваши данные для подключения к TorrServer:*\n"
            f"🌐 *Адрес:* {TORR_SERVER_ADDRESS}\n"
            f"🔑 *Логин:* `{username}`\n"
            f"🔑 *Пароль:* `{password}`\n"
            f"📅 *Срок действия:* {expiry_date}\n\n"
            "Спасибо за использование нашего сервиса!",
            parse_mode="Markdown"
        )

        # Уведомляем администратора
        await callback_query.message.edit_text(
            f"Оплата через СБП пользователя с ID {user_id} подтверждена.\n\n"
            f"Сумма: {amount} руб.\n"
            f"Логин: `{username}`\n"
            f"Пароль: `{password}`\n"
            f"Срок действия: {expiry_date}",
            parse_mode="Markdown"
        )
        await callback_query.answer("Подтверждение выполнено.")
    except Exception as e:
        logging.error(f"Ошибка при подтверждении оплаты через СБП: {e}")
        await callback_query.answer("Произошла ошибка. Проверьте логи.", show_alert=True)

# TELEGRAM ------------------------------------------------------------------------------------------------------------------------------

@dp.callback_query_handler(lambda c: c.data == "pay_tg_wallet")
async def pay_tg_wallet_callback(callback_query: types.CallbackQuery):
    """
    Обработчик выбора оплаты через Telegram-кошелёк.
    """
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("1 месяц - 1 USDT", callback_data="topup_tg_wallet_amount_1"),
        InlineKeyboardButton("3 месяца - 3 USDT", callback_data="topup_tg_wallet_amount_3"),
        InlineKeyboardButton("6 месяцев - 6 USDT", callback_data="topup_tg_wallet_amount_6"),
        InlineKeyboardButton("🔙 Назад", callback_data="pay")
    )

    await callback_query.message.edit_text(
        "Выберите тариф для оплаты через Telegram-кошелёк. Переводите средства на:\n\n"
        f"💳 {ADMIN_WALLET}\n\n"
        "После перевода обязательно укажите уникальный идентификатор, который будет предоставлен на следующем шаге.",
        reply_markup=keyboard
    )
    await callback_query.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("topup_tg_wallet_amount_"))
async def handle_tg_wallet_topup(callback_query: types.CallbackQuery):
    """
    Обработка выбора тарифа для оплаты через Telegram-кошелёк.
    """
    try:
        # Извлекаем сумму из callback_data
        amount = int(callback_query.data.split("_")[-1])  # Получаем сумму (1, 3, 6)
        unique_id = str(uuid.uuid4())[:8]  # Генерация уникального идентификатора

        logging.info(f"Выбрана сумма: {amount} USDT")

        # Формируем сообщение с кнопкой "Оплатил"
        await callback_query.message.edit_text(
            f"Вы выбрали тариф на сумму *{amount} USDT* для подписки.\n\n"
            f"Переведите эту сумму через Telegram-кошелёк на:\n"
            f"💳 {ADMIN_WALLET}\n\n"
            f"‼️ Укажите уникальный идентификатор в комментарии: `{unique_id}`\n\n"
            f"После перевода нажмите кнопку 'Оплатил'.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("Оплатил", callback_data=f"topup_tg_wallet_paid_{unique_id}_{amount}")
            ),
            parse_mode="Markdown"
        )
        await callback_query.answer()
    except Exception as e:
        logging.error(f"Ошибка при обработке выбора тарифа через Telegram-кошелёк: {e}")
        await callback_query.answer("Произошла ошибка. Попробуйте ещё раз.", show_alert=True)


@dp.callback_query_handler(lambda c: c.data.startswith("topup_tg_wallet_paid_"))
async def topup_tg_wallet_paid_callback(callback_query: types.CallbackQuery):
    """
    Обработка нажатия кнопки "Оплатил" для оплаты через Telegram-кошелёк.
    """
    try:
        # Разбираем данные из callback_data
        data = callback_query.data.split("_")
        unique_id = data[4]  # Уникальный идентификатор
        amount = int(data[5])  # Сумма перевода

        user_id = callback_query.from_user.id
        username = callback_query.from_user.username or "Без имени"

        # Уведомляем администратора
        keyboard_admin = InlineKeyboardMarkup(row_width=1)
        keyboard_admin.add(
            InlineKeyboardButton(
                "Подтвердить", callback_data=f"topup_confirm_tg_wallet_{user_id}_{amount}_{unique_id}"
            ),
            InlineKeyboardButton(
                "Отклонить", callback_data=f"topup_reject_tg_wallet_{user_id}"
            )
        )

        logging.info(f"Отправка уведомления админу о платеже Telegram-кошельком: user_id={user_id}, amount={amount}, unique_id={unique_id}")

        await bot.send_message(
            ADMIN_ID,
            f"Пользователь @{username} (ID: {user_id}) сообщил о переводе через Telegram-кошелёк.\n\n"
            f"Сумма: *{amount} USDT*\n"
            f"Уникальный идентификатор: `{unique_id}`",
            reply_markup=keyboard_admin,
            parse_mode="Markdown"
        )

        # Уведомляем пользователя
        await callback_query.message.edit_text(
            "Ваш запрос на оплату подписки отправлен на проверку.\nОжидайте подтверждения от администратора.",
            reply_markup=back_to_main_menu()
        )

        await callback_query.answer("Запрос отправлен администратору.")
    except Exception as e:
        logging.error(f"Ошибка в обработчике topup_tg_wallet_paid_callback: {e}")
        await callback_query.answer("Произошла ошибка. Проверьте логи.", show_alert=True)


@dp.callback_query_handler(lambda c: c.data.startswith("topup_confirm_tg_wallet_"))
async def topup_confirm_tg_wallet_callback(callback_query: types.CallbackQuery):
    logging.info(f"Обработчик вызван с callback_data: {callback_query.data}")
    try:
        # Разбираем callback_data
        data = callback_query.data.split("_")
        user_id = int(data[4])  # ID пользователя
        amount = int(data[5])  # Сумма перевода
        unique_id = data[6]    # Уникальный идентификатор

        logging.info(f"Разобранные данные: user_id={user_id}, amount={amount}, unique_id={unique_id}")

        # Логика подтверждения
        days = calculate_subscription_days(amount)
        username, password, expiry_date = create_or_extend_torr_account(user_id, additional_days=days)

        # Отправляем данные пользователю
        await bot.send_message(
            user_id,
            f"Ваш платёж на сумму *{amount} USDT* через Telegram-кошелёк успешно подтверждён.\n\n"
            f"*Ваши данные для подключения к TorrServer:*\n"
            f"🌐 *Адрес:* {TORR_SERVER_ADDRESS}\n"
            f"🔑 *Логин:* `{username}`\n"
            f"🔑 *Пароль:* `{password}`\n"
            f"📅 *Срок действия:* {expiry_date}\n\n"
            "Спасибо за использование нашего сервиса!",
            parse_mode="Markdown"
        )

        # Уведомляем администратора
        await callback_query.message.edit_text(
            f"Оплата через Telegram-кошелёк пользователя с ID {user_id} подтверждена.\n\n"
            f"Сумма: {amount} USDT.\n"
            f"Логин: `{username}`\n"
            f"Пароль: `{password}`\n"
            f"Срок действия: {expiry_date}",
            parse_mode="Markdown"
        )
        await callback_query.answer("Подтверждение выполнено.")
    except Exception as e:
        logging.error(f"Ошибка в обработчике topup_confirm_tg_wallet_callback: {e}")
        await callback_query.answer("Произошла ошибка. Проверьте логи.", show_alert=True)


# -------------------------------------------------------------------------------------------------------------

@dp.callback_query_handler(lambda c: c.data == "status")
async def status_button_callback(callback_query: types.CallbackQuery):
    """
    Проверка статуса подписки.
    """
    user_id = callback_query.from_user.id
    username = f"User{user_id}"
    expiry = load_json(EXPIRY_DB_PATH)

    if username in expiry:
        try:
            expiry_date = parse_expiry_date(expiry[username])  # Универсальная обработка времени
        except ValueError as e:
            await callback_query.message.edit_text(
                f"Ошибка в данных подписки: {e}. Пожалуйста, свяжитесь с поддержкой.",
                reply_markup=support_chat_button()
            )
            return

        if expiry_date > datetime.now():
            is_trial = check_if_trial(user_id)  # Проверяем, активирована ли подписка как пробная
            message = (
                f"Ваш статус подписки:\n\n"
                f"*Логин:* {username}\n"
                f"*Срок действия подписки:* {expiry_date.strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            if is_trial:
                message += "\n*Тип подписки:* Пробный период (8 часов)\n"
            else:
                message += "\n*Тип подписки:* Обычная\n"

            await callback_query.message.edit_text(
                message,
                reply_markup=back_to_main_menu(),
                parse_mode="Markdown"
            )
            return

    await callback_query.message.edit_text(
        "У вас нет активной подписки. Оформите подписку через главное меню.",
        reply_markup=back_to_main_menu()
    )
    await callback_query.answ



@dp.message_handler(lambda message: message.text == "📅 Проверить статус подписки")
async def status_command(message: types.Message):
    """
    Проверка статуса подписки.
    """
    user_id = message.from_user.id
    expiry = load_json(EXPIRY_DB_PATH)
    username = f"User{user_id}"

    expiry_date = expiry.get(username)
    if expiry_date:
        expiry_date = datetime.strptime(expiry_date, "%Y-%m-%d")
        if expiry_date > datetime.now():
            await message.reply(
                f"Ваш статус подписки:\n\n"
                f"**Логин:** {username}\n"
                f"**Срок действия подписки:** {expiry_date.strftime('%Y-%m-%d')}\n\n"
                f"Спасибо, что пользуетесь нашим сервисом!",
                parse_mode="Markdown"
            )
        else:
            await message.reply("⚠️ Ваша подписка истекла. Продлите её через меню.")
    else:
        await message.reply("У вас нет активной подписки. Оформите подписку через меню.")


@dp.callback_query_handler(lambda c: c.data.startswith("reject_"))
async def reject_callback(callback_query: types.CallbackQuery):
    """
    Отклонение администратора.
    """
    user_id = int(callback_query.data.split("_")[1])

    # Уведомляем пользователя
    await bot.send_message(
        user_id,
        "Ваш платёж был отклонён. Проверьте данные и попробуйте снова."
    )

    # Обновляем сообщение для администратора
    await callback_query.message.edit_text(
        f"Платёж от пользователя с ID {user_id} был отклонён."
    )


# ====== Основной запуск ======
if __name__ == "__main__":
    from aiogram import executor
    executor.start_polling(dp, skip_updates=True)
