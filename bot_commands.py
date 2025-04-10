import logging
import re
import asyncio
import time
from asyncio import Task
from typing import Dict, Optional, Tuple, List, Set
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMemberUpdated
from telegram.ext import (ContextTypes, ConversationHandler, CommandHandler, 
                         MessageHandler, filters, CallbackQueryHandler)
from db_manager import (save_user, save_group, add_user_to_group, get_group_rules, 
                       set_group_rules, get_user, get_pending_transactions, get_group_members,
                       reset_group_data, get_expense_with_debts, update_expense_amount, 
                       delete_expense, get_group_transactions, delete_transaction, get_group_expenses)
from expense_handler import (handle_new_expense, format_debt_message, 
                           handle_money_transfer, confirm_transaction, reject_transaction)
from report_generator import generate_excel_report, generate_pdf_report
from utils import is_admin, extract_username_and_amount

# Configure logging
logger = logging.getLogger(__name__)

# Словарь для хранения таймеров удаления сообщений
message_deletion_tasks: Dict[Tuple[int, int], Task] = {}  # (chat_id, message_id) -> Task
# Словарь для хранения цепочек сообщений (родитель -> дети)
message_chains: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}  # (chat_id, parent_msg_id) -> [(chat_id, child_msg_id), ...]
# Словарь для хранения незавершенных цепочек операций пользователей
user_pending_operations: Dict[int, Dict[str, any]] = {}  # user_id -> {operation_data}
# Время в секундах до удаления сообщения
MESSAGE_DELETE_AFTER = 300  # 5 минут
MESSAGE_REMINDER_AFTER = 240  # 4 минуты (напоминание за минуту до удаления)

async def schedule_message_deletion(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    user_id: Optional[int] = None,
    operation_type: Optional[str] = None,
    extend_if_pending: bool = True
) -> None:
    """
    Планирует удаление сообщения через заданное время.
    
    Args:
        context: Контекст бота
        chat_id: ID чата
        message_id: ID сообщения
        user_id: ID пользователя, с которым связана операция (если есть)
        operation_type: Тип операции (напр. "expense_add", "send_money")
        extend_if_pending: Продлить таймер, если операция не завершена
    """
    message_key = (chat_id, message_id)
    
    # Если для этого сообщения уже запланировано удаление, отменяем старую задачу
    if message_key in message_deletion_tasks and not message_deletion_tasks[message_key].done():
        message_deletion_tasks[message_key].cancel()
    
    # Создаем и запускаем новую задачу для удаления сообщения
    task = asyncio.create_task(
        delayed_message_deletion(
            context, chat_id, message_id, user_id, operation_type, extend_if_pending
        )
    )
    message_deletion_tasks[message_key] = task
    
    # Записываем информацию о задаче
    logger.info(f"Запланировано удаление сообщения {message_id} в чате {chat_id} через {MESSAGE_DELETE_AFTER} секунд")

async def delayed_message_deletion(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    user_id: Optional[int] = None,
    operation_type: Optional[str] = None,
    extend_if_pending: bool = True
) -> None:
    """
    Удаляет сообщение после задержки с проверкой незавершенных операций.
    
    Args:
        context: Контекст бота
        chat_id: ID чата
        message_id: ID сообщения
        user_id: ID пользователя, с которым связана операция
        operation_type: Тип операции
        extend_if_pending: Продлить таймер, если операция не завершена
    """
    message_key = (chat_id, message_id)
    
    try:
        # Отправляем напоминание, если операция не завершена
        if user_id and operation_type and extend_if_pending:
            # Сначала ждем время до напоминания
            await asyncio.sleep(MESSAGE_REMINDER_AFTER)
            
            # Проверяем, не завершена ли операция
            operation_pending = False
            if user_id in user_pending_operations:
                user_ops = user_pending_operations[user_id]
                if user_ops.get("type") == operation_type and not user_ops.get("completed", False):
                    operation_pending = True
                    
                    # Отправляем напоминание
                    try:
                        reminder_text = f"⚠️ Напоминание: у вас есть незавершенная операция. Сообщение будет удалено через 1 минуту, если вы не завершите её."
                        reminder_message = await context.bot.send_message(
                            chat_id=chat_id,
                            text=reminder_text,
                            reply_to_message_id=message_id
                        )
                        
                        # Планируем удаление напоминания
                        reminder_key = (chat_id, reminder_message.message_id)
                        reminder_task = asyncio.create_task(
                            delayed_message_deletion(context, chat_id, reminder_message.message_id)
                        )
                        message_deletion_tasks[reminder_key] = reminder_task
                        
                        # Добавляем напоминание в цепочку сообщений
                        if message_key in message_chains:
                            message_chains[message_key].append((chat_id, reminder_message.message_id))
                        else:
                            message_chains[message_key] = [(chat_id, reminder_message.message_id)]
                    except Exception as e:
                        logger.error(f"Ошибка при отправке напоминания: {e}")
            
            # Ждем оставшееся время
            await asyncio.sleep(MESSAGE_DELETE_AFTER - MESSAGE_REMINDER_AFTER)
            
            # Проверяем еще раз, не завершена ли операция
            if operation_pending and user_id in user_pending_operations:
                user_ops = user_pending_operations[user_id]
                if user_ops.get("type") == operation_type and not user_ops.get("completed", False):
                    # Операция всё еще не завершена - прерываем её и удаляем сообщения
                    try:
                        # Отправляем сообщение о прерывании операции
                        abort_text = f"❌ Операция отменена из-за отсутствия активности. Пожалуйста, начните заново."
                        abort_message = await context.bot.send_message(
                            chat_id=chat_id,
                            text=abort_text
                        )
                        
                        # Планируем удаление этого сообщения
                        abort_key = (chat_id, abort_message.message_id)
                        abort_task = asyncio.create_task(
                            delayed_message_deletion(context, chat_id, abort_message.message_id)
                        )
                        message_deletion_tasks[abort_key] = abort_task
                        
                        # Очищаем данные незавершенной операции
                        user_pending_operations.pop(user_id, None)
                    except Exception as e:
                        logger.error(f"Ошибка при отправке сообщения о прерывании операции: {e}")
        else:
            # Просто ждем стандартное время до удаления
            await asyncio.sleep(MESSAGE_DELETE_AFTER)
        
        # Удаляем все сообщения в цепочке
        if message_key in message_chains:
            for child_chat_id, child_message_id in message_chains[message_key]:
                try:
                    await context.bot.delete_message(
                        chat_id=child_chat_id,
                        message_id=child_message_id
                    )
                    logger.info(f"Удалено дочернее сообщение {child_message_id} в чате {child_chat_id}")
                except Exception as e:
                    logger.error(f"Ошибка при удалении дочернего сообщения {child_message_id} в чате {child_chat_id}: {e}")
            
            # Удаляем запись о цепочке
            message_chains.pop(message_key, None)
        
        # Удаляем само сообщение
        await context.bot.delete_message(
            chat_id=chat_id,
            message_id=message_id
        )
        logger.info(f"Удалено сообщение {message_id} в чате {chat_id}")
        
    except asyncio.CancelledError:
        # Задача была отменена, ничего не делаем
        logger.info(f"Удаление сообщения {message_id} в чате {chat_id} отменено")
        
    except Exception as e:
        logger.error(f"Ошибка при удалении сообщения {message_id} в чате {chat_id}: {e}")
    
    finally:
        # Удаляем задачу из словаря
        message_deletion_tasks.pop(message_key, None)

async def add_message_to_chain(parent_key: Tuple[int, int], child_key: Tuple[int, int]) -> None:
    """
    Добавляет дочернее сообщение в цепочку сообщений.
    
    Args:
        parent_key: Кортеж (chat_id, parent_message_id)
        child_key: Кортеж (chat_id, child_message_id)
    """
    if parent_key in message_chains:
        message_chains[parent_key].append(child_key)
    else:
        message_chains[parent_key] = [child_key]
    
    logger.debug(f"Добавлено сообщение {child_key[1]} в цепочку к сообщению {parent_key[1]}")

async def register_pending_operation(
    user_id: int,
    operation_type: str,
    chat_id: int,
    message_id: int,
    data: Dict = None
) -> None:
    """
    Регистрирует незавершенную операцию пользователя.
    
    Args:
        user_id: ID пользователя
        operation_type: Тип операции (e.g., "expense_add", "send_money")
        chat_id: ID чата
        message_id: ID сообщения
        data: Дополнительные данные операции
    """
    user_pending_operations[user_id] = {
        "type": operation_type,
        "chat_id": chat_id,
        "message_id": message_id,
        "start_time": datetime.now(),
        "completed": False,
        "data": data or {}
    }
    
    logger.info(f"Зарегистрирована операция {operation_type} для пользователя {user_id}")

async def complete_pending_operation(user_id: int) -> None:
    """
    Отмечает операцию пользователя как завершенную.
    
    Args:
        user_id: ID пользователя
    """
    if user_id in user_pending_operations:
        user_pending_operations[user_id]["completed"] = True
        logger.info(f"Операция для пользователя {user_id} отмечена как завершенная")

# Conversation states
(RULES_DESCRIPTION, RULES_DEADLINE, RULES_NOTIFICATIONS,
 EXPENSE_AMOUNT, EXPENSE_DESCRIPTION, EXPENSE_PARTICIPANTS, EXPENSE_PHOTO,
 SEND_AMOUNT, SEND_CONFIRM, USER_INTRO_NAME, USER_INTRO_LASTNAME,
 EDIT_EXPENSE_AMOUNT, EDIT_EXPENSE_CONFIRM) = range(13)

# Обработчик для обработки ожидающих состояний после нажатия инлайн кнопок
async def handle_pending_state(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка текстовых сообщений в контексте ожидающих состояний."""
    user = update.effective_user
    chat = update.effective_chat
    message_text = update.message.text
    
    # Сохраняем информацию о пользователе
    save_user(user.id, user.username, user.first_name, user.last_name)
    
    # Обработка ожидания суммы расхода (после нажатия на кнопку "Добавить расход")
    if context.user_data.get('waiting_for_expense_amount'):
        try:
            amount = float(message_text.replace(',', '.'))
            if amount <= 0:
                await update.message.reply_text(
                    "Сумма должна быть положительным числом. Попробуйте снова:"
                )
                return
            
            # Сохраняем сумму и спрашиваем описание
            context.user_data['expense_amount'] = amount
            context.user_data['waiting_for_expense_amount'] = False
            context.user_data['waiting_for_expense_description'] = True
            
            await update.message.reply_text(
                "Теперь введите описание расхода:"
            )
        except ValueError:
            await update.message.reply_text(
                "Неверный формат суммы. Введите число:"
            )
        return
    
    # Обработка ожидания описания расхода
    elif context.user_data.get('waiting_for_expense_description'):
        # Сохраняем описание
        context.user_data['expense_description'] = message_text
        context.user_data['waiting_for_expense_description'] = False
        
        # Получаем информацию о группе и её участниках
        if chat.type in ['group', 'supergroup']:
            # Получаем всех участников группы кроме ботов
            members = get_group_members(chat.id, exclude_bots=True)
            
            # Если есть участники, предлагаем выбрать среди них
            if members and len(members) > 0:
                # Создаем кнопки для каждого участника
                keyboard = []
                row = []
                
                # Фильтруем, исключая ID бота
                bot_user_id = context.bot.id
                filtered_members = [m for m in members if m['user_id'] != bot_user_id]
                
                for i, member in enumerate(filtered_members):
                    # Используем имя и фамилию для отображения
                    first_name = member.get('first_name', '')
                    last_name = member.get('last_name', '')
                    full_name = f"{first_name} {last_name}".strip()
                    
                    # Если нет имени, используем никнейм
                    display_name = full_name if full_name else member.get('username', 'Без имени')
                    
                    # Создаем кнопку для участника
                    user_id = member['user_id']
                    callback_data = f"participant_{user_id}"
                    button = InlineKeyboardButton(display_name, callback_data=callback_data)
                    
                    # Добавляем максимум 2 кнопки в строку
                    row.append(button)
                    if len(row) == 2 or i == len(filtered_members) - 1:
                        keyboard.append(row)
                        row = []
                
                # Добавляем кнопки "Выбрать всех" и "Готово"
                keyboard.append([
                    InlineKeyboardButton("Выбрать всех", callback_data="participants_all"),
                    InlineKeyboardButton("Готово", callback_data="participants_done")
                ])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                # Сохраняем список участников и инициализируем выбранных (без бота)
                context.user_data['all_participants'] = [m['user_id'] for m in filtered_members]
                context.user_data['selected_participants'] = []
                
                await update.message.reply_text(
                    "Выберите участников для разделения расхода:",
                    reply_markup=reply_markup
                )
                return
        
        # Если не группа или нет участников, спрашиваем о фото
        keyboard = [
            [
                InlineKeyboardButton("Да", callback_data="expense_photo_yes"),
                InlineKeyboardButton("Нет", callback_data="expense_photo_no"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "Хотите прикрепить фото чека?",
            reply_markup=reply_markup
        )
        return
    
    # Обработка ожидания имени пользователя для отправки денег
    elif context.user_data.get('waiting_for_send_username'):
        username = message_text.strip()
        
        # Извлекаем имя пользователя без @
        if username.startswith('@'):
            username = username[1:]
        
        context.user_data['send_username'] = username
        context.user_data['waiting_for_send_username'] = False
        context.user_data['waiting_for_send_amount'] = True
        
        await update.message.reply_text(
            f"Сколько вы хотите отправить пользователю @{username}? Введите сумму:"
        )
        return
    
    # Обработка ожидания суммы для отправки денег
    elif context.user_data.get('waiting_for_send_amount'):
        try:
            amount = float(message_text.replace(',', '.'))
            if amount <= 0:
                await update.message.reply_text(
                    "Сумма должна быть положительным числом. Попробуйте снова:"
                )
                return
            
            context.user_data['send_amount'] = amount
            context.user_data['waiting_for_send_amount'] = False
            
            # Запрашиваем подтверждение
            keyboard = [
                [
                    InlineKeyboardButton("Подтвердить", callback_data="send_confirm"),
                    InlineKeyboardButton("Отменить", callback_data="send_cancel"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Проверяем, откуда пришел выбор пользователя - из меню или ручного ввода
            if context.user_data.get('send_receiver_id') and context.user_data.get('send_receiver_name'):
                # Если выбран из меню, используем ID и имя получателя
                receiver_id = context.user_data['send_receiver_id']
                receiver_name = context.user_data['send_receiver_name']
                await update.message.reply_text(
                    f"Вы собираетесь отправить {amount} руб. пользователю {receiver_name}. "
                    f"Подтвердите операцию:",
                    reply_markup=reply_markup
                )
            elif context.user_data.get('send_username'):
                # Если был введен username
                username = context.user_data['send_username']
                await update.message.reply_text(
                    f"Вы собираетесь отправить {amount} руб. пользователю @{username}. "
                    f"Подтвердите операцию:",
                    reply_markup=reply_markup
                )
            else:
                # Если каким-то образом нет данных о получателе
                await update.message.reply_text(
                    "Ошибка: не указан получатель платежа. Повторите операцию с помощью команды /send."
                )
        except ValueError:
            await update.message.reply_text(
                "Неверный формат суммы. Введите число:"
            )
        return
    
    # Обработка ожидания описания правил
    elif context.user_data.get('waiting_for_rules_description'):
        context.user_data['rules_description'] = message_text
        context.user_data['waiting_for_rules_description'] = False
        context.user_data['waiting_for_rules_deadline'] = True
        
        await update.message.reply_text(
            "Теперь укажите срок погашения долгов в часах (например, 24):"
        )
        return
    
    # Обработка ожидания срока погашения долгов
    elif context.user_data.get('waiting_for_rules_deadline'):
        try:
            deadline = int(message_text)
            if deadline <= 0:
                await update.message.reply_text(
                    "Срок должен быть положительным числом. Попробуйте снова:"
                )
                return
            
            context.user_data['rules_deadline'] = deadline
            context.user_data['waiting_for_rules_deadline'] = False
            context.user_data['waiting_for_rules_notifications'] = True
            
            await update.message.reply_text(
                "Укажите время для ежедневных уведомлений о долгах в формате ЧЧ:ММ (например, 20:00):"
            )
        except ValueError:
            await update.message.reply_text(
                "Неверный формат. Введите число часов:"
            )
        return
    
    # Обработка ожидания времени уведомлений
    elif context.user_data.get('waiting_for_rules_notifications'):
        time_pattern = re.compile(r'^([01]?[0-9]|2[0-3]):([0-5][0-9])$')
        
        if not time_pattern.match(message_text):
            await update.message.reply_text(
                "Неверный формат времени. Введите время в формате ЧЧ:ММ (например, 20:00):"
            )
            return
        
        # Сохраняем время уведомлений и настраиваем правила
        context.user_data['rules_notifications'] = message_text
        context.user_data['waiting_for_rules_notifications'] = False
        
        # Сохраняем правила в базе данных
        set_group_rules(
            chat.id,
            context.user_data['rules_description'],
            context.user_data['rules_deadline'],
            context.user_data['rules_notifications']
        )
        
        await update.message.reply_text(
            "Правила группы успешно настроены! 👍"
        )
    
    # Обработка ожидания новой суммы расхода для редактирования
    elif context.user_data.get('waiting_for_edit_expense_amount'):
        try:
            new_amount = float(message_text.replace(',', '.'))
            if new_amount <= 0:
                await update.message.reply_text(
                    "Сумма должна быть положительным числом. Попробуйте снова:"
                )
                return
            
            # Получаем ID расхода и старую сумму из контекста
            expense_id = context.user_data.get('edit_expense_id')
            old_amount = context.user_data.get('edit_expense_old_amount')
            description = context.user_data.get('edit_expense_description')
            
            if not expense_id:
                await update.message.reply_text(
                    "Ошибка: не удалось найти ID расхода. Пожалуйста, начните редактирование заново."
                )
                return
            
            # Обновляем сумму расхода
            success, message = update_expense_amount(expense_id, new_amount)
            
            # Очищаем данные редактирования
            context.user_data.pop('waiting_for_edit_expense_amount', None)
            context.user_data.pop('edit_expense_id', None)
            context.user_data.pop('edit_expense_old_amount', None)
            context.user_data.pop('edit_expense_description', None)
            
            if success:
                # Формируем сообщение об успешном обновлении
                update_message = (
                    f"✅ Сумма расхода успешно обновлена:\n\n"
                    f"Расход: {description}\n"
                    f"Старая сумма: {old_amount} руб.\n"
                    f"Новая сумма: {new_amount} руб."
                )
                await update.message.reply_text(update_message)
            else:
                await update.message.reply_text(f"❌ Ошибка: {message}")
            
        except ValueError:
            await update.message.reply_text(
                "Неверный формат суммы. Введите число:"
            )
            return

async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка изменений статуса бота в чате (получение/потеря прав администратора)."""
    chat_member_updated = update.my_chat_member
    chat = chat_member_updated.chat
    
    # Проверяем, что это групповой чат
    if chat.type not in ['group', 'supergroup']:
        return
    
    # Получаем предыдущий и новый статус бота
    old_status = chat_member_updated.old_chat_member.status
    new_status = chat_member_updated.new_chat_member.status
    
    # Логируем изменение статуса
    logger.info(f"Статус бота в группе {chat.id} ({chat.title}) изменен с {old_status} на {new_status}")
    
    # Проверяем, получил ли бот права администратора
    bot_member = chat_member_updated.new_chat_member
    if (new_status in ['administrator'] and 
        (old_status != 'administrator' or not getattr(chat_member_updated.old_chat_member, 'can_pin_messages', False)) and 
        bot_member.can_pin_messages):
        
        # Бот получил права администратора с возможностью закрепления сообщений
        # Проверяем, есть ли правила группы и закрепляем их
        await pin_group_rules_if_exist(context, chat.id)

async def pin_group_rules_if_exist(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Получает правила группы из базы данных и закрепляет их, если они существуют."""
    # Получаем правила группы
    rules = get_group_rules(chat_id)
    
    if not rules:
        logger.info(f"Правила группы {chat_id} не настроены, нечего закреплять")
        return
    
    try:
        # Формируем сообщение с правилами для закрепления
        rules_message = (
            "*ПРАВИЛА ГРУППЫ:*\n\n"
            f"• *Описание:* {rules.get('description', 'Не указано')}\n"
            f"• *Срок погашения:* {rules.get('deadline_hours', 'Не указано')} часов\n"
            f"• *Время уведомлений:* {rules.get('notifications_time', 'Не указано')}\n\n"
            "Используйте кнопки ниже для быстрого доступа к основным функциям:"
        )
        
        # Создаем инлайн кнопки для быстрого доступа
        keyboard = [
            [
                InlineKeyboardButton("➕ Добавить расход", callback_data="help_addexpense"),
                InlineKeyboardButton("💰 Мой долг", callback_data="help_mydebt")
            ],
            [
                InlineKeyboardButton("📊 Отчет", callback_data="help_report"),
                InlineKeyboardButton("💸 Отправить деньги", callback_data="help_send")
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Проверяем, есть ли уже закрепленное сообщение от бота
        chat = await context.bot.get_chat(chat_id)
        if chat.pinned_message and chat.pinned_message.from_user.id == context.bot.id:
            # Если есть закрепленное сообщение от бота, открепляем его
            await context.bot.unpin_chat_message(
                chat_id=chat_id,
                message_id=chat.pinned_message.message_id
            )
            logger.info(f"Откреплено старое сообщение с правилами в группе {chat_id}")
        
        # Отправляем и закрепляем новое сообщение с правилами
        pinned_message = await context.bot.send_message(
            chat_id=chat_id,
            text=rules_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        
        # Закрепляем сообщение
        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=pinned_message.message_id,
            disable_notification=False
        )
        
        logger.info(f"Правила группы {chat_id} успешно закреплены")
        
    except Exception as e:
        logger.error(f"Ошибка при закреплении правил группы {chat_id}: {e}")

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command."""
    user = update.effective_user
    chat = update.effective_chat
    
    # Save user info
    save_user(user.id, user.username, user.first_name, user.last_name)
    
    # Handle group chats
    if chat.type in ['group', 'supergroup']:
        # Логируем информацию о пользователе и группе
        logger.info(f"Start command from user {user.id} (@{user.username}, {user.first_name} {user.last_name}) in group {chat.id} ({chat.title})")
        
        # Save group info
        saved_group = save_group(chat.id, chat.title)
        # Add user to group
        added_to_group = add_user_to_group(chat.id, user.id)
        
        logger.info(f"Save group result: {saved_group}, Add user to group result: {added_to_group}")
        
        # Получаем список участников для проверки
        members = get_group_members(chat.id)
        logger.info(f"Group {chat.id} has {len(members)} members: {members}")
        
        await update.message.reply_html(
            f"Привет! Я бот для учета совместных расходов. "
            f"Чтобы узнать, как меня использовать, отправьте /help."
        )
    else:
        # Personal chat
        await update.message.reply_html(
            f"Привет, {user.mention_html()}! Я бот для учета совместных расходов. "
            f"Добавьте меня в группу, чтобы начать использовать мои функции."
        )

async def reset_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка команды /reset для сброса всех данных группы без удаления пользователей."""
    user = update.effective_user
    chat = update.effective_chat
    
    # Сохраняем информацию о пользователе
    save_user(user.id, user.username, user.first_name, user.last_name)
    
    # Проверяем, что команда вызвана в групповом чате
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text(
            "Эта команда работает только в группах."
        )
        return
    
    # Проверяем, что пользователь администратор
    if not await is_admin(update, context):
        await update.message.reply_text(
            "Только администраторы группы могут использовать эту команду."
        )
        return
    
    # Создаем кнопки для подтверждения/отмены сброса
    keyboard = [
        [
            InlineKeyboardButton("Да, подтверждаю", callback_data="reset_confirm"),
            InlineKeyboardButton("Отмена", callback_data="reset_cancel")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "⚠️ *ВНИМАНИЕ!* ⚠️\n\n"
        "Вы собираетесь сбросить *ВСЮ* историю группы.\n"
        "Это удалит все расходы, долги, транзакции и правила группы.\n"
        "Пользователи останутся в группе, но вся их финансовая история будет удалена.\n\n"
        "*Это действие необратимо.*\n\n"
        "Вы уверены, что хотите продолжить?",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка команды /help с инлайн кнопками для всех команд."""
    message = update.message
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Регистрируем незавершенную операцию
    await register_pending_operation(
        user_id=user_id,
        operation_type="help_command",
        chat_id=chat_id,
        message_id=message.message_id
    )
    
    help_text = (
        "*Команды бота:*\n\n"
        "Нажмите на кнопку ниже для выполнения соответствующей команды.\n\n"
        "*Как использовать:*\n"
        "1. Добавляйте расходы\n"
        "2. Проверяйте свой долг\n"
        "3. Отправляйте деньги участникам\n"
        "4. Получайте отчеты\n"
        "5. Администраторы имеют дополнительные функции"
    )
    
    # Создаем инлайн кнопки для всех команд
    keyboard = [
        [
            InlineKeyboardButton("➕ Добавить расход", callback_data="help_addexpense"),
            InlineKeyboardButton("💰 Мой долг", callback_data="help_mydebt")
        ],
        [
            InlineKeyboardButton("📊 Отчет", callback_data="help_report"),
            InlineKeyboardButton("💸 Отправить деньги", callback_data="help_send")
        ],
        [
            InlineKeyboardButton("⚙️ Правила группы", callback_data="help_rules"),
            InlineKeyboardButton("ℹ️ О боте", callback_data="help_about")
        ]
    ]
    
    # Проверяем, является ли пользователь администратором, и если да, добавляем кнопку админа
    is_user_admin = await is_admin(update, context)
    if is_user_admin:
        keyboard.append([
            InlineKeyboardButton("🔧 Администрирование", callback_data="help_admin")
        ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Отправляем сообщение с помощью
    help_message = await message.reply_markdown(help_text, reply_markup=reply_markup)
    
    # Планируем удаление исходной команды
    await schedule_message_deletion(
        context=context,
        chat_id=chat_id,
        message_id=message.message_id
    )
    
    # Планируем удаление сообщения с помощью, но с более длительным таймером,
    # так как это интерактивное меню
    await schedule_message_deletion(
        context=context,
        chat_id=chat_id,
        message_id=help_message.message_id,
        user_id=user_id,
        operation_type="help_command",
        extend_if_pending=True
    )
    
    # Добавляем сообщение с помощью в цепочку сообщений
    await add_message_to_chain(
        parent_key=(chat_id, message.message_id),
        child_key=(chat_id, help_message.message_id)
    )

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the /rules command."""
    user = update.effective_user
    chat = update.effective_chat
    
    # Save user info
    save_user(user.id, user.username, user.first_name, user.last_name)
    
    # Check if in group chat
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text(
            "Эта команда работает только в группах."
        )
        return ConversationHandler.END
    
    # Check if user is admin
    if not await is_admin(update, context):
        # Just show rules
        rules = get_group_rules(chat.id)
        if rules:
            await update.message.reply_text(
                f"*Правила группы:*\n\n"
                f"• *Описание:* {rules['description']}\n"
                f"• *Срок погашения:* {rules['deadline_hours']} часов\n"
                f"• *Время уведомлений:* {rules['notifications_time']}",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "В этой группе ещё не настроены правила. "
                "Администратор может настроить их с помощью команды /rules."
            )
        return ConversationHandler.END
    
    # Admin is configuring rules
    await update.message.reply_text(
        "Давайте настроим правила группы.\n\n"
        "Введите описание правил (например, 'Делим поровну'):"
    )
    
    return RULES_DESCRIPTION

async def rules_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the rules description input."""
    context.user_data['rules_description'] = update.message.text
    
    await update.message.reply_text(
        "Теперь укажите срок погашения долгов в часах (например, 24):"
    )
    
    return RULES_DEADLINE

async def rules_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the rules deadline input."""
    try:
        deadline = int(update.message.text)
        if deadline <= 0:
            await update.message.reply_text(
                "Срок должен быть положительным числом. Попробуйте снова:"
            )
            return RULES_DEADLINE
        
        context.user_data['rules_deadline'] = deadline
        
        await update.message.reply_text(
            "Укажите время для ежедневных уведомлений о долгах в формате ЧЧ:ММ (например, 20:00):"
        )
        
        return RULES_NOTIFICATIONS
    except ValueError:
        await update.message.reply_text(
            "Неверный формат. Введите число часов:"
        )
        return RULES_DEADLINE

async def rules_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the rules notifications time input."""
    time_pattern = re.compile(r'^([01]?[0-9]|2[0-3]):([0-5][0-9])$')
    
    if not time_pattern.match(update.message.text):
        await update.message.reply_text(
            "Неверный формат времени. Введите время в формате ЧЧ:ММ (например, 20:00):"
        )
        return RULES_NOTIFICATIONS
    
    context.user_data['rules_notifications'] = update.message.text
    
    # Save rules to database
    set_group_rules(
        update.effective_chat.id,
        context.user_data['rules_description'],
        context.user_data['rules_deadline'],
        context.user_data['rules_notifications']
    )
    
    # Создаем сообщение об успешной настройке правил
    await update.message.reply_text(
        "Правила группы успешно настроены! 👍"
    )
    
    # Проверяем, есть ли у бота права на закрепление сообщений
    try:
        # Получаем информацию о боте в чате
        bot_member = await context.bot.get_chat_member(
            update.effective_chat.id, 
            context.bot.id
        )
        
        # Проверяем права бота на закрепление сообщений
        can_pin = bot_member.can_pin_messages
        
        if can_pin:
            # Формируем сообщение с правилами для закрепления
            rules_message = (
                "*ПРАВИЛА ГРУППЫ:*\n\n"
                f"• *Описание:* {context.user_data['rules_description']}\n"
                f"• *Срок погашения:* {context.user_data['rules_deadline']} часов\n"
                f"• *Время уведомлений:* {context.user_data['rules_notifications']}\n\n"
                "Используйте кнопки ниже для быстрого доступа к основным функциям:"
            )
            
            # Создаем инлайн кнопки для быстрого доступа
            keyboard = [
                [
                    InlineKeyboardButton("➕ Добавить расход", callback_data="help_addexpense"),
                    InlineKeyboardButton("💰 Мой долг", callback_data="help_mydebt")
                ],
                [
                    InlineKeyboardButton("📊 Отчет", callback_data="help_report"),
                    InlineKeyboardButton("💸 Отправить деньги", callback_data="help_send")
                ]
            ]
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Отправляем сообщение с правилами и кнопками
            pinned_message = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=rules_message,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            
            # Закрепляем сообщение
            await context.bot.pin_chat_message(
                chat_id=update.effective_chat.id,
                message_id=pinned_message.message_id,
                disable_notification=False
            )
            
            await update.message.reply_text(
                "Правила группы закреплены в чате для удобного доступа! 📌"
            )
        else:
            await update.message.reply_text(
                "Я не могу закрепить правила, так как у меня нет прав администратора "
                "с возможностью закрепления сообщений. Чтобы я мог закреплять правила, "
                "пожалуйста, назначьте меня администратором и предоставьте права на закрепление сообщений."
            )
    except Exception as e:
        logger.error(f"Ошибка при закреплении правил: {e}")
        await update.message.reply_text(
            "Не удалось закрепить правила группы. Пожалуйста, убедитесь, что бот имеет "
            "необходимые права администратора."
        )
    
    return ConversationHandler.END

async def add_expense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the /addexpense command."""
    user = update.effective_user
    chat = update.effective_chat
    
    # Save user info
    save_user(user.id, user.username, user.first_name, user.last_name)
    
    # Планируем удаление исходной команды пользователя
    try:
        await context.bot.delete_message(
            chat_id=chat.id,
            message_id=update.message.message_id
        )
    except Exception as e:
        logger.info(f"Не удалось удалить команду: {e}")
    
    # Check if in group chat
    if chat.type not in ['group', 'supergroup']:
        message = await update.message.reply_text(
            "Эта команда работает только в группах."
        )
        # Планируем удаление сообщения
        await schedule_message_deletion(context, chat.id, message.message_id)
        return ConversationHandler.END
    
    # Parse command arguments if provided
    if context.args and len(context.args) >= 2:
        try:
            amount = float(context.args[0])
            description = ' '.join(context.args[1:])
            
            success, result = handle_new_expense(
                chat.id, amount, description, user.id
            )
            
            if success:
                message = await update.message.reply_text(
                    f"✅ Расход успешно добавлен: {amount} руб. за {description}"
                )
                # Планируем удаление сообщения
                await schedule_message_deletion(context, chat.id, message.message_id)
            else:
                message = await update.message.reply_text(
                    f"❌ Ошибка: {result}"
                )
                # Планируем удаление сообщения об ошибке
                await schedule_message_deletion(context, chat.id, message.message_id)
            
            return ConversationHandler.END
        except ValueError:
            # Continue with conversation if arguments are invalid
            pass
    
    # Создаем подменю для выбора типа добавления расхода
    keyboard = [
        [
            InlineKeyboardButton("На всех участников группы", callback_data="expense_all_members"),
            InlineKeyboardButton("Выборочно", callback_data="expense_selective")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = await update.message.reply_text(
        "Как вы хотите добавить расход?",
        reply_markup=reply_markup
    )
    
    # Регистрируем незавершенную операцию и планируем удаление с напоминанием
    await register_pending_operation(
        user_id=user.id,
        operation_type="expense_add",
        chat_id=chat.id,
        message_id=message.message_id
    )
    
    # Планируем удаление сообщения с проверкой незавершенной операции
    await schedule_message_deletion(
        context=context,
        chat_id=chat.id,
        message_id=message.message_id,
        user_id=user.id,
        operation_type="expense_add"
    )
    
    # Сохраняем информацию о том, что мы в процессе выбора типа расхода
    context.user_data['expense_add_state'] = 'selecting_type'
    
    return EXPENSE_AMOUNT

async def expense_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the expense amount input."""
    user = update.effective_user
    chat = update.effective_chat
    
    # Планируем удаление сообщения пользователя
    try:
        await context.bot.delete_message(
            chat_id=chat.id,
            message_id=update.message.message_id
        )
    except Exception as e:
        logger.info(f"Не удалось удалить сообщение пользователя: {e}")
    
    try:
        amount = float(update.message.text.replace(',', '.'))
        if amount <= 0:
            message = await update.message.reply_text(
                "Сумма должна быть положительным числом. Попробуйте снова:"
            )
            
            # Планируем удаление сообщения с проверкой незавершенной операции
            await schedule_message_deletion(
                context=context,
                chat_id=chat.id,
                message_id=message.message_id,
                user_id=user.id,
                operation_type="expense_add"
            )
            
            return EXPENSE_AMOUNT
        
        context.user_data['expense_amount'] = amount
        
        message = await update.message.reply_text(
            "Теперь введите описание расхода:"
        )
        
        # Планируем удаление сообщения с проверкой незавершенной операции
        await schedule_message_deletion(
            context=context,
            chat_id=chat.id,
            message_id=message.message_id,
            user_id=user.id,
            operation_type="expense_add"
        )
        
        return EXPENSE_DESCRIPTION
    except ValueError:
        message = await update.message.reply_text(
            "Неверный формат суммы. Введите число:"
        )
        
        # Планируем удаление сообщения с проверкой незавершенной операции
        await schedule_message_deletion(
            context=context,
            chat_id=chat.id,
            message_id=message.message_id,
            user_id=user.id,
            operation_type="expense_add"
        )
        
        return EXPENSE_AMOUNT

async def expense_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка ввода описания расхода."""
    user = update.effective_user
    chat = update.effective_chat
    chat_id = chat.id
    
    # Планируем удаление сообщения пользователя
    try:
        await context.bot.delete_message(
            chat_id=chat_id,
            message_id=update.message.message_id
        )
    except Exception as e:
        logger.info(f"Не удалось удалить сообщение пользователя: {e}")
    
    context.user_data['expense_description'] = update.message.text
    
    # Проверяем выбранный режим расхода
    expense_all_members = context.user_data.get('expense_all_members', False)
    
    # Получаем информацию о группе и её участниках
    if chat_id < 0:  # Это групповой чат
        # Если выбран режим "на всех участников", сразу переходим к запросу о фото
        if expense_all_members:
            # Получаем всех участников группы (кроме ботов)
            members = get_group_members(chat_id, exclude_bots=True)
            
            # Сохраняем список всех участников в контексте
            context.user_data['all_participants'] = [m['user_id'] for m in members]
            # Автоматически выбираем всех участников
            context.user_data['selected_participants'] = [m['user_id'] for m in members]
            
            # Переходим к запросу фото чека
            keyboard = [
                [
                    InlineKeyboardButton("Да", callback_data="expense_photo_yes"),
                    InlineKeyboardButton("Нет", callback_data="expense_photo_no"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            message = await update.message.reply_text(
                "Хотите прикрепить фото чека?",
                reply_markup=reply_markup
            )
            
            # Планируем удаление сообщения с проверкой незавершенной операции
            await schedule_message_deletion(
                context=context,
                chat_id=chat_id,
                message_id=message.message_id,
                user_id=user.id,
                operation_type="expense_add"
            )
            
            return EXPENSE_PHOTO
        
        # Для режима "выборочно" показываем список участников для выбора
        else:
            # Получаем список участников группы
            members = get_group_members(chat_id, exclude_bots=True)
            
            # Если есть участники, предлагаем выбрать среди них
            if members and len(members) > 1:
                # Создаем кнопки для каждого участника
                keyboard = []
                row = []
                for i, member in enumerate(members):
                    full_name = f"{member.get('first_name', '')} {member.get('last_name', '')}".strip()
                    username = member.get('username', '')
                    display_name = full_name if full_name else username
                    
                    # Создаем кнопку для участника
                    user_id = member['user_id']
                    callback_data = f"participant_{user_id}"
                    button = InlineKeyboardButton(display_name, callback_data=callback_data)
                    
                    # Добавляем максимум 2 кнопки в строку
                    row.append(button)
                    if len(row) == 2 or i == len(members) - 1:
                        keyboard.append(row)
                        row = []
                
                # Добавляем кнопки "Выбрать всех" и "Готово"
                keyboard.append([
                    InlineKeyboardButton("Выбрать всех", callback_data="participants_all"),
                    InlineKeyboardButton("Готово", callback_data="participants_done")
                ])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                # Сохраняем список участников и инициализируем выбранных
                context.user_data['all_participants'] = [m['user_id'] for m in members]
                context.user_data['selected_participants'] = []
                
                message = await update.message.reply_text(
                    "Выберите участников для разделения расхода:",
                    reply_markup=reply_markup
                )
                
                # Планируем удаление сообщения с проверкой незавершенной операции
                await schedule_message_deletion(
                    context=context,
                    chat_id=chat_id,
                    message_id=message.message_id,
                    user_id=user.id,
                    operation_type="expense_add"
                )
                
                return EXPENSE_PARTICIPANTS
    
    # Если не группа или нет участников, просто спрашиваем о фото
    keyboard = [
        [
            InlineKeyboardButton("Да", callback_data="expense_photo_yes"),
            InlineKeyboardButton("Нет", callback_data="expense_photo_no"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = await update.message.reply_text(
        "Хотите прикрепить фото чека?",
        reply_markup=reply_markup
    )
    
    # Планируем удаление сообщения с проверкой незавершенной операции
    await schedule_message_deletion(
        context=context,
        chat_id=chat_id,
        message_id=message.message_id,
        user_id=user.id,
        operation_type="expense_add"
    )
    
    return EXPENSE_PHOTO

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка нажатий на кнопки."""
    query = update.callback_query
    await query.answer()
    
    # Отмечаем операцию как завершенную, если она была связана с кнопкой
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    user = update.effective_user
    chat = update.effective_chat
    
    # Обработка кнопок выбора типа добавления расхода
    if query.data == "expense_all_members":
        # Добавление расхода на всех участников группы
        await query.edit_message_text(
            "Добавление расхода на всех участников группы.\n\n"
            "Введите сумму расхода (только число):"
        )
        
        # Сохраняем информацию о выбранном типе расхода
        context.user_data['expense_all_members'] = True
        context.user_data['expense_add_state'] = 'waiting_for_amount'
        
        # Регистрируем незавершенную операцию
        await register_pending_operation(
            user_id=user_id,
            operation_type="expense_add",
            chat_id=chat_id,
            message_id=query.message.message_id
        )
        
        return EXPENSE_AMOUNT
        
    elif query.data == "expense_selective":
        # Добавление расхода выборочно
        await query.edit_message_text(
            "Добавление расхода на выбранных участников.\n\n"
            "Введите сумму расхода (только число):"
        )
        
        # Сохраняем информацию о выбранном типе расхода
        context.user_data['expense_all_members'] = False
        context.user_data['expense_add_state'] = 'waiting_for_amount'
        
        # Регистрируем незавершенную операцию
        await register_pending_operation(
            user_id=user_id,
            operation_type="expense_add",
            chat_id=chat_id,
            message_id=query.message.message_id
        )
        
        return EXPENSE_AMOUNT
    
    # Обработка кнопок из меню помощи или админских кнопок
    if query.data.startswith("help_") or query.data.startswith("admin_"):
        if query.data.startswith("help_"):
            command_type = "help"
            command = query.data.split("_")[1]
        else:  # admin_
            command_type = "admin"
            command = query.data.split("_")[1]
            
            # Особая обработка админских команд из help-меню
            if command in ["edit_expenses", "delete_expenses", "delete_transactions", "reset", "back"]:
                # Проверяем права администратора
                is_user_admin = await is_admin(update, context)
                
                if not is_user_admin:
                    await query.edit_message_text(
                        "❌ Только администраторы группы могут выполнять эти действия."
                    )
                    return ConversationHandler.END
                
                # Обрабатываем каждую админскую кнопку
                if command == "edit_expenses":
                    # Получаем список всех расходов группы
                    expenses = get_group_expenses(chat.id)
                    
                    if not expenses:
                        await query.edit_message_text(
                            "В этой группе еще нет расходов."
                        )
                        return ConversationHandler.END
                        
                    # Создаем кнопки для каждого расхода
                    keyboard = []
                    for expense in expenses[:10]:  # Ограничиваем до 10 последних расходов
                        description = expense['description']
                        amount = expense['amount']
                        exp_id = expense['id']
                        
                        # Формируем текст кнопки
                        button_text = f"{description} ({amount} руб.)"
                        
                        # Добавляем кнопку
                        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"edit_expense_{exp_id}")])
                    
                    # Добавляем кнопку назад
                    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="help_admin")])
                    
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await query.edit_message_text(
                        "Выберите расход для редактирования:",
                        reply_markup=reply_markup
                    )
                    return ConversationHandler.END
                
                elif command == "delete_expenses":
                    # Получаем список всех расходов группы
                    expenses = get_group_expenses(chat.id)
                    
                    if not expenses:
                        await query.edit_message_text(
                            "В этой группе еще нет расходов."
                        )
                        return ConversationHandler.END
                        
                    # Создаем кнопки для каждого расхода
                    keyboard = []
                    for expense in expenses[:10]:  # Ограничиваем до 10 последних расходов
                        description = expense['description']
                        amount = expense['amount']
                        exp_id = expense['id']
                        
                        # Формируем текст кнопки
                        button_text = f"{description} ({amount} руб.)"
                        
                        # Добавляем кнопку
                        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"delete_expense_{exp_id}")])
                    
                    # Добавляем кнопку назад
                    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="help_admin")])
                    
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await query.edit_message_text(
                        "Выберите расход для удаления:",
                        reply_markup=reply_markup
                    )
                    return ConversationHandler.END
                
                elif command == "delete_transactions":
                    # Получаем список всех транзакций группы
                    transactions = get_group_transactions(chat.id)
                    
                    if not transactions:
                        await query.edit_message_text(
                            "В этой группе еще нет транзакций."
                        )
                        return ConversationHandler.END
                        
                    # Создаем кнопки для каждой транзакции
                    keyboard = []
                    for tx in transactions[:10]:  # Ограничиваем до 10 последних транзакций
                        sender_name = tx.get('sender_username', tx.get('sender_first_name', 'Неизвестно'))
                        receiver_name = tx.get('receiver_username', tx.get('receiver_first_name', 'Неизвестно'))
                        amount = tx['amount']
                        tx_id = tx['id']
                        
                        # Формируем текст кнопки
                        button_text = f"{sender_name} → {receiver_name} ({amount} руб.)"
                        
                        # Добавляем кнопку
                        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"delete_transaction_{tx_id}")])
                    
                    # Добавляем кнопку назад
                    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="help_admin")])
                    
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await query.edit_message_text(
                        "Выберите транзакцию для удаления:",
                        reply_markup=reply_markup
                    )
                    return ConversationHandler.END
                    
                elif command == "back":
                    # Возвращаемся к основному меню помощи
                    help_text = (
                        "*Команды бота:*\n\n"
                        "Нажмите на кнопку ниже для выполнения соответствующей команды.\n\n"
                        "*Как использовать:*\n"
                        "1. Добавляйте расходы\n"
                        "2. Проверяйте свой долг\n"
                        "3. Отправляйте деньги участникам\n"
                        "4. Получайте отчеты\n"
                        "5. Администраторы имеют дополнительные функции"
                    )
                    
                    # Создаем инлайн кнопки для всех команд
                    keyboard = [
                        [
                            InlineKeyboardButton("➕ Добавить расход", callback_data="help_addexpense"),
                            InlineKeyboardButton("💰 Мой долг", callback_data="help_mydebt")
                        ],
                        [
                            InlineKeyboardButton("📊 Отчет", callback_data="help_report"),
                            InlineKeyboardButton("💸 Отправить деньги", callback_data="help_send")
                        ],
                        [
                            InlineKeyboardButton("⚙️ Правила группы", callback_data="help_rules"),
                            InlineKeyboardButton("ℹ️ О боте", callback_data="help_about")
                        ],
                        [
                            InlineKeyboardButton("🔧 Администрирование", callback_data="help_admin")
                        ]
                    ]
                    
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await query.edit_message_text(
                        text=help_text,
                        reply_markup=reply_markup,
                        parse_mode='Markdown'
                    )
                    return ConversationHandler.END
        
        user = update.effective_user
        chat = update.effective_chat
        
        # Отмечаем завершение операции help_command
        await complete_pending_operation(user_id)
        
        # Обработка кнопки администрирования
        if command == "admin":
            # Проверяем, является ли пользователь администратором
            is_user_admin = await is_admin(update, context)
            
            if not is_user_admin:
                await query.edit_message_text(
                    "Только администраторы группы имеют доступ к этому меню."
                )
                return ConversationHandler.END
            
            # Создаем админ-меню
            admin_text = (
                "*Меню администратора*\n\n"
                "Выберите действие из списка ниже:"
            )
            
            keyboard = [
                [
                    InlineKeyboardButton("📝 Редактировать расходы", callback_data="admin_edit_expenses"),
                    InlineKeyboardButton("🗑️ Удалить расходы", callback_data="admin_delete_expenses")
                ],
                [
                    InlineKeyboardButton("🧹 Удалить транзакции", callback_data="admin_delete_transactions"),
                    InlineKeyboardButton("⚙️ Настроить правила", callback_data="help_rules")
                ],
                [
                    InlineKeyboardButton("♻️ Сбросить данные группы", callback_data="admin_reset"),
                    InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")
                ]
            ]
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                text=admin_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return ConversationHandler.END
        
        if command == "addexpense":
            # Отправляем новое сообщение вместо запуска команды напрямую
            await query.edit_message_text(
                "Добавление нового расхода.\n\n"
                "Введите сумму расхода (только число):"
            )
            # Сохраняем состояние в user_data чтобы продолжить диалог позже
            context.user_data['waiting_for_expense_amount'] = True
            return ConversationHandler.END
        
        elif command == "mydebt":
            # Получаем информацию о долге пользователя напрямую
            debt_message = format_debt_message(user.id, chat.id)
            
            # Проверяем наличие ожидающих подтверждения переводов
            pending_transactions = get_pending_transactions(user.id, as_receiver=True)
            
            # Отображаем информацию о долге
            await query.edit_message_text(
                text=debt_message,
                parse_mode='Markdown'
            )
            
            # Если есть ожидающие подтверждения переводы, отображаем их отдельными сообщениями
            if pending_transactions:
                for transaction in pending_transactions:
                    sender = get_user(transaction['sender_id'])
                    sender_name = sender.get('username', sender.get('first_name', 'Unknown'))
                    
                    keyboard = [
                        [
                            InlineKeyboardButton("Подтвердить получение", 
                                                callback_data=f"confirm_transaction_{transaction['id']}"),
                            InlineKeyboardButton("Отклонить", 
                                                callback_data=f"reject_transaction_{transaction['id']}"),
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=f"Перевод от @{sender_name} на сумму {transaction['amount']:.2f} руб.",
                        reply_markup=reply_markup
                    )
            
            return ConversationHandler.END
        
        elif command == "report":
            # Проверяем, является ли пользователь администратором
            is_user_admin = await is_admin(update, context)
            
            if not is_user_admin:
                await query.edit_message_text(
                    "Только администраторы могут генерировать отчеты."
                )
                return ConversationHandler.END
            
            # Начинаем генерацию отчета
            await query.edit_message_text("Генерирую отчеты, пожалуйста подождите...")
            
            # Генерируем отчеты
            excel_report = generate_excel_report(chat.id)
            pdf_report = generate_pdf_report(chat.id)
            
            # Отправляем отчеты отдельными сообщениями
            if excel_report:
                await context.bot.send_document(
                    chat_id=chat.id,
                    document=excel_report,
                    filename=f"expenses_report_{chat.id}.xlsx",
                    caption="Отчет о расходах (Excel)"
                )
            else:
                await context.bot.send_message(
                    chat_id=chat.id,
                    text="Не удалось создать Excel отчет."
                )
            
            if pdf_report:
                await context.bot.send_document(
                    chat_id=chat.id,
                    document=pdf_report,
                    filename=f"expenses_report_{chat.id}.pdf",
                    caption="Отчет о расходах (PDF)"
                )
            else:
                await context.bot.send_message(
                    chat_id=chat.id,
                    text="Не удалось создать PDF отчет."
                )
            
            return ConversationHandler.END
        
        elif command == "send":
            # Сохраняем группу и добавляем текущего пользователя
            logger.info(f"Saving group {chat.id} ({chat.title}) and adding user {user.id}")
            save_group(chat.id, chat.title)
            add_user_to_group(chat.id, user.id)
            
            # Добавляем администраторов чата в группу бота
            chat_members = await context.bot.get_chat_administrators(chat.id)
            logger.info(f"Found {len(chat_members)} admins in chat {chat.id}")
            
            for member in chat_members:
                member_user = member.user
                logger.info(f"Adding admin {member_user.id} (@{member_user.username}) to group {chat.id}")
                save_user(member_user.id, member_user.username, member_user.first_name, member_user.last_name)
                add_user_to_group(chat.id, member_user.id)
                
            # Получаем список участников группы для выбора
            logger.info(f"Get group members for help/send. Chat ID: {chat.id}")
            members = get_group_members(chat.id)
            logger.info(f"Found {len(members) if members else 0} members for chat {chat.id}: {members}")
            
            if members and len(members) > 1:
                # Создаем кнопки для каждого участника
                keyboard = []
                for member in members:
                    # Пропускаем текущего пользователя
                    if member['user_id'] == user.id:
                        continue
                        
                    # Формируем отображаемое имя
                    first_name = member.get('first_name', '')
                    last_name = member.get('last_name', '')
                    username = member.get('username', '')
                    user_id = member['user_id']
                    
                    # Создаем текст кнопки (имя/юзернейм)
                    if first_name and last_name:
                        display_name = f"{first_name} {last_name}"
                    elif username:
                        display_name = f"@{username}"
                    else:
                        display_name = f"ID: {user_id}"
                        
                    # Добавляем кнопку
                    keyboard.append([InlineKeyboardButton(
                        display_name, 
                        callback_data=f"send_to_{user_id}"
                    )])
                
                # Добавляем кнопку отмены
                keyboard.append([InlineKeyboardButton("Отмена", callback_data="send_cancel")])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.edit_message_text(
                    "Выберите пользователя, которому хотите отправить деньги:",
                    reply_markup=reply_markup
                )
                
                return SEND_AMOUNT
            else:
                # Если участников мало или их нет, используем стандартный ввод
                await query.edit_message_text(
                    "Кому вы хотите отправить деньги? Введите @username:"
                )
                # Сохраняем флаг для обработки следующего сообщения
                context.user_data['waiting_for_send_username'] = True
                
                return ConversationHandler.END
        
        elif command == "rules":
            # Проверяем правила группы
            rules_data = get_group_rules(chat.id)
            
            if rules_data:
                # Если правила существуют, показываем их
                await query.edit_message_text(
                    f"*Правила группы:*\n\n"
                    f"• *Описание:* {rules_data['description']}\n"
                    f"• *Срок погашения:* {rules_data['deadline_hours']} часов\n"
                    f"• *Время уведомлений:* {rules_data['notifications_time']}",
                    parse_mode='Markdown'
                )
            else:
                # Если правил нет и пользователь - админ, предлагаем настроить
                is_user_admin = await is_admin(update, context)
                
                if is_user_admin:
                    await query.edit_message_text(
                        "В этой группе ещё не настроены правила.\n"
                        "Хотите настроить их сейчас?",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("Да", callback_data="setup_rules_yes"),
                            InlineKeyboardButton("Нет", callback_data="setup_rules_no")]
                        ])
                    )
                else:
                    await query.edit_message_text(
                        "В этой группе ещё не настроены правила. "
                        "Администратор может настроить их с помощью команды /rules."
                    )
            
            return ConversationHandler.END
        
        elif command == "about":
            # Показываем информацию о боте
            about_text = (
                "*О боте для учета расходов*\n\n"
                "Этот бот помогает группам друзей или коллег вести учет совместных расходов и разделять их между участниками.\n\n"
                "*Основные возможности:*\n"
                "• Добавление расходов с выбором участников\n"
                "• Автоматический расчет долга для каждого участника\n"
                "• Загрузка фото чеков для подтверждения расходов\n"
                "• Перевод денег между участниками\n"
                "• Генерация отчетов для контроля финансов\n\n"
                "Создан с использованием Python и библиотеки python-telegram-bot."
            )
            
            await query.edit_message_text(
                text=about_text,
                parse_mode='Markdown'
            )
            return ConversationHandler.END
    
    # Обработка выбора участников для расхода
    elif query.data.startswith("participant_"):
        user_id = int(query.data.split("_")[1])
        
        # Если пользователь уже выбран, удаляем его из списка, иначе добавляем
        if user_id in context.user_data.get('selected_participants', []):
            context.user_data['selected_participants'].remove(user_id)
        else:
            if 'selected_participants' not in context.user_data:
                context.user_data['selected_participants'] = []
            context.user_data['selected_participants'].append(user_id)
        
        # Обновляем сообщение с отметкой выбранных участников
        message_text = "Выберите участников для разделения расхода:\n\n"
        
        for member_id in context.user_data.get('all_participants', []):
            member = get_user(member_id)
            if member:
                # Используем имя и фамилию для отображения
                first_name = member.get('first_name', '')
                last_name = member.get('last_name', '')
                full_name = f"{first_name} {last_name}".strip()
                
                # Если нет имени и фамилии, используем никнейм
                display_name = full_name if full_name else member.get('username', 'Без имени')
                
                if member_id in context.user_data.get('selected_participants', []):
                    message_text += f"✅ {display_name}\n"
                else:
                    message_text += f"⬜ {display_name}\n"
        
        await query.edit_message_text(
            text=message_text,
            reply_markup=query.message.reply_markup
        )
        
        return EXPENSE_PARTICIPANTS
    
    elif query.data == "participants_all":
        # Выбираем всех участников
        context.user_data['selected_participants'] = list(context.user_data.get('all_participants', []))
        
        # Обновляем сообщение с отметкой всех участников
        message_text = "Выберите участников для разделения расхода:\n\n"
        
        for member_id in context.user_data.get('all_participants', []):
            member = get_user(member_id)
            if member:
                # Используем имя и фамилию для отображения
                first_name = member.get('first_name', '')
                last_name = member.get('last_name', '')
                full_name = f"{first_name} {last_name}".strip()
                
                # Если нет имени и фамилии, используем никнейм
                display_name = full_name if full_name else member.get('username', 'Без имени')
                
                message_text += f"✅ {display_name}\n"
        
        await query.edit_message_text(
            text=message_text,
            reply_markup=query.message.reply_markup
        )
        
        return EXPENSE_PARTICIPANTS
    
    elif query.data == "participants_done":
        # Переходим к вопросу о фото
        keyboard = [
            [
                InlineKeyboardButton("Да", callback_data="expense_photo_yes"),
                InlineKeyboardButton("Нет", callback_data="expense_photo_no"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "Хотите прикрепить фото чека?",
            reply_markup=reply_markup
        )
        
        return EXPENSE_PHOTO
    
    # Обработка выбора фото чека
    elif query.data == "expense_photo_yes":
        await query.edit_message_text(
            "Отправьте фото чека:"
        )
        return EXPENSE_PHOTO
    
    elif query.data == "expense_photo_no":
        # Сохраняем расход без фото
        user = update.effective_user
        chat_id = update.effective_chat.id
        
        # Используем выбранных участников, если они есть
        participants = context.user_data.get('selected_participants')
        
        # Отмечаем операцию как завершенную
        await complete_pending_operation(user.id)
        
        success, result = handle_new_expense(
            chat_id,
            context.user_data['expense_amount'],
            context.user_data['expense_description'],
            user.id,
            participants=participants
        )
        
        if success:
            # Формируем сообщение об успехе с деталями
            success_message = (
                f"✅ Расход успешно добавлен: {context.user_data['expense_amount']} руб. "
                f"за {context.user_data['expense_description']}"
            )
            
            # Если выбраны конкретные участники, добавляем информацию
            if participants and len(participants) > 0:
                # Получаем имена участников
                names = []
                for participant_id in participants:
                    participant = get_user(participant_id)
                    if participant:
                        # Используем имя и фамилию для отображения
                        first_name = participant.get('first_name', '')
                        last_name = participant.get('last_name', '')
                        full_name = f"{first_name} {last_name}".strip()
                        
                        # Если нет имени и фамилии, используем никнейм
                        display_name = full_name if full_name else participant.get('username', 'Без имени')
                        names.append(display_name)
                
                success_message += f"\nУчастники: {', '.join(names)}"
            
            # Редактируем сообщение с результатом
            await query.edit_message_text(success_message)
            
            # Планируем удаление сообщения
            await schedule_message_deletion(
                context=context,
                chat_id=chat_id,
                message_id=query.message.message_id
            )
        else:
            error_message = f"❌ Ошибка: {result}"
            await query.edit_message_text(error_message)
            
            # Планируем удаление сообщения об ошибке
            await schedule_message_deletion(
                context=context,
                chat_id=chat_id,
                message_id=query.message.message_id
            )
        
        # Очистка данных
        for key in ['expense_amount', 'expense_description', 'expense_file_id', 
                   'all_participants', 'selected_participants']:
            if key in context.user_data:
                del context.user_data[key]
        
        return ConversationHandler.END
    
    # Handle transaction confirmation
    elif query.data.startswith("confirm_transaction_"):
        transaction_id = int(query.data.split("_")[-1])
        success, message = confirm_transaction(transaction_id)
        
        if success:
            # Отображаем сообщение об успешном подтверждении перевода
            await query.edit_message_text(
                "✅ Перевод подтвержден!"
            )
            
            # Планируем удаление сообщения через 5 минут
            await schedule_message_deletion(
                context=context,
                chat_id=query.message.chat_id,
                message_id=query.message.message_id
            )
        else:
            # Отображаем сообщение об ошибке при подтверждении перевода
            await query.edit_message_text(
                f"❌ Ошибка: {message}"
            )
            
            # Планируем удаление сообщения об ошибке
            await schedule_message_deletion(
                context=context,
                chat_id=query.message.chat_id,
                message_id=query.message.message_id
            )
    
    # Handle transaction rejection
    elif query.data.startswith("reject_transaction_"):
        transaction_id = int(query.data.split("_")[-1])
        success, message = reject_transaction(transaction_id)
        
        if success:
            # Отображаем сообщение об отклонении перевода
            await query.edit_message_text(
                "❌ Перевод отклонен."
            )
            
            # Планируем удаление сообщения через 5 минут
            await schedule_message_deletion(
                context=context,
                chat_id=query.message.chat_id,
                message_id=query.message.message_id
            )
        else:
            # Отображаем сообщение об ошибке при отклонении перевода
            await query.edit_message_text(
                f"❌ Ошибка: {message}"
            )
            
            # Планируем удаление сообщения об ошибке
            await schedule_message_deletion(
                context=context,
                chat_id=query.message.chat_id,
                message_id=query.message.message_id
            )
    
    # Обработка выбора получателя денег (send_to_ID)
    elif query.data.startswith("send_to_"):
        # Извлекаем ID получателя из данных колбэка
        receiver_id = int(query.data.split("_")[2])
        
        # Получаем информацию о получателе
        receiver = get_user(receiver_id)
        if not receiver:
            await query.edit_message_text(
                "❌ Ошибка: пользователь не найден."
            )
            return ConversationHandler.END
        
        # Формируем отображаемое имя
        receiver_name = ""
        if receiver.get('first_name') and receiver.get('last_name'):
            receiver_name = f"{receiver['first_name']} {receiver['last_name']}"
        elif receiver.get('username'):
            receiver_name = f"@{receiver['username']}"
        else:
            receiver_name = f"ID: {receiver_id}"
        
        # Сохраняем ID получателя в контексте
        context.user_data['send_receiver_id'] = receiver_id
        context.user_data['send_receiver_name'] = receiver_name
        
        # Запрашиваем сумму
        await query.edit_message_text(
            f"Сколько вы хотите отправить пользователю {receiver_name}? Введите сумму:"
        )
        
        # Устанавливаем флаг ожидания суммы
        context.user_data['waiting_for_send_amount'] = True
        
        return SEND_AMOUNT
    
    # Подтверждение отправки денег
    elif query.data == "send_confirm":
        # Получаем данные из контекста
        username = context.user_data.get('send_username')
        amount = context.user_data.get('send_amount')
        receiver_id = context.user_data.get('send_receiver_id')
        receiver_name = context.user_data.get('send_receiver_name')
        
        # Получаем инфо о пользователе и чате
        user = update.effective_user
        chat = update.effective_chat
        
        # Отмечаем операцию как завершенную
        await complete_pending_operation(user.id)
        
        # Если есть ID получателя (выбран из меню), используем его
        if receiver_id and amount:
            # Создаем транзакцию
            success, result = handle_money_transfer(
                chat.id, user.id, receiver_id, amount
            )
            
            if success:
                # Сообщение об успешном переводе
                await query.edit_message_text(
                    f"✅ Запрос на перевод {amount} руб. пользователю {receiver_name} отправлен. "
                    f"Ожидайте подтверждения от получателя."
                )
                
                # Планируем удаление сообщения через 5 минут
                await schedule_message_deletion(
                    context=context,
                    chat_id=chat.id,
                    message_id=query.message.message_id
                )
            else:
                # Сообщение об ошибке
                await query.edit_message_text(
                    f"❌ Ошибка: {result}"
                )
                
                # Планируем удаление сообщения об ошибке
                await schedule_message_deletion(
                    context=context,
                    chat_id=chat.id,
                    message_id=query.message.message_id
                )
        # Если есть имя пользователя (введено вручную), ищем его
        elif username and amount:
            # Находим пользователя по имени пользователя
            user_by_name = None
            members = get_group_members(chat.id)
            
            if members:
                for member in members:
                    if member.get('username') == username:
                        user_by_name = member
                        break
            
            if user_by_name:
                receiver_id = user_by_name['user_id']
                success, result = handle_money_transfer(
                    chat.id, user.id, receiver_id, amount
                )
                
                if success:
                    # Сообщение об успешном переводе
                    await query.edit_message_text(
                        f"✅ Запрос на перевод {amount} руб. пользователю @{username} отправлен. "
                        f"Ожидайте подтверждения от получателя."
                    )
                    
                    # Планируем удаление сообщения
                    await schedule_message_deletion(
                        context=context,
                        chat_id=chat.id,
                        message_id=query.message.message_id
                    )
                else:
                    # Сообщение об ошибке
                    await query.edit_message_text(
                        f"❌ Ошибка: {result}"
                    )
                    
                    # Планируем удаление сообщения об ошибке
                    await schedule_message_deletion(
                        context=context,
                        chat_id=chat.id,
                        message_id=query.message.message_id
                    )
            else:
                # Пользователь не найден
                await query.edit_message_text(
                    f"⚠️ Пользователь @{username} не найден в текущей группе. "
                    f"Проверьте правильность имени пользователя."
                )
                
                # Планируем удаление сообщения
                await schedule_message_deletion(
                    context=context,
                    chat_id=chat.id,
                    message_id=query.message.message_id
                )
        else:
            # Недостаточно данных
            await query.edit_message_text(
                "❌ Ошибка: недостаточно данных для перевода."
            )
            
            # Планируем удаление сообщения об ошибке
            await schedule_message_deletion(
                context=context,
                chat_id=chat.id,
                message_id=query.message.message_id
            )
        
        # Очищаем данные контекста
        context.user_data.pop('send_username', None)
        context.user_data.pop('send_amount', None)
        context.user_data.pop('send_receiver_id', None)
        context.user_data.pop('send_receiver_name', None)
        context.user_data.pop('waiting_for_send_amount', None)
        
        return ConversationHandler.END
    
    # Отмена отправки денег
    elif query.data == "send_cancel":
        # Сообщение об отмене операции
        await query.edit_message_text(
            "❌ Операция отменена."
        )
        
        # Планируем удаление сообщения об отмене
        await schedule_message_deletion(
            context=context,
            chat_id=chat.id,
            message_id=query.message.message_id
        )
        
        # Отмечаем операцию как завершенную
        await complete_pending_operation(update.effective_user.id)
        
        # Очищаем данные контекста
        context.user_data.pop('send_username', None)
        context.user_data.pop('send_amount', None)
        context.user_data.pop('send_receiver_id', None)
        context.user_data.pop('send_receiver_name', None)
        context.user_data.pop('waiting_for_send_amount', None)
        
        return ConversationHandler.END
    
    # Обработка настройки правил группы
    elif query.data == "setup_rules_yes":
        await query.edit_message_text(
            "Давайте настроим правила группы.\n\n"
            "Введите описание правил (например, 'Делим поровну'):"
        )
        # Сохраняем состояние для ожидания ввода описания правил
        context.user_data['waiting_for_rules_description'] = True
        
    elif query.data == "setup_rules_no":
        await query.edit_message_text(
            "Вы решили не настраивать правила. Вы всегда можете сделать это позже с помощью команды /rules."
        )
    
    # Обработка кнопок сброса данных группы
    elif query.data == "reset_confirm":
        # Обработка подтверждения сброса данных группы
        chat = update.effective_chat
        user = update.effective_user
        
        # Проверяем права администратора еще раз
        if not await is_admin(update, context):
            await query.edit_message_text(
                "❌ Только администраторы группы могут сбросить данные."
            )
            return ConversationHandler.END
        
        # Проверяем, может ли бот открепить сообщения (требуется для сброса закрепленных правил)
        try:
            # Проверяем права бота в чате
            bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
            can_pin = bot_member.can_pin_messages
            
            # Если у бота есть права на закрепление, пробуем найти и открепить закрепленные сообщения
            if can_pin:
                try:
                    # Получаем закрепленное сообщение
                    chat_info = await context.bot.get_chat(chat.id)
                    pinned_message = chat_info.pinned_message
                    
                    # Проверяем, является ли закрепленное сообщение сообщением с правилами от бота
                    if pinned_message and pinned_message.from_user.id == context.bot.id and "ПРАВИЛА ГРУППЫ" in pinned_message.text:
                        # Открепляем старое сообщение с правилами
                        await context.bot.unpin_chat_message(
                            chat_id=chat.id,
                            message_id=pinned_message.message_id
                        )
                        logger.info(f"Откреплено сообщение с правилами в группе {chat.id}")
                except Exception as e:
                    logger.error(f"Ошибка при откреплении сообщения: {e}")
        except Exception as e:
            logger.error(f"Ошибка при проверке прав бота: {e}")
            
        # Информируем пользователя о начале операции очистки
        await query.edit_message_text(
            "⏳ Начинаем очистку чата и сброс данных группы...\n\n"
            "Это может занять некоторое время. Пожалуйста, подождите."
        )
        
        # Проверяем, есть ли у бота права на удаление сообщений
        can_delete_messages = False
        try:
            bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
            can_delete_messages = bot_member.can_delete_messages
        except Exception as e:
            logger.error(f"Ошибка при проверке прав на удаление сообщений: {e}")
        
        # Если бот может удалять сообщения, удаляем все сообщения
        if can_delete_messages:
            # Сначала получаем последнее сообщение в чате
            try:
                # Попробуем удалить последние 1000 сообщений
                # Отправляем временное сообщение для получения последнего ID
                temp_message = await context.bot.send_message(
                    chat_id=chat.id,
                    text="Определение последнего ID сообщения..."
                )
                
                latest_message_id = temp_message.message_id
                await context.bot.delete_message(
                    chat_id=chat.id,
                    message_id=temp_message.message_id
                )
                
                # Удаляем все сообщения до текущего ID (кроме фото и видео)
                deleted_count = 0
                for msg_id in range(latest_message_id - 1000, latest_message_id):
                    try:
                        await context.bot.delete_message(
                            chat_id=chat.id,
                            message_id=msg_id
                        )
                        deleted_count += 1
                        # Делаем небольшую паузу, чтобы не перегрузить API Telegram
                        if deleted_count % 20 == 0:
                            await asyncio.sleep(0.5)
                    except Exception:
                        # Игнорируем ошибки - сообщения могут не существовать или быть фото/видео
                        pass
                
                logger.info(f"Удалено {deleted_count} сообщений в группе {chat.id}")
            except Exception as e:
                logger.error(f"Ошибка при удалении сообщений в группе {chat.id}: {e}")
        
        # Выполняем сброс данных в базе
        success = reset_group_data(chat.id)
        
        if success:
            # Добавляем лог о сбросе
            logger.info(f"Пользователь {user.id} (@{user.username}) сбросил данные группы {chat.id} ({chat.title})")
            
            # Отправляем новое сообщение (так как старое могло быть удалено)
            await context.bot.send_message(
                chat_id=chat.id,
                text="✅ Данные группы успешно сброшены.\n\n"
                     "Удалены: все расходы, долги, транзакции, правила и сообщения.\n"
                     "Пользователи сохранены в группе."
            )
            
            # Также обновляем сообщение с кнопкой (если оно еще существует)
            try:
                await query.edit_message_text(
                    "✅ Данные группы успешно сброшены.\n\n"
                    "Удалены: все расходы, долги, транзакции, правила и сообщения.\n"
                    "Пользователи сохранены в группе."
                )
            except Exception:
                pass
            
            logger.info(f"Пользователь {user.id} (@{user.username}) сбросил данные группы {chat.id} ({chat.title})")
        else:
            # Отправляем сообщение об ошибке
            await context.bot.send_message(
                chat_id=chat.id,
                text="❌ Произошла ошибка при сбросе данных группы.\n"
                     "Пожалуйста, попробуйте позже или обратитесь к разработчикам."
            )
            
            # Обновляем сообщение с кнопкой, если оно еще существует
            try:
                await query.edit_message_text(
                    "❌ Произошла ошибка при сбросе данных группы.\n"
                    "Пожалуйста, попробуйте позже или обратитесь к разработчикам."
                )
            except Exception:
                pass
    
    elif query.data == "reset_cancel":
        # Отмена сброса данных группы
        await query.edit_message_text(
            "❌ Сброс данных группы отменен."
        )
        
    # Обработка кнопок административного меню - этот блок активируется только для прямых admin_* действий, 
    # Обработка кнопок администратора
    elif query.data.startswith("admin_"):
        admin_action = query.data.split("_")[1]
        user = update.effective_user
        chat = update.effective_chat
        
        # Проверяем права администратора
        if not await is_admin(update, context):
            await query.edit_message_text(
                "❌ Только администраторы группы могут выполнять эти действия."
            )
            return ConversationHandler.END
            
        if admin_action == "edit_expenses":
            # Получаем список всех расходов группы
            expenses = get_group_expenses(chat.id)
            
            if not expenses:
                await query.edit_message_text(
                    "В этой группе еще нет расходов."
                )
                return ConversationHandler.END
                
            # Создаем кнопки для каждого расхода
            keyboard = []
            for expense in expenses[:10]:  # Ограничиваем до 10 последних расходов
                description = expense['description']
                amount = expense['amount']
                exp_id = expense['id']
                
                # Формируем текст кнопки
                button_text = f"{description} ({amount} руб.)"
                
                # Добавляем кнопку
                keyboard.append([InlineKeyboardButton(button_text, callback_data=f"edit_expense_{exp_id}")])
            
            # Добавляем кнопку назад
            keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                "Выберите расход для редактирования:",
                reply_markup=reply_markup
            )
            return ConversationHandler.END
            
        elif admin_action == "delete_expenses":
            # Получаем список всех расходов группы
            expenses = get_group_expenses(chat.id)
            
            if not expenses:
                await query.edit_message_text(
                    "В этой группе еще нет расходов."
                )
                return ConversationHandler.END
                
            # Создаем кнопки для каждого расхода
            keyboard = []
            for expense in expenses[:10]:  # Ограничиваем до 10 последних расходов
                description = expense['description']
                amount = expense['amount']
                exp_id = expense['id']
                
                # Формируем текст кнопки
                button_text = f"{description} ({amount} руб.)"
                
                # Добавляем кнопку
                keyboard.append([InlineKeyboardButton(button_text, callback_data=f"delete_expense_{exp_id}")])
            
            # Добавляем кнопку назад
            keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                "Выберите расход для удаления:",
                reply_markup=reply_markup
            )
            return ConversationHandler.END
            
        elif admin_action == "delete_transactions":
            # Получаем список всех транзакций группы
            transactions = get_group_transactions(chat.id)
            
            if not transactions:
                await query.edit_message_text(
                    "В этой группе еще нет транзакций."
                )
                return ConversationHandler.END
                
            # Создаем кнопки для каждой транзакции
            keyboard = []
            for tx in transactions[:10]:  # Ограничиваем до 10 последних транзакций
                sender_name = tx.get('sender_username', tx.get('sender_first_name', 'Неизвестно'))
                receiver_name = tx.get('receiver_username', tx.get('receiver_first_name', 'Неизвестно'))
                amount = tx['amount']
                tx_id = tx['id']
                
                # Формируем текст кнопки
                button_text = f"{sender_name} → {receiver_name} ({amount} руб.)"
                
                # Добавляем кнопку
                keyboard.append([InlineKeyboardButton(button_text, callback_data=f"delete_transaction_{tx_id}")])
            
            # Добавляем кнопку назад
            keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                "Выберите транзакцию для удаления:",
                reply_markup=reply_markup
            )
            return ConversationHandler.END
            
        elif admin_action == "reset":
            # Переадресуем на команду сброса с подтверждением
            keyboard = [
                [
                    InlineKeyboardButton("Да, подтверждаю", callback_data="reset_confirm"),
                    InlineKeyboardButton("Отмена", callback_data="reset_cancel")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                "⚠️ *ВНИМАНИЕ!* ⚠️\n\n"
                "Вы собираетесь сбросить *ВСЮ* историю группы.\n"
                "Это удалит все расходы, долги, транзакции и правила группы.\n"
                "Пользователи останутся в группе, но вся их финансовая история будет удалена.\n\n"
                "*Это действие необратимо.*\n\n"
                "Вы уверены, что хотите продолжить?",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return ConversationHandler.END
            
        elif admin_action == "back":
            # Возвращаемся к основному меню помощи
            help_text = (
                "*Команды бота:*\n\n"
                "Нажмите на кнопку ниже для выполнения соответствующей команды.\n\n"
                "*Как использовать:*\n"
                "1. Добавляйте расходы\n"
                "2. Проверяйте свой долг\n"
                "3. Отправляйте деньги участникам\n"
                "4. Получайте отчеты\n"
                "5. Администраторы имеют дополнительные функции"
            )
            
            # Создаем инлайн кнопки для всех команд
            keyboard = [
                [
                    InlineKeyboardButton("➕ Добавить расход", callback_data="help_addexpense"),
                    InlineKeyboardButton("💰 Мой долг", callback_data="help_mydebt")
                ],
                [
                    InlineKeyboardButton("📊 Отчет", callback_data="help_report"),
                    InlineKeyboardButton("💸 Отправить деньги", callback_data="help_send")
                ],
                [
                    InlineKeyboardButton("⚙️ Правила группы", callback_data="help_rules"),
                    InlineKeyboardButton("ℹ️ О боте", callback_data="help_about")
                ],
                [
                    InlineKeyboardButton("🔧 Администрирование", callback_data="help_admin")
                ]
            ]
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                text=help_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            return ConversationHandler.END
    
    # Обработка редактирования расхода
    elif query.data.startswith("edit_expense_"):
        expense_id = int(query.data.split("_")[-1])
        expense = get_expense_with_debts(expense_id)
        
        if not expense:
            await query.edit_message_text(
                "❌ Не удалось найти указанный расход."
            )
            return ConversationHandler.END
        
        # Сохраняем ID расхода в контексте
        context.user_data['edit_expense_id'] = expense_id
        context.user_data['edit_expense_description'] = expense['description']
        context.user_data['edit_expense_old_amount'] = expense['amount']
        
        # Спрашиваем новую сумму
        await query.edit_message_text(
            f"Редактирование расхода: {expense['description']}\n\n"
            f"Текущая сумма: {expense['amount']} руб.\n\n"
            f"Введите новую сумму расхода:"
        )
        
        # Сохраняем флаг для обработки следующего сообщения
        context.user_data['waiting_for_edit_expense_amount'] = True
        
        return EDIT_EXPENSE_AMOUNT
    
    # Обработка удаления расхода
    elif query.data.startswith("delete_expense_"):
        expense_id = int(query.data.split("_")[-1])
        expense = get_expense_with_debts(expense_id)
        
        if not expense:
            await query.edit_message_text(
                "❌ Не удалось найти указанный расход."
            )
            return ConversationHandler.END
        
        # Запрашиваем подтверждение
        keyboard = [
            [
                InlineKeyboardButton("Да, удалить", callback_data=f"confirm_delete_expense_{expense_id}"),
                InlineKeyboardButton("Отмена", callback_data="admin_back")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"⚠️ Вы уверены, что хотите удалить расход?\n\n"
            f"Описание: {expense['description']}\n"
            f"Сумма: {expense['amount']} руб.\n\n"
            f"Это действие удалит расход и связанные с ним долги!",
            reply_markup=reply_markup
        )
        return ConversationHandler.END
    
    # Обработка подтверждения удаления расхода
    elif query.data.startswith("confirm_delete_expense_"):
        expense_id = int(query.data.split("_")[-1])
        
        # Удаляем расход
        success, message = delete_expense(expense_id)
        
        if success:
            await query.edit_message_text(
                f"✅ {message}"
            )
        else:
            await query.edit_message_text(
                f"❌ {message}"
            )
        return ConversationHandler.END
    
    # Обработка удаления транзакции
    elif query.data.startswith("delete_transaction_"):
        transaction_id = int(query.data.split("_")[-1])
        
        # Запрашиваем подтверждение
        keyboard = [
            [
                InlineKeyboardButton("Да, удалить", callback_data=f"confirm_delete_transaction_{transaction_id}"),
                InlineKeyboardButton("Отмена", callback_data="admin_back")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"⚠️ Вы уверены, что хотите удалить эту транзакцию?\n\n"
            f"Это действие не может быть отменено!",
            reply_markup=reply_markup
        )
        return ConversationHandler.END
        
    # Обработка подтверждения удаления транзакции
    elif query.data.startswith("confirm_delete_transaction_"):
        transaction_id = int(query.data.split("_")[-1])
        
        # Удаляем транзакцию
        success, message = delete_transaction(transaction_id)
        
        if success:
            await query.edit_message_text(
                f"✅ {message}"
            )
        else:
            await query.edit_message_text(
                f"❌ {message}"
            )
        return ConversationHandler.END
    
    return ConversationHandler.END

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка загрузки фото чека."""
    if 'expense_amount' not in context.user_data or 'expense_description' not in context.user_data:
        await update.message.reply_text(
            "Сначала начните добавление расхода с команды /addexpense"
        )
        return ConversationHandler.END
    
    # Получаем ID файла фотографии
    photo_file_id = update.message.photo[-1].file_id
    
    # Сохраняем расход с фото
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    # Используем выбранных участников, если они есть
    participants = context.user_data.get('selected_participants')
    
    success, result = handle_new_expense(
        chat_id,
        context.user_data['expense_amount'],
        context.user_data['expense_description'],
        user.id,
        photo_file_id,
        participants
    )
    
    # Отмечаем операцию как завершенную
    await complete_pending_operation(user.id)
    
    if success:
        # Формируем сообщение об успехе с деталями
        success_message = (
            f"✅ Расход успешно добавлен: {context.user_data['expense_amount']} руб. "
            f"за {context.user_data['expense_description']} с фото чека"
        )
        
        # Если были выбраны участники, покажем их в сообщении
        if participants:
            participants_text = ""
            for user_id in participants:
                user = get_user(user_id)
                if user:
                    name = user.get('username', user.get('first_name', str(user_id)))
                    participants_text += f"@{name}, "
            
            if participants_text:
                success_message += f"\nУчастники: {participants_text[:-2]}"
        
        # Отправляем сообщение об успешном добавлении
        reply_message = await update.message.reply_text(success_message)
        
        # Планируем удаление сообщений
        await schedule_message_deletion(
            context=context,
            chat_id=chat_id,
            message_id=reply_message.message_id
        )
        
        await schedule_message_deletion(
            context=context,
            chat_id=chat_id,
            message_id=update.message.message_id
        )
    else:
        # Сообщение об ошибке
        error_message = f"❌ Ошибка: {result}"
        reply_message = await update.message.reply_text(error_message)
        
        # Планируем удаление сообщений
        await schedule_message_deletion(
            context=context,
            chat_id=chat_id,
            message_id=reply_message.message_id
        )
        
        await schedule_message_deletion(
            context=context,
            chat_id=chat_id,
            message_id=update.message.message_id
        )
    
    # Очистка данных
    for key in ['expense_amount', 'expense_description', 'expense_file_id', 
               'all_participants', 'selected_participants']:
        if key in context.user_data:
            del context.user_data[key]
    
    return ConversationHandler.END

async def my_debt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /mydebt command."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.message
    
    # Save user info
    save_user(user.id, user.username, user.first_name, user.last_name)
    
    # Check if in group chat
    if chat.type not in ['group', 'supergroup']:
        reply = await message.reply_text(
            "Эта команда работает только в группах."
        )
        # Планируем удаление сообщений
        await schedule_message_deletion(
            context=context,
            chat_id=chat.id,
            message_id=reply.message_id
        )
        await schedule_message_deletion(
            context=context,
            chat_id=chat.id,
            message_id=message.message_id
        )
        return
    
    # Get and format user's debt message
    debt_message = format_debt_message(user.id, chat.id)
    
    # Check for pending transactions
    pending_transactions = get_pending_transactions(user.id, as_receiver=True)
    
    # Список сообщений для планирования удаления
    messages_to_delete = []
    
    if pending_transactions:
        debt_message += "\n\n*У вас есть ожидающие подтверждения переводы:*\n"
        
        for transaction in pending_transactions:
            sender = get_user(transaction['sender_id'])
            sender_name = sender.get('username', sender.get('first_name', 'Unknown'))
            
            debt_message += (f"- {transaction['amount']:.2f} руб. от @{sender_name}\n")
            
            # Add confirmation buttons
            keyboard = [
                [
                    InlineKeyboardButton("Подтвердить получение", 
                                         callback_data=f"confirm_transaction_{transaction['id']}"),
                    InlineKeyboardButton("Отклонить", 
                                         callback_data=f"reject_transaction_{transaction['id']}"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            tx_message = await message.reply_text(
                f"Перевод от @{sender_name} на сумму {transaction['amount']:.2f} руб.",
                reply_markup=reply_markup
            )
            messages_to_delete.append(tx_message.message_id)
    
    # Отправляем основное сообщение с долгами
    debt_reply = await message.reply_markdown(debt_message)
    messages_to_delete.append(debt_reply.message_id)
    
    # Планируем удаление всех сообщений через 5 минут
    for msg_id in messages_to_delete:
        await schedule_message_deletion(
            context=context,
            chat_id=chat.id,
            message_id=msg_id
        )
    
    # Также планируем удаление команды от пользователя
    await schedule_message_deletion(
        context=context,
        chat_id=chat.id,
        message_id=message.message_id
    )

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /report command."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.message
    
    # Save user info
    save_user(user.id, user.username, user.first_name, user.last_name)
    
    # Список сообщений для планирования удаления
    messages_to_delete = []
    
    # Check if in group chat
    if chat.type not in ['group', 'supergroup']:
        reply = await message.reply_text(
            "Эта команда работает только в группах."
        )
        # Планируем удаление сообщений
        await schedule_message_deletion(
            context=context,
            chat_id=chat.id,
            message_id=reply.message_id
        )
        await schedule_message_deletion(
            context=context,
            chat_id=chat.id,
            message_id=message.message_id
        )
        return
    
    # Check if user is admin (only admins can generate reports)
    if not await is_admin(update, context):
        reply = await message.reply_text(
            "Только администраторы могут генерировать отчеты."
        )
        # Планируем удаление сообщений
        await schedule_message_deletion(
            context=context,
            chat_id=chat.id,
            message_id=reply.message_id
        )
        await schedule_message_deletion(
            context=context,
            chat_id=chat.id,
            message_id=message.message_id
        )
        return
    
    # Сообщение о процессе генерации - планируем удалить после завершения
    process_msg = await message.reply_text("Генерирую отчеты, пожалуйста подождите...")
    messages_to_delete.append(process_msg.message_id)
    
    # Добавляем сообщение о процессе для отладки
    logger.info(f"Начало генерации отчетов для группы {chat.id}")
    
    # Generate Excel report
    try:
        excel_report = generate_excel_report(chat.id)
        if excel_report:
            # Отправляем Excel отчет - отчеты НЕ планируем удалять автоматически
            await message.reply_document(
                document=excel_report,
                filename=f"expenses_report_{chat.id}.xlsx",
                caption="Отчет о расходах (Excel)"
            )
            logger.info(f"Excel отчет успешно создан и отправлен для группы {chat.id}")
        else:
            error_msg = await message.reply_text(
                "Не удалось создать Excel отчет. Проверьте логи для подробностей."
            )
            messages_to_delete.append(error_msg.message_id)
            logger.error(f"Ошибка при создании Excel отчета для группы {chat.id}: пустой результат")
    except Exception as e:
        error_msg = await message.reply_text(
            f"Ошибка при отправке Excel отчета: {str(e)[:100]}..."
        )
        messages_to_delete.append(error_msg.message_id)
        logger.error(f"Ошибка при отправке Excel отчета для группы {chat.id}: {e}")
        logger.exception(e)
    
    # Generate PDF report
    try:
        logger.info(f"Начало генерации PDF отчета для группы {chat.id}")
        pdf_report = generate_pdf_report(chat.id)
        
        if pdf_report:
            logger.info(f"PDF отчет создан, размер: {pdf_report.getbuffer().nbytes} байт")
            try:
                # Отправляем PDF отчет - отчеты НЕ планируем удалять автоматически
                await message.reply_document(
                    document=pdf_report,
                    filename=f"expenses_report_{chat.id}.pdf",
                    caption="Отчет о расходах (PDF)"
                )
                logger.info(f"PDF отчет успешно отправлен для группы {chat.id}")
            except Exception as send_err:
                error_msg = await message.reply_text(
                    f"PDF отчет был создан, но произошла ошибка при отправке: {str(send_err)[:100]}..."
                )
                messages_to_delete.append(error_msg.message_id)
                logger.error(f"Ошибка при отправке PDF для группы {chat.id}: {send_err}")
        else:
            error_msg = await message.reply_text(
                "Не удалось создать PDF отчет. Проверьте логи для подробностей."
            )
            messages_to_delete.append(error_msg.message_id)
            logger.error(f"Ошибка при создании PDF отчета для группы {chat.id}: пустой результат")
    except Exception as e:
        error_msg = await message.reply_text(
            f"Ошибка при создании PDF отчета: {str(e)[:100]}..."
        )
        messages_to_delete.append(error_msg.message_id)
        logger.error(f"Ошибка при создании PDF отчета для группы {chat.id}: {e}")
        logger.exception(e)
    
    # Планируем удаление всех информационных и ошибочных сообщений
    for msg_id in messages_to_delete:
        await schedule_message_deletion(
            context=context,
            chat_id=chat.id,
            message_id=msg_id
        )
    
    # Планируем удаление исходной команды
    await schedule_message_deletion(
        context=context,
        chat_id=chat.id,
        message_id=message.message_id
    )

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка новых участников группы - запрос на представление."""
    chat = update.effective_chat
    new_members = update.message.new_chat_members
    bot_id = context.bot.id
    
    # Проверяем, что это групповой чат
    if chat.type not in ['group', 'supergroup']:
        return ConversationHandler.END
    
    # Сохраняем информацию о группе
    save_group(chat.id, chat.title)
    
    # Обрабатываем каждого нового участника
    for member in new_members:
        # Пропускаем бота
        if member.id == bot_id:
            continue
            
        # Сохраняем базовую информацию о пользователе
        save_user(member.id, member.username, member.first_name, member.last_name)
        
        # Добавляем пользователя в группу
        add_user_to_group(chat.id, member.id)
        
        # Запрашиваем представление от всех новых пользователей, независимо от данных профиля
        # Приветствуем пользователя и запрашиваем его имя и фамилию
        user_mention = f"@{member.username}" if member.username else ""
        await update.message.reply_text(
            f"Добро пожаловать в группу, {user_mention}! "
            f"Для полного доступа к функциям бота, пожалуйста, представьтесь.\n\n"
            f"Введите ваше имя:",
            reply_to_message_id=update.message.message_id
        )
        
        # Сохраняем id пользователя, которого нужно представить
        context.user_data['intro_user_id'] = member.id
        context.user_data['waiting_for_name'] = True
        
        # Создаем обработчик диалога
        user_intro_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, user_intro_name_step)],
            states={
                USER_INTRO_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_intro_name_step)],
                USER_INTRO_LASTNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_intro_lastname_step)],
            },
            fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
            per_chat=True,
            per_user=True,
            name="user_intro_conversation"
        )
        
        # Добавляем обработчик
        context.application.add_handler(user_intro_handler)
        
        return USER_INTRO_NAME
    
    return ConversationHandler.END

async def user_intro_name_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка ввода имени при представлении нового участника."""
    # Сохраняем имя
    name = update.message.text.strip()
    context.user_data['intro_name'] = name
    context.user_data['waiting_for_name'] = False
    context.user_data['waiting_for_lastname'] = True
    
    await update.message.reply_text(
        f"Спасибо, {name}! Теперь введите вашу фамилию:"
    )
    
    return USER_INTRO_LASTNAME

async def user_intro_lastname_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка ввода фамилии при представлении нового участника."""
    # Сохраняем фамилию
    lastname = update.message.text.strip()
    name = context.user_data.get('intro_name', '')
    user_id = context.user_data.get('intro_user_id')
    
    if user_id:
        # Обновляем информацию о пользователе, сохраняя имя и фамилию
        # Не обновляем username, оставляя None, чтобы не затереть существующее значение
        save_user(user_id, None, name, lastname)
        
        await update.message.reply_text(
            f"Спасибо за представление, {name} {lastname}! "
            f"Теперь вы полноправный участник группы и можете пользоваться всеми функциями бота. "
            f"Отправьте /help для получения списка доступных команд."
        )
    else:
        # Если по какой-то причине user_id не сохранился
        await update.message.reply_text(
            f"Спасибо за представление, {name} {lastname}! "
            f"Теперь вы полноправный участник группы и можете пользоваться всеми функциями бота. "
            f"Отправьте /help для получения списка доступных команд."
        )
    
    # Очищаем данные
    context.user_data.pop('waiting_for_lastname', None)
    context.user_data.pop('waiting_for_name', None)
    context.user_data.pop('intro_name', None)
    context.user_data.pop('intro_user_id', None)
    
    return ConversationHandler.END

async def send_money(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка команды /send для отправки денег другому пользователю."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.message
    
    # Регистрируем незавершенную операцию
    await register_pending_operation(
        user_id=user.id,
        operation_type="send_money",
        chat_id=chat.id,
        message_id=message.message_id
    )
    
    # Сохраняем информацию о пользователе
    save_user(user.id, user.username, user.first_name, user.last_name)
    
    # Проверяем, что команда вызвана в групповом чате
    if chat.type not in ['group', 'supergroup']:
        reply = await message.reply_text(
            "Эта команда работает только в группах."
        )
        
        # Планируем удаление сообщений
        await schedule_message_deletion(
            context=context,
            chat_id=chat.id,
            message_id=reply.message_id
        )
        
        await schedule_message_deletion(
            context=context,
            chat_id=chat.id,
            message_id=message.message_id
        )
        
        # Отмечаем операцию как завершенную
        await complete_pending_operation(user.id)
        
        return ConversationHandler.END
        
    # Добавляем текущего пользователя в группу и сохраняем группу
    logger.info(f"Saving group {chat.id} ({chat.title}) and adding user {user.id}")
    save_group(chat.id, chat.title)
    add_user_to_group(chat.id, user.id)
    
    # Добавляем всех видимых участников чата в группу
    chat_members = await context.bot.get_chat_administrators(chat.id)
    logger.info(f"Found {len(chat_members)} admins in chat {chat.id}")
    
    for member in chat_members:
        member_user = member.user
        logger.info(f"Adding admin {member_user.id} (@{member_user.username}) to group {chat.id}")
        save_user(member_user.id, member_user.username, member_user.first_name, member_user.last_name)
        add_user_to_group(chat.id, member_user.id)
    
    # Разбираем аргументы команды, если они предоставлены
    if context.args and len(context.args) >= 2:
        username, amount = extract_username_and_amount(context.args)
        
        if username and amount:
            # Сохраняем данные для шага подтверждения
            context.user_data['send_username'] = username
            context.user_data['send_amount'] = amount
            
            # Создаем кнопки для подтверждения или отмены перевода
            keyboard = [
                [
                    InlineKeyboardButton("Подтвердить", callback_data="send_confirm"),
                    InlineKeyboardButton("Отменить", callback_data="send_cancel"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"Вы собираетесь отправить {amount} руб. пользователю @{username}. "
                f"Подтвердите операцию:",
                reply_markup=reply_markup
            )
            
            return SEND_CONFIRM
    
    # Получаем список участников группы для выбора
    logger.info(f"Getting group members in send_money command. Chat ID: {chat.id}")
    members = get_group_members(chat.id)
    logger.info(f"Found {len(members) if members else 0} members for chat {chat.id} in send_money: {members}")
    
    if members and len(members) > 1:
        # Создаем кнопки для каждого участника
        keyboard = []
        for member in members:
            # Пропускаем текущего пользователя
            if member['user_id'] == user.id:
                continue
                
            # Формируем отображаемое имя
            first_name = member.get('first_name', '')
            last_name = member.get('last_name', '')
            username = member.get('username', '')
            user_id = member['user_id']
            
            # Создаем текст кнопки (ID + имя/юзернейм)
            if first_name and last_name:
                display_name = f"{first_name} {last_name}"
            elif username:
                display_name = f"@{username}"
            else:
                display_name = f"ID: {user_id}"
                
            # Добавляем кнопку
            keyboard.append([InlineKeyboardButton(
                display_name, 
                callback_data=f"send_to_{user_id}"
            )])
        
        # Добавляем кнопку отмены
        keyboard.append([InlineKeyboardButton("Отмена", callback_data="send_cancel")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "Выберите пользователя, которому хотите отправить деньги:",
            reply_markup=reply_markup
        )
        
        return SEND_AMOUNT
    else:
        # Если участников мало или их нет, используем стандартный ввод
        await update.message.reply_text(
            "Кому вы хотите отправить деньги? Введите @username:"
        )
        context.user_data['waiting_for_send_username'] = True
        
        return SEND_AMOUNT

async def send_amount_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка ввода имени пользователя при отправке денег."""
    message = update.message
    chat_id = update.effective_chat.id
    username = message.text.strip()
    
    # Извлекаем имя пользователя без символа @
    if username.startswith('@'):
        username = username[1:]
    
    context.user_data['send_username'] = username
    
    # Отправляем сообщение с запросом суммы
    reply = await message.reply_text(
        f"Сколько вы хотите отправить пользователю @{username}? Введите сумму:"
    )
    
    # Планируем удаление введенного пользователем имени
    await schedule_message_deletion(
        context=context,
        chat_id=chat_id,
        message_id=message.message_id
    )
    
    # Запрос суммы будет удален, когда пользователь введет сумму
    await schedule_message_deletion(
        context=context,
        chat_id=chat_id,
        message_id=reply.message_id,
        user_id=update.effective_user.id,
        operation_type="send_amount",
        extend_if_pending=True
    )
    
    return SEND_CONFIRM

async def send_confirm_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка ввода суммы и подтверждения для отправки денег."""
    message = update.message
    chat_id = update.effective_chat.id
    user = update.effective_user
    
    try:
        amount = float(message.text.replace(',', '.'))
        if amount <= 0:
            error_msg = await message.reply_text(
                "Сумма должна быть положительным числом. Попробуйте снова:"
            )
            
            # Планируем удаление сообщения об ошибке и введенной пользователем суммы
            await schedule_message_deletion(
                context=context,
                chat_id=chat_id,
                message_id=error_msg.message_id
            )
            await schedule_message_deletion(
                context=context,
                chat_id=chat_id,
                message_id=message.message_id
            )
            
            return SEND_CONFIRM
        
        context.user_data['send_amount'] = amount
        username = context.user_data['send_username']
        
        # Отмечаем операцию как завершенную
        await complete_pending_operation(user.id)
        
        # Создание транзакции
        # Находим пользователя по имени пользователя
        user_by_name = None
        members = get_group_members(chat_id)
        
        if members:
            for member in members:
                if member.get('username') == username:
                    user_by_name = member
                    break
        
        if user_by_name:
            receiver_id = user_by_name['user_id']
            success, result = handle_money_transfer(
                chat_id, user.id, receiver_id, amount
            )
            
            if success:
                success_msg = await message.reply_text(
                    f"✅ Запрос на перевод {amount} руб. пользователю @{username} отправлен. "
                    f"Ожидайте подтверждения от получателя."
                )
                
                # Планируем удаление сообщений
                await schedule_message_deletion(
                    context=context,
                    chat_id=chat_id,
                    message_id=success_msg.message_id
                )
                await schedule_message_deletion(
                    context=context,
                    chat_id=chat_id,
                    message_id=message.message_id
                )
            else:
                error_msg = await message.reply_text(
                    f"❌ Ошибка: {result}"
                )
                
                # Планируем удаление сообщений
                await schedule_message_deletion(
                    context=context,
                    chat_id=chat_id,
                    message_id=error_msg.message_id
                )
                await schedule_message_deletion(
                    context=context,
                    chat_id=chat_id,
                    message_id=message.message_id
                )
        else:
            # Пользователь не найден, просто показываем подтверждение
            not_found_msg = await message.reply_text(
                f"⚠️ Пользователь @{username} не найден в текущей группе, "
                f"но запрос на перевод {amount} руб. отправлен. "
                f"Обратитесь к пользователю для завершения транзакции."
            )
            
            # Планируем удаление сообщений
            await schedule_message_deletion(
                context=context,
                chat_id=chat_id,
                message_id=not_found_msg.message_id
            )
            await schedule_message_deletion(
                context=context,
                chat_id=chat_id,
                message_id=message.message_id
            )
        
        # Очистка данных пользователя
        context.user_data.pop('send_username', None)
        context.user_data.pop('send_amount', None)
        context.user_data.pop('send_receiver_id', None)
        context.user_data.pop('send_receiver_name', None)
        context.user_data.pop('waiting_for_send_amount', None)
        context.user_data.pop('waiting_for_send_username', None)
        
        return ConversationHandler.END
    except ValueError:
        error_msg = await message.reply_text(
            "Неверный формат суммы. Введите число:"
        )
        
        # Планируем удаление сообщений
        await schedule_message_deletion(
            context=context,
            chat_id=chat_id,
            message_id=error_msg.message_id
        )
        await schedule_message_deletion(
            context=context,
            chat_id=chat_id,
            message_id=message.message_id
        )
        
        return SEND_CONFIRM

# Настройка обработчиков диалога
rules_conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("rules", rules)],
    states={
        RULES_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, rules_description)],
        RULES_DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, rules_deadline)],
        RULES_NOTIFICATIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, rules_notifications)],
    },
    fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    per_chat=True,
    per_user=True,
    per_message=False
)

expense_conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("addexpense", add_expense)],
    states={
        EXPENSE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, expense_amount)],
        EXPENSE_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, expense_description)],
        EXPENSE_PARTICIPANTS: [],  # Обрабатывается глобальными обработчиками в main.py
        EXPENSE_PHOTO: [MessageHandler(filters.PHOTO, photo_handler)],  # Только обработка фото
        EDIT_EXPENSE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, 
                                           lambda update, context: handle_pending_state(update, context))],
    },
    fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    per_chat=True,
    per_user=True,
    name="expense_conversation"  # Для отладки
)

send_conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("send", send_money)],
    states={
        SEND_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_amount_step)],
        SEND_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_confirm_step)],
    },
    fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    per_chat=True,
    per_user=True,
    name="send_conversation"  # Для отладки
)
