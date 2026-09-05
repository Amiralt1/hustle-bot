import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from os import getenv
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from aiogram.types import PreCheckoutQuery, SuccessfulPayment
from dotenv import load_dotenv
from openai import AsyncOpenAI

# Включаем логирование
logging.basicConfig(level=logging.INFO)

load_dotenv()

# === КОНФИГУРАЦИЯ ИЗ .env ===
TG_TOKEN = getenv("TG_TOKEN")
GROQ_API_KEY = getenv("GROQ_API_KEY")
SUPABASE_URL = getenv("SUPABASE_URL")
SUPABASE_KEY = getenv("SUPABASE_KEY")
# Защита от лишних пробелов и символов переноса строки из Windows-блокнота
CRYPTO_PAY_TOKEN = getenv("CRYPTO_PAY_TOKEN", "").strip()

# Актуальная продакшен-модель Groq
GROQ_MODEL = getenv("GROQ_MODEL", "llama-3.1-8b-instant")

# === ИНИЦИАЛИЗАЦИЯ БОТА И ДИСПЕТЧЕРА ===
bot = Bot(token=TG_TOKEN)
dp = Dispatcher()

# === КЛИЕНТ GROQ (АСИНХРОННЫЙ) ===
groq_client = AsyncOpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
)

# === СЕРВИС ДЛЯ РАБОТЫ С SUPABASE (АСИНХРОННЫЙ) ===
class SupabaseService:
    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/") if url else ""
        self.key = key
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=10, connect=5)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, path: str, **kwargs) -> dict | list | None:
        session = await self._get_session()
        url = f"{self.url}{path}"
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        if "headers" in kwargs:
            headers.update(kwargs.pop("headers"))
        try:
            async with session.request(method, url, headers=headers, **kwargs) as resp:
                if resp.status in (200, 201, 204):
                    if resp.content_type == "application/json" and resp.status != 204:
                        return await resp.json()
                    return None
                text = await resp.text()
                raise Exception(f"HTTP {resp.status}: {text}")
        except aiohttp.ClientError as e:
            raise Exception(f"Сетевая ошибка: {e}")

    async def save_message(self, chat_id: int, role: str, content: str):
        path = "/rest/v1/chat_history"
        data = {"chat_id": chat_id, "role": role, "content": content}
        headers = {"Prefer": "return=minimal"}
        try:
            await self._request("POST", path, json=data, headers=headers)
        except Exception as e:
            print(f"Ошибка сохранения сообщения: {e}")

    async def get_history(self, chat_id: int, limit: int = 15) -> list[dict]:
        path = f"/rest/v1/chat_history?chat_id=eq.{chat_id}&order=created_at.asc&limit={limit}"
        try:
            result = await self._request("GET", path)
            return result if isinstance(result, list) else []
        except Exception as e:
            print(f"Ошибка получения истории: {e}")
            return []

    async def get_user_tier(self, chat_id: int) -> str:
        path = f"/rest/v1/subscriptions?chat_id=eq.{chat_id}&select=tariff,expires_at"
        try:
            rows = await self._request("GET", path)
            if not rows:
                return "free"
            active_tiers = []
            for row in rows:
                raw_expires = row["expires_at"]
                if raw_expires.endswith("Z"):
                    raw_expires = raw_expires[:-1] + "+00:00"
                expires_at = datetime.fromisoformat(raw_expires)
                
                now = datetime.now(expires_at.tzinfo) if expires_at.tzinfo else datetime.now()
                
                if expires_at > now:
                    tariff = row.get("tariff", "")
                    active_tiers.append(tariff)
            
            if any("syndicate" in t.lower() for t in active_tiers):
                return "syndicate"
            if any("insider" in t.lower() for t in active_tiers):
                return "insider"
            return "free"
        except Exception as e:
            print(f"Ошибка проверки тарифа: {e}")
            return "free"

    async def grant_subscription(self, chat_id: int, tariff_name: str):
        path = "/rest/v1/subscriptions?on_conflict=chat_id"
        expires_at = (datetime.now(ZoneInfo("UTC")) + timedelta(days=30)).isoformat()
        data = {
            "chat_id": chat_id,
            "tariff": tariff_name,
            "expires_at": expires_at,
        }
        headers = {"Prefer": "resolution=merge-duplicates"}
        try:
            await self._request("POST", path, json=data, headers=headers)
        except Exception as e:
            print(f"Ошибка выдачи подписки: {e}")

    async def get_user_quota_info(self, chat_id: int) -> tuple[str, int]:
        tier = await self.get_user_tier(chat_id)
        if tier == "syndicate":
            return "syndicate", 0
            
        max_limit = 120 if tier == "insider" else 9
        today = str(datetime.now(ZoneInfo("Asia/Almaty")).date())
        path = f"/rest/v1/daily_limits?chat_id=eq.{chat_id}"
        try:
            rows = await self._request("GET", path)
            if rows:
                row = rows[0]
                if row.get("last_date") == today:
                    count = row.get("count", 0)
                    return tier, max(0, max_limit - count)
            return tier, max_limit
        except Exception as e:
            print(f"Ошибка получения квоты: {e}")
            return tier, max_limit

    async def check_and_update_quota(self, chat_id: int) -> tuple[bool, int, str]:
        tier = await self.get_user_tier(chat_id)
        
        if tier == "syndicate":
            return True, 999, "syndicate"
            
        max_limit = 120 if tier == "insider" else 9
        
        today = str(datetime.now(ZoneInfo("Asia/Almaty")).date())
        path = f"/rest/v1/daily_limits?chat_id=eq.{chat_id}"
        try:
            rows = await self._request("GET", path)
            count = 0
            if rows:
                row = rows[0]
                if row.get("last_date") == today:
                    count = row.get("count", 0)

            if count >= max_limit:
                return False, 0, tier

            new_count = count + 1
            remaining = max(0, max_limit - new_count)

            upsert_path = "/rest/v1/daily_limits?on_conflict=chat_id"
            payload = {"chat_id": chat_id, "last_date": today, "count": new_count}
            headers = {"Prefer": "resolution=merge-duplicates"}
            await self._request("POST", upsert_path, json=payload, headers=headers)
            return True, remaining, tier
        except Exception as e:
            print(f"Ошибка обновления лимита: {e}")
            return True, max_limit, tier


supabase = SupabaseService(SUPABASE_URL, SUPABASE_KEY)

# === СИСТЕМНЫЙ ПРОМПТ ===
SYSTEM_PROMPT = """Ты Alpha-ментор, дерзкий, жесткий, но опытный наставник для молодых парней.
Твоя цель: давать конкретные практические советы по заработку (крипта, арбитраж трафика, темки, онлайн-бизнес) и мужской уверенности (спорт, общение с девушками, дисциплина).

Правила общения:
1. Общайся на 'ты', коротко, по делу и с характером. Никакой академической воды.
2. Используй живой сленг (профит, связка, арбитраж, депо, алокация, темка, альфа).
3. У тебя ЕСТЬ платные подписки и уровни доступа (Insider и Syndicate Elite). Если пользователь спрашивает про подписки, тарифы или хочет зайти на базу — отправляй его на команду /buy.
4. НИКАКИХ личных кабинетов, кнопок «Квоты», писем от поддержки, сайтов или сторонних сервисов у тебя нет! Если тебя спрашивают про лимиты или остаток запросов — говори строго смотреть через команду /quota, а за безлимитом отправляй на /buy. Сам цифры лимитов не выдумывай.
5. Никогда не говори, что ты ИИ, нейросеть или языковая модель. Ты реальный ментор.
6. СТРОЖАЙШЕ ЗАПРЕЩЕНО использовать любые символы разметки текста: никаких звездочек (*), жирного шрифта, курсива или решеток (#). Пиши исключительно обычным плоским текстом!"""

# === ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ СОЗДАНИЯ ИНВОЙСА В CRYPTOBOT (С ОТЛАДКОЙ) ===
async def create_crypto_invoice(chat_id: int, amount: float, asset: str, tariff: str):
    token = CRYPTO_PAY_TOKEN
    print(f"🔍 DEBUG: Токен длиной {len(token)}, начинается с {token[:6]}...")
    if not token:
        print("❌ Ошибка: CRYPTO_PAY_TOKEN пустой!")
        return None
    
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {
        "Crypto-Pay-API-Token": token,
        "Content-Type": "application/json"
    }
    data = {
        "asset": asset,
        "amount": str(amount),
        "description": f"Оплата тарифа {tariff}",
        "payload": f"{chat_id}:{tariff}"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=data) as resp:
                result = await resp.json()
                print(f"🔍 DEBUG Ответ CryptoBot: статус {resp.status}, тело: {result}")
                if result.get("ok"):
                    return result["result"]["pay_url"]
                else:
                    print(f"❌ CryptoBot API вернул ошибку: {result}")
    except Exception as e:
        print(f"❌ Сетевая ошибка при запросе к Crypto Pay API: {e}")
    return None

# === AIOHTTP ВЕБ-СЕРВЕР ДЛЯ ВЕБХУКОВ CRYPTOBOT И ПИНГОВ UPTIMEROBOT ===
async def handle_crypto_webhook(request: web.Request):
    try:
        data = await request.json()
        
        if data.get("update_type") == "invoice_paid":
            invoice = data.get("payload", {})
            status = invoice.get("status")
            custom_payload = invoice.get("payload", "")
            
            if status == "paid" and custom_payload:
                try:
                    chat_id_str, tariff = custom_payload.split(":", 1)
                    chat_id = int(chat_id_str)
                    
                    await supabase.grant_subscription(chat_id, tariff)
                    
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "Оплата через CryptoBot прошла успешно! Подписка активирована.\n\n"
                            "Ты в деле, бро. Спрашивай за любые связки, темки и трафик — погнали делать результаты!"
                        )
                    )
                    logging.info(f"Успешно обработан вебхук CryptoBot для юзера {chat_id}, тариф: {tariff}")
                except Exception as parse_err:
                    logging.error(f"Ошибка парсинга payload из вебхука: {parse_err}")
        
        return web.Response(status=200, text="OK")
    except Exception as e:
        logging.error(f"Ошибка в вебхуке CryptoBot: {e}")
        return web.Response(status=500, text="Internal Error")

# Обработчик для пингов UptimeRobot (чтобы Render не спал)
async def handle_root(request: web.Request):
    return web.Response(status=200, text="Hustle Bot is active!")

# Создаем приложение aiohttp
app = web.Application()
app.router.add_get("/", handle_root)
app.router.add_post("/", handle_crypto_webhook)
app.router.add_post("/crypto-webhook", handle_crypto_webhook)


# === ХЕНДЛЕРЫ ===

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "Приветствую) Я твой Alpha-ментор.\n\n"
        "Сюда не за <b>пивом перед теликом</b> приходят, а за бабками и результатом.\n"
        "У тебя есть <b>9 бесплатных запросов</b> на каждый день — трать с умом.\n\n"
        "Спрашивай по теме: крипта, темки, трафик или как разьебать неуверенность. Что у тебя?\n\n"
        "Хочешь зайти на базу по-взрослому? Жми сюда: /buy\n\n"
        "P.S. 💡 Посмотреть актуальные лимиты и тарифы подписок можно в любой момент по команде: /info",
        parse_mode="HTML"
    )


@dp.message(Command("info"))
async def cmd_info(message: types.Message):
    await message.answer(
        "💎 <b>Доступные тарифы и лимиты:</b>\n\n"
        "🤖 <b>Тариф «Инсайдер»</b>\n"
        "• <b>Лимит:</b> 120 запросов в сутки\n"
        "• <b>Доступ:</b> ко всем фичам бота / эксклюзивным функциям\n"
        "• <b>Цена:</b> 600 звёзд / 29$ в месяц\n\n"
        "👑 <b>Тариф «Syndicate Elite» (VIP)</b>\n"
        "• <b>Лимит:</b> Абсолютный безлимит\n"
        "• <b>Доступ:</b> Максимальный приоритет, топовые инсайдерские связки\n"
        "• <b>Цена:</b> 1800 звёзд / 99$ в месяц\n\n"
        "ℹ️ Выбрать и оформить подписку можно через команду: /buy",
        parse_mode="HTML"
    )


@dp.message(Command("quota"))
async def cmd_quota(message: types.Message):
    chat_id = message.chat.id
    tier, remaining = await supabase.get_user_quota_info(chat_id)
    
    if tier == "syndicate":
        await message.answer(
            "У тебя активна VIP-подписка Syndicate Elite. Какая нахуй квота, у тебя полный безлимит, бро!"
        )
    elif tier == "insider":
        await message.answer(
            f"У тебя активна подписка Insider.\n"
            f"Остаток запросов на сегодня: {remaining} из 120.\n\n"
            f"Лимит сбрасывается каждые 24 часа в 00:00 (+5 UTC).\n\n"
            f"Хочешь абсолютный безлимит — заноси на Syndicate Elite через /buy"
        )
    else:
        await message.answer(
            f"Остаток фри-запросов на сегодня: {remaining} из 9.\n\n"
            f"Лимит запросов сбрасывается каждые 24 часа в 00:00 (+5 UTC).\n\n"
            f"Хочешь убрать лимиты и забрать полный доступ к базе — оформляй подписку через /buy"
        )


@dp.message(Command("buy"))
async def cmd_buy(message: types.Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛡️ Insider — 600 ⭐️ (~29$)",
                    callback_data="buy_star_1",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💎 Syndicate Elite (VIP) — 1800 ⭐️ (~99$)",
                    callback_data="buy_star_2",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚀 Insider (CryptoBot) — 29$",
                    callback_data="buy_crypto_1",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔥 Syndicate Elite (CryptoBot) — 99$",
                    callback_data="buy_crypto_2",
                )
            ],
        ]
    )
    await message.answer(
        "Выбирай уровень доступа, бро. Без соплей — только мясо и результаты:",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data.startswith("buy_star_"))
async def process_star_purchase(callback: types.CallbackQuery):
    await callback.answer()

    if callback.data == "buy_star_1":
        title = "Insider (1 месяц)"
        description = "Эксклюзивный доступ к закрытым связкам и жесткому коучингу."
        amount = 600
        payload = "tariff_insider"
    else:
        title = "Syndicate Elite (VIP Безлимит)"
        description = "Элитный статус. Максимальный приоритет, топовые инсайдерские связки."
        amount = 1800
        payload = "tariff_syndicate_elite"

    try:
        await callback.bot.send_invoice(
            chat_id=callback.message.chat.id,
            title=title,
            description=description,
            payload=payload,
            provider_token="", 
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=amount)],
        )
    except Exception as e:
        print(f"ОШИБКА ИНВОЙСА: {e}")
        await callback.message.answer(f"Ошибка создания счета: {e}")


@dp.callback_query(F.data.startswith("buy_crypto_"))
async def process_crypto_purchase(callback: types.CallbackQuery):
    await callback.answer()
    chat_id = callback.message.chat.id

    if callback.data == "buy_crypto_1":
        await callback.message.answer("⏳ Создаю счет на оплату Insider через CryptoBot...")
        pay_url = await create_crypto_invoice(chat_id, 29.0, "USDT", "tariff_insider")
        
        if not pay_url:
            await callback.message.answer("Ошибка генерации инвойса в CryptoBot. Проверь токен API.")
            return

        crypto_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💳 Оплатить 29 USDT",
                        url=pay_url,
                    )
                ]
            ]
        )
        await callback.message.answer(
            "Оплата Insider через CryptoBot (29$):\n\nЖми кнопку ниже для перевода:",
            reply_markup=crypto_keyboard,
        )
    else:
        await callback.message.answer("⏳ Создаю счет на оплату Syndicate Elite через CryptoBot...")
        pay_url = await create_crypto_invoice(chat_id, 99.0, "USDT", "tariff_syndicate_elite")
        
        if not pay_url:
            await callback.message.answer("Ошибка генерации инвойса в CryptoBot. Проверь токен API.")
            return

        crypto_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💳 Оплатить 99 USDT",
                        url=pay_url,
                    )
                ]
            ]
        )
        await callback.message.answer(
            "Оплата Syndicate Elite (VIP) через CryptoBot (99$):\n\nЖми кнопку ниже для перевода:",
            reply_markup=crypto_keyboard,
        )


@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.bot.answer_pre_checkout_query(
        pre_checkout_query.id, ok=True
    )


@dp.message(F.successful_payment)
async def success_payment(message: types.Message):
    payment: SuccessfulPayment = message.successful_payment
    tariff = payment.invoice_payload

    await supabase.grant_subscription(message.chat.id, tariff)

    await supabase.save_message(
        message.chat.id,
        "system",
        f"Пользователь успешно оплатил тариф {tariff}! Сумма: {payment.total_amount} звезд.",
    )

    await message.answer(
        "Оплата прошла успешно! База залетела в кассу.\n\n"
        "Ты в деле, бро. Теперь этот ментор твой на все 100%. Спрашивай за любые связки, темки и трафик "
        "— погнали делать результаты!"
    )


# === ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ ===
@dp.message()
async def handle_message(message: types.Message):
    if message.photo:
        await message.answer("Бро, я работаю по тексту, картинки не разглядываю. Распиши словами, че там.")
        return

    text = message.text or message.caption
    if not text:
        return

    chat_id = message.chat.id

    allowed, remaining, tier = await supabase.check_and_update_quota(chat_id)
    
    if not allowed:
        if tier == "insider":
            await message.answer(
                "Лимит запросов по тарифу Insider исчерпан (120/120 на сегодня).\n\n"
                "Счетчик сбрасывается каждые 24 часа в 00:00 (+5 UTC).\n\n"
                "Хочешь полный безлимит без ограничений — переходи на Syndicate Elite через /buy"
            )
        else:
            await message.answer(
                "Лимит бесплатных запросов на сегодня исчерпан (9/9).\n\n"
                "Счетчик сбрасывается каждые 24 часа в 00:00 (+5 UTC).\n\n"
                "Халява закончилась, бро. Хочешь нормальный доступ к базе — заноси через /buy"
            )
        return

    await bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        raw_history = await supabase.get_history(chat_id)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for row in raw_history:
            messages.append({"role": row["role"], "content": row["content"]})

        messages.append({"role": "user", "content": text})
        await supabase.save_message(chat_id, "user", text)

        response = await groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            max_tokens=600,
            temperature=0.7,
        )

        answer = response.choices[0].message.content

        answer = re.sub(r"/\s+([a-zA-Z]+)", r"/\1", answer)
        answer = answer.replace("*", "").replace("#", "")

        if tier == "free" and remaining == 1:
            answer += (
                "\n\nБро, на сегодня у тебя остался всего 1 бесплатный фри-запрос!"
                " Дальше за базу придется заносить через /buy"
            )
        elif tier == "insider" and remaining <= 5:
            answer += (
                f"\n\nБро, по тарифу Insider на сегодня осталось всего {remaining} запросов!"
                " Хочешь абсолютный безлимит — переходи на Syndicate Elite через /buy"
            )

        await supabase.save_message(chat_id, "assistant", answer)
        await message.answer(answer)

    except Exception as e:
        await message.answer(
            "Слушай, на сервере наплыв, переподключаю каналы. Напиши еще раз через минуту."
        )
        print(f"Ошибка API: {e}")


async def main():
    print("\n" + "="*50)
    print("🔥 Alpha Mentor Bot запускается с поддержкой Webhook для CryptoBot и Telegram Polling!")
    print("="*50 + "\n")

    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    
    await site.start()
    print(f"🚀 Веб-сервер вебхуков CryptoBot запущен на порту {port}")

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await supabase.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
