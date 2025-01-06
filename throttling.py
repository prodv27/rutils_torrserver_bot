import asyncio
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.utils.exceptions import Throttled


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate_limit=1.0):
        """
        Middleware для ограничения частоты запросов пользователей.
        :param rate_limit: Время в секундах между запросами.
        """
        super(ThrottlingMiddleware, self).__init__()
        self.rate_limit = rate_limit
        self.rate_limits = {}

    async def on_pre_process_message(self, message, data):
        """
        Проверка перед обработкой сообщения.
        """
        user_id = message.from_user.id
        if user_id in self.rate_limits:
            last_request = self.rate_limits[user_id]
            if asyncio.get_event_loop().time() - last_request < self.rate_limit:
                raise Throttled()
        self.rate_limits[user_id] = asyncio.get_event_loop().time()

    async def on_process_exception(self, update, exception):
        """
        Обработка исключений, возникающих из-за частых запросов.
        """
        if isinstance(exception, Throttled):
            if update.message:
                await update.message.reply("Слишком много запросов. Подождите немного!")
            return True  # Исключение обработано
        return False
