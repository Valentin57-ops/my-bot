import os
import asyncio
import requests
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import logging
import re
import pytz

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Telegram токен
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# HH API параметры
HH_API_URL = "https://api.hh.ru/vacancies"
HH_SEARCH_PARAMS = {
    "text": "Оператор контакт-центра" or "call-центр" or "оператор" or "начинающий специалист" or "чат",
    "area": 113,
    "per_page": 99,
    "schedule": "remote",
    "experience": "noExperience",
}

# Лимит вакансий
VACANCY_LIMIT = 2000

# Словари для хранения данных по каждому пользователю
user_daily_vacancies = {}  # Сводка вакансий по пользователю {chat_id: {company: count}}
user_sent_vacancies = {}  # Отправленные вакансии по пользователю {chat_id: set()}


def get_vacancies():
    """
    Получение вакансий с hh.ru, включая обработку лимита 2000 вакансий.
    """
    vacancies = []
    page = 0

    # Установка даты поиска в московском времени
    msk_tz = pytz.timezone("Europe/Moscow")
    now = datetime.now(msk_tz)
    HH_SEARCH_PARAMS.update({
        "date_from": now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
        "date_to": now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat(),
    })

    while True:
        HH_SEARCH_PARAMS["page"] = page
        logger.info(f"Запрос вакансий с параметрами: {HH_SEARCH_PARAMS}")

        try:
            response = requests.get(HH_API_URL, params=HH_SEARCH_PARAMS, timeout=30)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка запроса: {e}")
            break

        items = response.json().get("items", [])
        vacancies.extend(items)

        logger.info(f"Получено {len(items)} вакансий на странице {page + 1}.")
        if len(items) < 99 or len(vacancies) >= VACANCY_LIMIT:
            logger.info("Достигнут лимит или конец вакансий.")
            break

        page += 1

    return vacancies[:VACANCY_LIMIT]


def clean_text(text):
    """
    Очистка текста от HTML тегов.
    """
    return re.sub(r"<[^>]*>", "", text).strip() if text else ''


def format_vacancy(vacancy):
    """
    Форматирование информации о вакансии в текстовое сообщение.
    """
    name = vacancy.get('name', 'Не указано')
    salary = vacancy.get('salary', {})
    salary_from = salary.get('from', 'Не указана') if salary else 'Не указана'
    salary_to = salary.get('to', 'Не указана') if salary else 'Не указана'
    area = vacancy.get('area', {}).get('name', 'Не указан')
    employer = vacancy.get('employer', {}).get('name', 'Не указана')
    schedule = vacancy.get('schedule', {}).get('name', 'Не указано')

    snippet = vacancy.get('snippet', {})
    description = snippet.get('responsibility', 'Описание отсутствует.')
    description = clean_text(description)

    return f"""🔹 *{name}*
💼 Компания: {employer}
💰 Зарплата: от {salary_from} до {salary_to} руб.
📍 Город: {area}
🕒 Формат работы: {schedule}
✍️ Описание:
{description}
🔗 [Подробнее]({vacancy.get('alternate_url', '#')})"""


async def send_message_with_retry(context, chat_id, message):
    """
    Отправка сообщения с обработкой ошибок flood control.
    """
    while True:
        try:
            await context.bot.send_message(chat_id, message, parse_mode="Markdown")
            break
        except Exception as e:
            error_message = str(e)
            if "Flood control exceeded" in error_message:
                retry_after = int(error_message.split("Retry in")[1].strip().split()[0])
                logger.warning(f"Flood control triggered. Повтор через {retry_after} секунд.")
                await asyncio.sleep(retry_after + 1)  # Ждем перед повторной отправкой
            else:
                logger.error(f"Ошибка отправки сообщения: {e}")
                break


async def send_vacancies(context: ContextTypes.DEFAULT_TYPE):
    """
    Отправка вакансий в Telegram. Учитывает уникальность для каждого пользователя.
    """
    chat_id = context.job.chat_id

    # Инициализация данных для нового пользователя
    if chat_id not in user_sent_vacancies:
        user_sent_vacancies[chat_id] = set()
        user_daily_vacancies[chat_id] = {}

    try:
        vacancies = get_vacancies()

        # Фильтруем только новые вакансии для данного пользователя
        unique_vacancies = [v for v in vacancies if v['id'] not in user_sent_vacancies[chat_id]]

        for vacancy in unique_vacancies:
            user_sent_vacancies[chat_id].add(vacancy['id'])  # Помечаем как отправленные
            company = vacancy.get('employer', {}).get('name', 'Не указано')
            user_daily_vacancies[chat_id][company] = user_daily_vacancies[chat_id].get(company, 0) + 1

        # Отправляем вакансии группами по 5
        for i in range(0, len(unique_vacancies), 5):
            batch = unique_vacancies[i:i + 5]
            message = "\n\n".join(format_vacancy(vacancy) for vacancy in batch)
            await send_message_with_retry(context, chat_id, message)
            await asyncio.sleep(2)  # Задержка для избежания flood control

    except Exception as e:
        logger.error(f"Ошибка обработки вакансий: {e}")


async def daily_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Отправка ежедневной сводки по запросу через кнопку, отсортированной по убыванию количества вакансий.
    """
    chat_id = update.effective_chat.id

    # Инициализация данных для нового пользователя
    if chat_id not in user_daily_vacancies:
        user_daily_vacancies[chat_id] = {}

    summary = "📊 *Ежедневная сводка о вакансиях (по убыванию количества):*\n\n"

    # Сортируем словарь по количеству вакансий
    sorted_vacancies = sorted(user_daily_vacancies[chat_id].items(), key=lambda item: item[1], reverse=True)

    if not sorted_vacancies:
        summary += "Сегодня вакансий не было."
    else:
        for company, count in sorted_vacancies:
            summary += f"🏢 {company}: {count} вакансий\n"

    # Разбиение длинного сообщения на части
    chunk_size = 4000  # Максимальная длина одного сообщения (включая Markdown)
    summary_chunks = [summary[i:i + chunk_size] for i in range(0, len(summary), chunk_size)]

    try:
        for chunk in summary_chunks:
            await context.bot.send_message(chat_id, chunk, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка отправки сводки: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Запуск работы бота.
    """
    chat_id = update.effective_chat.id
    # Создаем клавиатуру с кнопкой
    reply_markup = ReplyKeyboardMarkup([["📊 Ежедневная сводка"]], resize_keyboard=True)

    await update.message.reply_text(
        "👋 Бот запущен. Вакансии будут отправляться в режиме онлайн.\n"
        "Нажмите на кнопку ниже, чтобы получить ежедневную сводку.",
        reply_markup=reply_markup
    )

    # Запуск задач
    context.job_queue.run_repeating(send_vacancies, interval=600, first=10, chat_id=chat_id)


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработка нажатий кнопок на клавиатуре.
    """
    if update.message.text == "📊 Ежедневная сводка":
        await daily_summary_command(update, context)


def main():
    """
    Основной запуск приложения.
    """
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_button))

    application.run_polling()


if __name__ == "__main__":
    main()
