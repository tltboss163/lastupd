import datetime
import logging
from db_manager import (add_expense, get_group_members, get_user_debt_summary, 
                       create_transaction, update_transaction_status, 
                       get_transaction, get_user_debts)

# Configure logging
logger = logging.getLogger(__name__)

def handle_new_expense(group_id, amount, description, admin_id, file_id=None, participants=None):
    """Обрабатывает создание нового расхода и рассчитывает долги."""
    try:
        # Проверка входных данных
        amount = float(amount)
        if amount <= 0:
            return False, "Сумма должна быть больше нуля"
        
        # Добавление расхода в базу данных
        expense_id = add_expense(group_id, amount, description, admin_id, file_id, participants)
        
        if expense_id:
            return True, expense_id
        else:
            return False, "Не удалось добавить расход"
    except ValueError:
        return False, "Неверный формат суммы"
    except Exception as e:
        logger.error(f"Ошибка при обработке нового расхода: {e}")
        return False, f"Ошибка: {str(e)}"

def calculate_individual_debt(amount, group_id, admin_id):
    """Рассчитывает, сколько каждый участник должен за расход."""
    try:
        # Получаем всех участников группы (кроме ботов)
        members = get_group_members(group_id, exclude_bots=True)
        
        # Исключаем создателя расхода из расчетов, если он в группе
        members = [m for m in members if m['user_id'] != admin_id]
        
        if not members:
            return 0, []
        
        # Рассчитываем индивидуальную сумму
        individual_amount = amount / len(members)
        
        return individual_amount, [m['user_id'] for m in members]
    except Exception as e:
        logger.error(f"Ошибка при расчете индивидуального долга: {e}")
        return 0, []

def get_user_total_debt(user_id, group_id):
    """Получает общую сумму долга пользователя в определенной группе."""
    try:
        return get_user_debt_summary(user_id, group_id)
    except Exception as e:
        logger.error(f"Ошибка при получении общего долга пользователя: {e}")
        return 0

def get_user_detailed_debts(user_id, group_id):
    """Получает детализированный список долгов пользователя в группе."""
    try:
        debts = get_user_debts(user_id, group_id)
        return debts
    except Exception as e:
        logger.error(f"Ошибка при получении детализации долгов пользователя: {e}")
        return []

def handle_money_transfer(group_id, sender_id, receiver_id, amount):
    """Обрабатывает перевод денег между пользователями."""
    try:
        # Проверка входных данных
        amount = float(amount)
        if amount <= 0:
            return False, "Сумма должна быть больше нуля"
        
        # Создаем ожидающую транзакцию
        transaction_id = create_transaction(group_id, sender_id, receiver_id, amount)
        
        if transaction_id:
            return True, transaction_id
        else:
            return False, "Не удалось создать транзакцию"
    except ValueError:
        return False, "Неверный формат суммы"
    except Exception as e:
        logger.error(f"Ошибка при обработке перевода денег: {e}")
        return False, f"Ошибка: {str(e)}"

def confirm_transaction(transaction_id):
    """Подтверждает транзакцию и обновляет долги."""
    try:
        # Обновляем статус транзакции на "подтверждено"
        success = update_transaction_status(transaction_id, 'confirmed')
        
        if success:
            # Обновление долгов происходит в функции update_transaction_status
            return True, "Транзакция подтверждена"
        else:
            return False, "Не удалось подтвердить транзакцию"
    except Exception as e:
        logger.error(f"Ошибка при подтверждении транзакции: {e}")
        return False, f"Ошибка: {str(e)}"

def reject_transaction(transaction_id):
    """Отклоняет транзакцию."""
    try:
        # Обновляем статус транзакции на "отклонено"
        success = update_transaction_status(transaction_id, 'rejected')
        
        if success:
            return True, "Транзакция отклонена"
        else:
            return False, "Не удалось отклонить транзакцию"
    except Exception as e:
        logger.error(f"Ошибка при отклонении транзакции: {e}")
        return False, f"Ошибка: {str(e)}"

def format_debt_message(user_id, group_id):
    """Форматирует сообщение с информацией о долгах пользователя."""
    total_debt = get_user_total_debt(user_id, group_id)
    detailed_debts = get_user_detailed_debts(user_id, group_id)
    
    if total_debt <= 0 and not detailed_debts:
        return "У вас нет долгов в этой группе! 🎉"
    
    message = f"💰 *Ваш общий долг:* {total_debt:.2f} руб.\n\n"
    
    if detailed_debts:
        message += "*Детали по расходам:*\n"
        for debt in detailed_debts:
            date_str = datetime.datetime.fromisoformat(debt['date']).strftime('%d.%m.%Y')
            message += f"- {debt['description']}: {debt['amount']:.2f} руб. ({date_str})\n"
    
    return message
