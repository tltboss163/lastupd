import sqlite3
import json
import datetime
from sqlite3 import Error
import logging
import os

# Настройка логирования
logger = logging.getLogger(__name__)

# Файл базы данных
DB_FILE = "./expense_tracker.db"

def init_db():
    """Инициализация базы данных с необходимыми таблицами, если они не существуют."""
    # Используем корневую директорию для файла базы данных
    
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Create users table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            joined_date TIMESTAMP
        )
        ''')
        
        # Create groups table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            group_id INTEGER PRIMARY KEY,
            title TEXT,
            created_date TIMESTAMP
        )
        ''')
        
        # Create group_members table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS group_members (
            group_id INTEGER,
            user_id INTEGER,
            joined_date TIMESTAMP,
            PRIMARY KEY (group_id, user_id),
            FOREIGN KEY (group_id) REFERENCES groups (group_id),
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
        ''')
        
        # Создание таблицы расходов
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER,
            amount REAL,
            description TEXT,
            date TIMESTAMP,
            admin_id INTEGER,
            file_id TEXT,
            participants TEXT,
            FOREIGN KEY (group_id) REFERENCES groups (group_id),
            FOREIGN KEY (admin_id) REFERENCES users (user_id)
        )
        ''')
        
        # Создание таблицы долгов
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS debts (
            user_id INTEGER,
            expense_id INTEGER,
            amount REAL,
            status TEXT,
            PRIMARY KEY (user_id, expense_id),
            FOREIGN KEY (user_id) REFERENCES users (user_id),
            FOREIGN KEY (expense_id) REFERENCES expenses (id)
        )
        ''')
        
        # Создание таблицы транзакций
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER,
            sender_id INTEGER,
            receiver_id INTEGER,
            amount REAL,
            timestamp TIMESTAMP,
            status TEXT,
            FOREIGN KEY (group_id) REFERENCES groups (group_id),
            FOREIGN KEY (sender_id) REFERENCES users (user_id),
            FOREIGN KEY (receiver_id) REFERENCES users (user_id)
        )
        ''')
        
        # Создание таблицы правил
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS rules (
            group_id INTEGER PRIMARY KEY,
            description TEXT,
            deadline_hours INTEGER,
            notifications_time TEXT,
            FOREIGN KEY (group_id) REFERENCES groups (group_id)
        )
        ''')
        
        conn.commit()
        logger.info("Database initialized successfully.")
    except Error as e:
        logger.error(f"Error initializing database: {e}")
    finally:
        if conn:
            conn.close()

def get_connection():
    """Create a database connection and return it."""
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row  # This enables dictionary-like access to rows
        return conn
    except Error as e:
        logger.error(f"Error connecting to database: {e}")
        return None

# User-related functions
def save_user(user_id, username, first_name, last_name):
    """Save user to database if not exists, otherwise update."""
    conn = get_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        # Check if user exists
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        if cursor.fetchone():
            # Update existing user
            cursor.execute("""
                UPDATE users 
                SET username = ?, first_name = ?, last_name = ? 
                WHERE user_id = ?
            """, (username, first_name, last_name, user_id))
        else:
            # Insert new user
            cursor.execute("""
                INSERT INTO users (user_id, username, first_name, last_name, joined_date) 
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, username, first_name, last_name, datetime.datetime.now()))
        
        conn.commit()
        return True
    except Error as e:
        logger.error(f"Error saving user: {e}")
        return False
    finally:
        conn.close()

def get_user(user_id):
    """Get user from database by user_id."""
    conn = get_connection()
    if not conn:
        return None
    
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    except Error as e:
        logger.error(f"Error getting user: {e}")
        return None
    finally:
        conn.close()

# Group-related functions
def save_group(group_id, title):
    """Save group to database if not exists, otherwise update."""
    conn = get_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        # Check if group exists
        cursor.execute("SELECT * FROM groups WHERE group_id = ?", (group_id,))
        if cursor.fetchone():
            # Update existing group
            cursor.execute("""
                UPDATE groups 
                SET title = ? 
                WHERE group_id = ?
            """, (title, group_id))
        else:
            # Insert new group
            cursor.execute("""
                INSERT INTO groups (group_id, title, created_date) 
                VALUES (?, ?, ?)
            """, (group_id, title, datetime.datetime.now()))
        
        conn.commit()
        return True
    except Error as e:
        logger.error(f"Error saving group: {e}")
        return False
    finally:
        conn.close()

def add_user_to_group(group_id, user_id):
    """Add user to group if not already a member."""
    conn = get_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        # Check if user is already in group
        cursor.execute("SELECT * FROM group_members WHERE group_id = ? AND user_id = ?", 
                      (group_id, user_id))
        if not cursor.fetchone():
            # Add user to group
            cursor.execute("""
                INSERT INTO group_members (group_id, user_id, joined_date) 
                VALUES (?, ?, ?)
            """, (group_id, user_id, datetime.datetime.now()))
            
            conn.commit()
        return True
    except Error as e:
        logger.error(f"Error adding user to group: {e}")
        return False
    finally:
        conn.close()

def get_group_members(group_id, exclude_bots=False):
    """Получение всех участников группы.
    
    Args:
        group_id: ID группы
        exclude_bots: если True, исключить ботов из результата
    """
    conn = get_connection()
    if not conn:
        logger.error(f"Не удалось подключиться к базе данных при получении участников группы")
        return []
    
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.* FROM users u
            JOIN group_members gm ON u.user_id = gm.user_id
            WHERE gm.group_id = ?
        """, (group_id,))
        rows = cursor.fetchall()
        members = [dict(row) for row in rows]
        
        # Фильтрация ботов если требуется
        if exclude_bots:
            # Исключаем пользователей с именем, содержащим "bot" или "_bot"
            members = [m for m in members if not (
                (m.get('username') and ('bot' in m.get('username').lower())) or
                (m.get('first_name') and ('bot' in m.get('first_name').lower()))
            )]
        
        logger.info(f"Группа {group_id} имеет {len(members)} участников: {members}")
        return members
    except Error as e:
        logger.error(f"Ошибка при получении участников группы: {e}")
        return []
    finally:
        conn.close()

# Expense-related functions
def add_expense(group_id, amount, description, admin_id, file_id=None, participants=None):
    """Добавляет новый расход в базу данных."""
    conn = get_connection()
    if not conn:
        return None
    
    try:
        cursor = conn.cursor()
        
        # Если участники не указаны, получаем всех участников группы (кроме ботов)
        if participants is None:
            # Получаем всех участников группы, исключая ботов
            members = get_group_members(group_id, exclude_bots=True)
            participants = [m['user_id'] for m in members]
        else:
            # Если список участников указан явно, проверяем, есть ли среди них боты
            # Получаем информацию о пользователях, чтобы проверить, являются ли они ботами
            filtered_participants = []
            for user_id in participants:
                cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                user = cursor.fetchone()
                if user:
                    user_dict = dict(user)
                    # Проверяем, является ли пользователь ботом
                    is_bot = False
                    username = user_dict.get('username')
                    first_name = user_dict.get('first_name')
                    
                    if username and 'bot' in username.lower():
                        is_bot = True
                    if first_name and 'bot' in first_name.lower():
                        is_bot = True
                    
                    if not is_bot:
                        filtered_participants.append(user_id)
            participants = filtered_participants
        
        # Исключаем создателя расхода из списка участников, если он там есть
        if admin_id in participants:
            participants.remove(admin_id)
        
        # Добавляем расход
        cursor.execute("""
            INSERT INTO expenses (group_id, amount, description, date, admin_id, file_id, participants) 
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (group_id, amount, description, datetime.datetime.now(), 
              admin_id, file_id, json.dumps(participants)))
        
        expense_id = cursor.lastrowid
        
        # Рассчитываем индивидуальную сумму долга
        if participants:
            individual_amount = amount / len(participants)
            
            # Добавляем долги для всех участников
            for user_id in participants:
                cursor.execute("""
                    INSERT INTO debts (user_id, expense_id, amount, status) 
                    VALUES (?, ?, ?, ?)
                """, (user_id, expense_id, individual_amount, 'unpaid'))
        
        conn.commit()
        return expense_id
    except Error as e:
        logger.error(f"Ошибка при добавлении расхода: {e}")
        return None
    finally:
        conn.close()

def get_expense(expense_id):
    """Get expense details by ID."""
    conn = get_connection()
    if not conn:
        return None
    
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,))
        row = cursor.fetchone()
        if row:
            expense = dict(row)
            expense['participants'] = json.loads(expense['participants'])
            return expense
        return None
    except Error as e:
        logger.error(f"Error getting expense: {e}")
        return None
    finally:
        conn.close()

def get_group_expenses(group_id, start_date=None, end_date=None):
    """Get all expenses for a group, optionally filtered by date range."""
    conn = get_connection()
    if not conn:
        return []
    
    try:
        cursor = conn.cursor()
        query = "SELECT * FROM expenses WHERE group_id = ?"
        params = [group_id]
        
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        
        query += " ORDER BY date DESC"
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        expenses = []
        for row in rows:
            expense = dict(row)
            expense['participants'] = json.loads(expense['participants'])
            expenses.append(expense)
        
        return expenses
    except Error as e:
        logger.error(f"Error getting group expenses: {e}")
        return []
    finally:
        conn.close()

def get_user_debts(user_id, group_id=None):
    """Get all debts for a user, optionally filtered by group."""
    conn = get_connection()
    if not conn:
        return []
    
    try:
        cursor = conn.cursor()
        
        if group_id:
            cursor.execute("""
                SELECT d.*, e.description, e.date, e.group_id 
                FROM debts d
                JOIN expenses e ON d.expense_id = e.id
                WHERE d.user_id = ? AND e.group_id = ? AND d.status = 'unpaid'
                ORDER BY e.date DESC
            """, (user_id, group_id))
        else:
            cursor.execute("""
                SELECT d.*, e.description, e.date, e.group_id 
                FROM debts d
                JOIN expenses e ON d.expense_id = e.id
                WHERE d.user_id = ? AND d.status = 'unpaid'
                ORDER BY e.date DESC
            """, (user_id,))
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    except Error as e:
        logger.error(f"Error getting user debts: {e}")
        return []
    finally:
        conn.close()

def get_user_debt_summary(user_id, group_id):
    """Get summary of user's debts for a specific group."""
    conn = get_connection()
    if not conn:
        return 0.0
    
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT SUM(d.amount) as total_debt
            FROM debts d
            JOIN expenses e ON d.expense_id = e.id
            WHERE d.user_id = ? AND e.group_id = ? AND d.status = 'unpaid'
        """, (user_id, group_id))
        
        result = cursor.fetchone()
        return float(result['total_debt']) if result and result['total_debt'] else 0.0
    except Error as e:
        logger.error(f"Error getting user debt summary: {e}")
        return 0.0
    finally:
        conn.close()

# Transaction-related functions
def create_transaction(group_id, sender_id, receiver_id, amount):
    """Create a new pending transaction."""
    conn = get_connection()
    if not conn:
        return None
    
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO transactions (group_id, sender_id, receiver_id, amount, timestamp, status) 
            VALUES (?, ?, ?, ?, ?, ?)
        """, (group_id, sender_id, receiver_id, amount, datetime.datetime.now(), 'pending'))
        
        transaction_id = cursor.lastrowid
        conn.commit()
        return transaction_id
    except Error as e:
        logger.error(f"Error creating transaction: {e}")
        return None
    finally:
        conn.close()

def get_transaction(transaction_id):
    """Get transaction details by ID."""
    conn = get_connection()
    if not conn:
        return None
    
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    except Error as e:
        logger.error(f"Error getting transaction: {e}")
        return None
    finally:
        conn.close()

def update_transaction_status(transaction_id, status):
    """Update transaction status (pending, confirmed, rejected)."""
    conn = get_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE transactions 
            SET status = ? 
            WHERE id = ?
        """, (status, transaction_id))
        conn.commit()
        
        # If transaction is confirmed, update debts
        if status == 'confirmed':
            # Get transaction details
            transaction = get_transaction(transaction_id)
            if transaction:
                # Get receiver's debts to pay off with this amount
                debts = get_user_debts(transaction['receiver_id'], transaction['group_id'])
                # Сортируем долги по дате создания (сначала старые)
                debts.sort(key=lambda x: x.get('date', ''), reverse=False)
                remaining_amount = transaction['amount']
                
                # Логируем для отладки
                logger.info(f"Обработка транзакции {transaction_id}: сумма {remaining_amount}, получатель {transaction['receiver_id']}")
                logger.info(f"Найдено {len(debts)} неоплаченных долгов на общую сумму: {sum([d['amount'] for d in debts])}")
                
                # Pay off debts until the amount is used up
                for debt in debts:
                    if remaining_amount <= 0:
                        break
                    
                    logger.info(f"Обрабатываем долг {debt['id']}: {debt['amount']} руб. за {debt.get('description', 'без описания')}")
                    
                    if remaining_amount >= debt['amount']:
                        # Pay off entire debt
                        cursor.execute("""
                            UPDATE debts 
                            SET status = 'paid' 
                            WHERE user_id = ? AND expense_id = ? AND status = 'unpaid'
                        """, (transaction['receiver_id'], debt['expense_id']))
                        remaining_amount -= debt['amount']
                    else:
                        # Pay off part of debt
                        new_amount = debt['amount'] - remaining_amount
                        cursor.execute("""
                            UPDATE debts 
                            SET amount = ? 
                            WHERE user_id = ? AND expense_id = ? AND status = 'unpaid'
                        """, (new_amount, transaction['receiver_id'], debt['expense_id']))
                        remaining_amount = 0
                
                conn.commit()
        
        return True
    except Error as e:
        logger.error(f"Error updating transaction status: {e}")
        return False
    finally:
        conn.close()

def get_pending_transactions(user_id, as_receiver=True):
    """Get pending transactions for a user."""
    conn = get_connection()
    if not conn:
        return []
    
    try:
        cursor = conn.cursor()
        if as_receiver:
            cursor.execute("""
                SELECT t.*, u.username as sender_username, u.first_name as sender_first_name
                FROM transactions t
                JOIN users u ON t.sender_id = u.user_id
                WHERE t.receiver_id = ? AND t.status = 'pending'
                ORDER BY t.timestamp DESC
            """, (user_id,))
        else:
            cursor.execute("""
                SELECT t.*, u.username as receiver_username, u.first_name as receiver_first_name
                FROM transactions t
                JOIN users u ON t.receiver_id = u.user_id
                WHERE t.sender_id = ? AND t.status = 'pending'
                ORDER BY t.timestamp DESC
            """, (user_id,))
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    except Error as e:
        logger.error(f"Error getting pending transactions: {e}")
        return []
    finally:
        conn.close()

# Функции для управления расходами

def get_expense_with_debts(expense_id):
    """Получает детали расхода вместе со всеми связанными долгами."""
    conn = get_connection()
    if not conn:
        return None
    
    try:
        cursor = conn.cursor()
        
        # Получаем данные о расходе
        cursor.execute("""
            SELECT e.*, u.username as admin_username, u.first_name as admin_first_name, u.last_name as admin_last_name
            FROM expenses e
            LEFT JOIN users u ON e.admin_id = u.user_id
            WHERE e.id = ?
        """, (expense_id,))
        
        expense = cursor.fetchone()
        if not expense:
            return None
        
        expense_dict = dict(expense)
        expense_dict['participants'] = json.loads(expense_dict['participants'])
        
        # Получаем все связанные долги
        cursor.execute("""
            SELECT d.*, u.username, u.first_name, u.last_name
            FROM debts d
            JOIN users u ON d.user_id = u.user_id
            WHERE d.expense_id = ?
        """, (expense_id,))
        
        debts = [dict(row) for row in cursor.fetchall()]
        expense_dict['debts'] = debts
        
        return expense_dict
    except Error as e:
        logger.error(f"Ошибка при получении расхода с долгами: {e}")
        return None
    finally:
        conn.close()

def update_expense_amount(expense_id, new_amount):
    """Обновляет сумму расхода и пересчитывает связанные долги."""
    conn = get_connection()
    if not conn:
        return False, "Ошибка соединения с базой данных"
    
    try:
        cursor = conn.cursor()
        
        # Получаем текущую информацию о расходе
        cursor.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,))
        expense = cursor.fetchone()
        if not expense:
            return False, "Расход не найден"
        
        old_amount = expense['amount']
        participants = json.loads(expense['participants'])
        
        # Если нет участников, просто обновляем сумму
        if not participants:
            cursor.execute("UPDATE expenses SET amount = ? WHERE id = ?", (new_amount, expense_id))
            conn.commit()
            return True, "Сумма расхода обновлена"
        
        # Вычисляем новую индивидуальную сумму долга
        new_individual_amount = new_amount / len(participants)
        
        # Обновляем сумму расхода
        cursor.execute("UPDATE expenses SET amount = ? WHERE id = ?", (new_amount, expense_id))
        
        # Обновляем все связанные долги
        cursor.execute("SELECT * FROM debts WHERE expense_id = ?", (expense_id,))
        debts = cursor.fetchall()
        
        for debt in debts:
            # Пропорционально обновляем сумму долга
            if old_amount > 0:  # Избегаем деления на ноль
                proportion = debt['amount'] / old_amount
                new_debt_amount = new_amount * proportion
            else:
                new_debt_amount = new_individual_amount
            
            cursor.execute("""
                UPDATE debts 
                SET amount = ? 
                WHERE id = ?
            """, (new_debt_amount, debt['id']))
        
        conn.commit()
        return True, "Сумма расхода и связанные долги обновлены"
    except Error as e:
        logger.error(f"Ошибка при обновлении суммы расхода: {e}")
        return False, f"Ошибка: {str(e)}"
    finally:
        conn.close()

def delete_expense(expense_id):
    """Удаляет расход и связанные с ним долги."""
    conn = get_connection()
    if not conn:
        return False, "Ошибка соединения с базой данных"
    
    try:
        cursor = conn.cursor()
        
        # Сначала удаляем все связанные долги
        cursor.execute("DELETE FROM debts WHERE expense_id = ?", (expense_id,))
        
        # Затем удаляем сам расход
        cursor.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        
        conn.commit()
        return True, "Расход и связанные долги успешно удалены"
    except Error as e:
        logger.error(f"Ошибка при удалении расхода: {e}")
        return False, f"Ошибка: {str(e)}"
    finally:
        conn.close()

def get_group_transactions(group_id, status=None):
    """Получает все транзакции в группе с опциональной фильтрацией по статусу."""
    conn = get_connection()
    if not conn:
        return []
    
    try:
        cursor = conn.cursor()
        
        query = """
            SELECT t.*, 
                su.username as sender_username, su.first_name as sender_first_name, su.last_name as sender_last_name,
                ru.username as receiver_username, ru.first_name as receiver_first_name, ru.last_name as receiver_last_name
            FROM transactions t
            JOIN users su ON t.sender_id = su.user_id
            JOIN users ru ON t.receiver_id = ru.user_id
            WHERE t.group_id = ?
        """
        params = [group_id]
        
        if status:
            query += " AND t.status = ?"
            params.append(status)
        
        query += " ORDER BY t.timestamp DESC"
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    except Error as e:
        logger.error(f"Ошибка при получении транзакций группы: {e}")
        return []
    finally:
        conn.close()

def delete_transaction(transaction_id):
    """Удаляет транзакцию."""
    conn = get_connection()
    if not conn:
        return False, "Ошибка соединения с базой данных"
    
    try:
        cursor = conn.cursor()
        
        # Проверяем, существует ли транзакция
        cursor.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,))
        transaction = cursor.fetchone()
        if not transaction:
            return False, "Транзакция не найдена"
        
        # Удаляем транзакцию
        cursor.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
        
        conn.commit()
        return True, "Транзакция успешно удалена"
    except Error as e:
        logger.error(f"Ошибка при удалении транзакции: {e}")
        return False, f"Ошибка: {str(e)}"
    finally:
        conn.close()

# Функция для сброса данных группы
def reset_group_data(group_id):
    """Сбрасывает все данные группы (расходы, долги, транзакции) без удаления пользователей."""
    conn = get_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        
        # Получаем все ID расходов в этой группе
        cursor.execute("SELECT id FROM expenses WHERE group_id = ?", (group_id,))
        expense_ids = [row['id'] for row in cursor.fetchall()]
        
        # Удаляем все долги, связанные с этими расходами
        if expense_ids:
            expense_ids_str = ','.join(['?' for _ in expense_ids])
            cursor.execute(f"DELETE FROM debts WHERE expense_id IN ({expense_ids_str})", expense_ids)
        
        # Удаляем все расходы группы
        cursor.execute("DELETE FROM expenses WHERE group_id = ?", (group_id,))
        
        # Удаляем все транзакции группы
        cursor.execute("DELETE FROM transactions WHERE group_id = ?", (group_id,))
        
        # Сбрасываем правила группы (если они есть)
        cursor.execute("DELETE FROM rules WHERE group_id = ?", (group_id,))
        
        conn.commit()
        return True
    except Error as e:
        logger.error(f"Ошибка при сбросе данных группы: {e}")
        return False
    finally:
        conn.close()

# Rules-related functions
def get_group_rules(group_id):
    """Получает правила для определенной группы."""
    conn = get_connection()
    if not conn:
        return None
    
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM rules WHERE group_id = ?", (group_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    except Error as e:
        logger.error(f"Ошибка при получении правил группы: {e}")
        return None
    finally:
        conn.close()

def set_group_rules(group_id, description, deadline_hours, notifications_time):
    """Устанавливает или обновляет правила для группы."""
    conn = get_connection()
    if not conn:
        return False
    
    try:
        cursor = conn.cursor()
        # Проверяем, существуют ли правила
        cursor.execute("SELECT * FROM rules WHERE group_id = ?", (group_id,))
        if cursor.fetchone():
            # Обновляем существующие правила
            cursor.execute("""
                UPDATE rules 
                SET description = ?, deadline_hours = ?, notifications_time = ? 
                WHERE group_id = ?
            """, (description, deadline_hours, notifications_time, group_id))
        else:
            # Вставляем новые правила
            cursor.execute("""
                INSERT INTO rules (group_id, description, deadline_hours, notifications_time) 
                VALUES (?, ?, ?, ?)
            """, (group_id, description, deadline_hours, notifications_time))
        
        conn.commit()
        return True
    except Error as e:
        logger.error(f"Ошибка при установке правил группы: {e}")
        return False
    finally:
        conn.close()
