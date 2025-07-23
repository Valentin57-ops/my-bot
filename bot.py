import os
import asyncio
import logging
import re
from datetime import datetime
import pytz
import requests
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Например https://your-app.onrender.com/webhook
PORT = int(os.getenv("PORT", 8443))

HH_API_URL = "https://api.hh.ru/vacancies"
HH_SEARCH_PARAMS = {
    "text": "Оператор контакт-центра",
    "area": 113,
    "per_page": 99,
    "schedule": "remote",
    "experience": "noExperience",
}
VACANCY_LIMIT = 2000

user_daily_vacancies = {}
user_sent_vacancies = {}

def get_vacancies():
    vacancies = []
    page = 0
    msk_tz = pytz.timezone("Europe/Moscow")
    now = datetime.now(msk_tz)
    HH_SEARCH_PARAMS.update({
        "date_from": now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
        "date_to": now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat(),
    })
    while True:
        HH_SEARCH_PARAMS["page"] = page
        logger.info(f"Fetching vacancies, page {page}")
        try:
            r = requests.get(HH_API_URL, params=HH_SEARCH_PARAMS, timeout=30)
            r.raise_for_status()
        except Exception as e:
            logger.error(f"Request error: {e}")
            break
        items = r.json().get("items", [])
        vacancies.extend(items)
        if len(items) < 99 or len(vacancies) >= VACANCY_LIMIT:
            break
        page += 1
    return vacancies[:VACANCY_LIMIT]

def clean_text(text):
    return re.sub(r"<[^>]*>", "", text).strip() if text else ''

def format_vacancy(vac):
    name = vac.get('name', 'Не указано')
    salary = vac.get('salary') or {}
    salary_from = salary.get('from', 'Не указана')
    salary_to = salary.get('to', 'Не указана')
    area = vac.get('area', {}).get('name', 'Не указан')
    employer = vac.get('employer', {}).get('name', 'Не указана')
    schedule = vac.get('schedule', {}).get('name', 'Не указано')
    desc = clean_text(vac.get('snippet', {}).get('responsibility', 'Описание отсутствует.'))
    return (
        f"🔹 *{name}*\n"
        f"💼 Компания: {employer}\n"
        f"💰 Зарплата: от {salary_from} до {salary_to} руб.\n"
        f"📍 Город: {area}\n"
        f"🕒 Формат работы: {schedule}\n"
        f"✍️ Описание:\n{desc}\n"
        f"🔗 [Подробнее]({vac.get('alternate_url', '#' )})"
    )

async def send_message_with_retry(context, chat_id, message):
    while True:
        try:
            await context.bot.send_message(chat_id, message, parse_mode="Markdown")
            break
        except Exception as e:
            err = str(e)
            if "Flood control exceeded" in err:
                retry_after = int(err.split("Retry in")[1].strip().split()[0])
                logger.warning(f"Flood control: wait {retry_after}s")
                await asyncio.sleep(retry_after + 1)
            else:
                logger.error(f"Send message error: {e}")
                break

async def send_vacancies(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    if chat_id not in user_sent_vacancies:
        user_sent_vacancies[chat_id] = set()
        user_daily_vacancies[chat_id] = {}
    try:
        vacancies = get_vacancies()
        unique = [v for v in vacancies if v['id'] not in user_sent_vacancies[chat_id]]
        for v in unique:
            user_sent_vacancies[chat_id].add(v['id'])
            company = v.get('employer', {}).get('name', 'Не указано')
            user_daily_vacancies[chat_id][company] = user_daily_vacancies[chat_id].get(company, 0) + 1
        for i in range(0, len(unique), 5):
            batch = unique[i:i+5]
            msg = "\n\n".join(format_vacancy(v) for v in batch)
            await send_message_with_retry(context, chat_id, msg)
            await asyncio.sleep(2)
    except Exception as e:
        logger.error(f"Error processing vacancies: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    reply_markup = ReplyKeyboardMarkup([["📊 Ежедневная сводка"]], resize_keyboard=True)
    await update.message.reply_text(
        "👋 Бот запущен. Вакансии будут отправляться в режиме онлайн.\n"
        "Нажмите на кнопку ниже, чтобы получить ежедневную сводку.",
        reply_markup=reply_markup
    )
    context.job_queue.run_repeating(send_vacancies, interval=600, first=10, chat_id=chat_id)

async def daily_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in user_daily_vacancies:
        user_daily_vacancies[chat_id] = {}
    summary = "📊 *Ежедневная сводка по компаниям:*\n\n"
    sorted_data = sorted(user_daily_vacancies[chat_id].items(), key=lambda x: x[1], reverse=True)
    if not sorted_data:
        summary += "Сегодня вакансий не было."
    else:
        for company, count in sorted_data:
            summary += f"🏢 {company}: {count} вакансий\n"
    chunk_size = 4000
    for i in range(0, len(summary), chunk_size):
        await context.bot.send_message(chat_id, summary[i:i+chunk_size], parse_mode="Markdown")

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "📊 Ежедневная сводка":
        await daily_summary_command(update, context)

async def on_startup(application: Application):
    await application.bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook set: {WEBHOOK_URL}")

async def on_shutdown(application: Application):
    await application.bot.delete_webhook()
    logger.info("Webhook deleted")

async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_button))

    logger.info(f"Webhook URL: {WEBHOOK_URL}")

    # Вручной вызов старта и остановки
    await on_startup(app)
    try:
        await app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=WEBHOOK_URL,
        )
    finally:
        await on_shutdown(app)

if __name__ == "__main__":
    asyncio.run(main())
