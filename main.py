import os
import logging
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ConversationHandler
from bot_commands import (start, rules, add_expense, my_debt, report, send_money, 
                          help_command, button_callback, photo_handler, handle_pending_state,
                          expense_conversation_handler, rules_conversation_handler, 
                          send_conversation_handler, handle_new_member, reset_group,
                          handle_my_chat_member)
from db_manager import init_db

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def main():
    """Запуск бота."""
    # Инициализация базы данных
    init_db()
    
    # Получение токена из переменной окружения
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("Токен не предоставлен. Установите переменную окружения TELEGRAM_BOT_TOKEN.")
        return

    # Создание экземпляра приложения
    application = Application.builder().token(token).build()

    # Добавление обработчиков диалогов
    application.add_handler(expense_conversation_handler)
    application.add_handler(rules_conversation_handler)
    application.add_handler(send_conversation_handler)
    
    # Добавление обработчиков команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("mydebt", my_debt))
    application.add_handler(CommandHandler("report", report))
    application.add_handler(CommandHandler("reset", reset_group))
    
    # Добавление обработчиков для различных типов кнопок
    # Обработчики транзакций
    application.add_handler(CallbackQueryHandler(button_callback, pattern=r"^(confirm|reject)_transaction_\d+$"))
    
    # Обработчики выбора участников
    application.add_handler(CallbackQueryHandler(button_callback, pattern=r"^participant_\d+$"))
    application.add_handler(CallbackQueryHandler(button_callback, pattern=r"^participants_all$"))
    application.add_handler(CallbackQueryHandler(button_callback, pattern=r"^participants_done$"))
    
    # Обработчики фото чеков
    application.add_handler(CallbackQueryHandler(button_callback, pattern=r"^expense_photo_(yes|no)$"))
    
    # Обработчики перевода денег
    application.add_handler(CallbackQueryHandler(button_callback, pattern=r"^send_(to_\d+|confirm|cancel)$"))
    
    # Обработчики меню помощи
    application.add_handler(CallbackQueryHandler(button_callback, pattern=r"^help_\w+$"))
    
    # Обработчики настройки правил
    application.add_handler(CallbackQueryHandler(button_callback, pattern=r"^setup_rules_(yes|no)$"))
    
    # Обработчики сброса данных группы
    application.add_handler(CallbackQueryHandler(button_callback, pattern=r"^reset_(confirm|cancel)$"))
    
    # Обработчики административного меню
    application.add_handler(CallbackQueryHandler(button_callback, pattern=r"^admin_(edit_expenses|delete_expenses|delete_transactions|reset|back)$"))
    application.add_handler(CallbackQueryHandler(button_callback, pattern=r"^edit_expense_\d+$"))
    application.add_handler(CallbackQueryHandler(button_callback, pattern=r"^delete_expense_\d+$"))
    application.add_handler(CallbackQueryHandler(button_callback, pattern=r"^delete_transaction_\d+$"))
    
    # Обработчики подтверждения удаления расходов и транзакций
    application.add_handler(CallbackQueryHandler(button_callback, pattern=r"^confirm_delete_(expense|transaction)_\d+$"))
    
    # Обработчик для продолжения диалога после нажатия на кнопки в меню help
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & ~filters.REPLY,
        handle_pending_state
    ))
    
    # Обработчик для фото вне диалогов
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.REPLY, photo_handler))
    
    # Обработчик для новых участников группы
    application.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, 
        handle_new_member
    ))
    
    # TODO: Добавить обработчик для MY_CHAT_MEMBER события
    # Временно отключено из-за проблем с совместимостью версий
    
    # Запуск бота
    application.run_polling()

if __name__ == '__main__':
    main()
