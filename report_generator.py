import pandas as pd
import logging
import io
import datetime
from db_manager import get_group_expenses, get_group_members, get_user_debt_summary, get_user
from fpdf import FPDF

# Настройка логирования
logger = logging.getLogger(__name__)

def generate_excel_report(group_id, start_date=None, end_date=None):
    """Создает Excel отчет о расходах и долгах для группы."""
    try:
        # Получаем расходы
        expenses = get_group_expenses(group_id, start_date, end_date)
        
        # Получаем участников
        members = get_group_members(group_id)
        
        # Создаем датафрейм расходов
        expense_data = []
        for expense in expenses:
            # Получаем информацию о пользователе, добавившем расход
            admin = get_user(expense['admin_id'])
            admin_name = f"{admin.get('first_name', '')} {admin.get('last_name', '')}".strip() if admin else str(expense['admin_id'])
            
            expense_data.append({
                'ID': expense['id'],
                'Дата': datetime.datetime.fromisoformat(expense['date']).strftime('%d.%m.%Y %H:%M'),
                'Описание': expense['description'],
                'Сумма': expense['amount'],
                'Добавил': admin_name
            })
        
        expense_df = pd.DataFrame(expense_data)
        
        # Создаем датафрейм для сводной таблицы, исключая бота
        debt_data = []
        # Фильтруем ботов по имени пользователя (как правило, имена ботов заканчиваются на 'bot')
        human_members = [m for m in members if not m.get('username', '').lower().endswith('bot')]
        
        # Рассчитываем общую сумму расходов
        total_expense_sum = sum(e['amount'] for e in expenses)
        
        # Перебираем каждого участника, кроме бота
        for i, member in enumerate(human_members, 1):
            user_id = member['user_id']
            
            # Получаем полное имя пользователя
            first_name = member.get('first_name', '')
            last_name = member.get('last_name', '')
            full_name = f"{first_name} {last_name}".strip()
            if not full_name:
                full_name = member.get('username', 'Неизвестно')
            
            # Определяем общую сумму трат пользователя
            # Находим все расходы, созданные этим пользователем
            user_expenses = [e for e in expenses if e['admin_id'] == user_id]
            total_expenses = sum(e['amount'] for e in user_expenses)
            
            # Рассчитываем долг по новой формуле:
            # (Сумма всех трат всех участников / кол-во участников) - сумма трат пользователя
            share_per_person = total_expense_sum / len(human_members)
            debt = share_per_person - total_expenses
            
            debt_data.append({
                '№': i,
                'ID': user_id,
                'Имя Фамилия': full_name,
                'Общая сумма трат': total_expenses,
                'Долг': debt
            })
        
        debt_df = pd.DataFrame(debt_data)
        
        # Создаем объект BytesIO для хранения данных в памяти
        output = io.BytesIO()
        
        # Создаем Excel-писатель с использованием BytesIO
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            expense_df.to_excel(writer, sheet_name='Расходы', index=False)
            debt_df.to_excel(writer, sheet_name='Сводная таблица', index=False)
        
        # Получаем содержимое объекта BytesIO
        output.seek(0)
        return output
    except Exception as e:
        logger.error(f"Ошибка создания Excel отчета: {e}")
        return None

def generate_pdf_report(group_id, start_date=None, end_date=None):
    """Создать PDF отчет о расходах и долгах для группы."""
    try:
        # Получаем расходы
        expenses = get_group_expenses(group_id, start_date, end_date)
        
        # Получаем участников
        members = get_group_members(group_id)
        
        # Создаем PDF с поддержкой кириллицы (DejaVu)
        class PDF(FPDF):
            def __init__(self):
                super().__init__()
                # Добавляем поддержку кириллицы
                self.add_font('DejaVu', '', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', uni=True)
                self.add_font('DejaVu', 'B', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', uni=True)
        
        # Создаем PDF объект
        try:
            pdf = PDF()
            # Проверяем, что шрифты загрузились
            font_test = pdf.fonts.get('DejaVu')
            use_dejavu = True
        except Exception as e:
            logger.warning(f"Не удалось загрузить шрифт DejaVu, используем стандартный: {e}")
            # Если шрифт не загрузился, то мы не сможем отображать кириллицу правильно
            # Вместо этого создаем простой PDF без кириллицы и сразу возвращаем его
            return generate_simple_pdf_report(group_id, expenses, members)
        
        # Если шрифты загрузились успешно, создаем отчет с кириллицей
        pdf.add_page()
        
        # Заголовок
        pdf.set_font("DejaVu", "B", 16)
        title = "Отчет о расходах"
        pdf.cell(0, 10, title, 0, 1, "C")
        pdf.ln(10)
        
        # Раздел расходов
        pdf.set_font("DejaVu", "B", 14)
        expenses_title = "Расходы"
        pdf.cell(0, 10, expenses_title, 0, 1, "L")
        pdf.ln(5)
        
        # Заголовок таблицы расходов
        pdf.set_font("DejaVu", "B", 10)
        
        # Заголовки таблицы
        headers = ["ID", "Дата", "Описание", "Сумма", "Добавил"]
        
        pdf.cell(20, 10, headers[0], 1, 0, "C")
        pdf.cell(40, 10, headers[1], 1, 0, "C")
        pdf.cell(70, 10, headers[2], 1, 0, "C")
        pdf.cell(30, 10, headers[3], 1, 0, "C")
        pdf.cell(30, 10, headers[4], 1, 1, "C")
        
        # Данные таблицы расходов
        pdf.set_font("DejaVu", "", 10)
            
        for expense in expenses:
            date_str = datetime.datetime.fromisoformat(expense['date']).strftime('%d.%m.%Y %H:%M')
            pdf.cell(20, 10, str(expense['id']), 1, 0, "C")
            pdf.cell(40, 10, date_str, 1, 0, "C")
            
            # Обработка длинных описаний
            description = expense['description']
            if len(description) > 30:
                # Используем простые точки вместо специального символа многоточия
                description = description[:27] + "..."
            
            pdf.cell(70, 10, description, 1, 0, "L")
            pdf.cell(30, 10, f"{expense['amount']:.2f}", 1, 0, "R")
            
            # Получаем информацию о пользователе, добавившем расход
            admin = get_user(expense['admin_id'])
            admin_name = f"{admin.get('first_name', '')} {admin.get('last_name', '')}".strip() if admin else str(expense['admin_id'])
            
            if not admin_name and admin:
                admin_name = admin.get('username', str(expense['admin_id']))
                
            pdf.cell(30, 10, admin_name, 1, 1, "C")
        
        pdf.ln(10)
        
        # Раздел сводной таблицы
        pdf.set_font("DejaVu", "B", 14)
        debts_title = "Сводная таблица"
        pdf.cell(0, 10, debts_title, 0, 1, "L")
        pdf.ln(5)
        
        # Заголовок таблицы долгов
        pdf.set_font("DejaVu", "B", 10)
        
        debt_headers = ["№", "ID", "Имя Фамилия", "Общая сумма трат", "Долг"]
        
        # Ширина столбцов
        pdf.cell(10, 10, debt_headers[0], 1, 0, "C")
        pdf.cell(30, 10, debt_headers[1], 1, 0, "C")
        pdf.cell(60, 10, debt_headers[2], 1, 0, "C")
        pdf.cell(45, 10, debt_headers[3], 1, 0, "C")
        pdf.cell(45, 10, debt_headers[4], 1, 1, "C")
        
        # Данные таблицы долгов
        pdf.set_font("DejaVu", "", 10)
        
        # Фильтруем ботов по имени пользователя (как правило, имена ботов заканчиваются на 'bot')
        human_members = [m for m in members if not m.get('username', '').lower().endswith('bot')]
        
        # Рассчитываем общую сумму расходов
        total_expense_sum = sum(e['amount'] for e in expenses)
        
        # Перебираем каждого участника, кроме бота
        for i, member in enumerate(human_members, 1):
            user_id = member['user_id']
            
            # Получаем полное имя пользователя
            first_name = member.get('first_name', '')
            last_name = member.get('last_name', '')
            full_name = f"{first_name} {last_name}".strip()
            if not full_name:
                full_name = member.get('username', 'Неизвестно')
            
            # Определяем общую сумму трат пользователя
            user_expenses = [e for e in expenses if e['admin_id'] == user_id]
            total_expenses = sum(e['amount'] for e in user_expenses)
            
            # Рассчитываем долг по новой формуле:
            # (Сумма всех трат всех участников / кол-во участников) - сумма трат пользователя
            share_per_person = total_expense_sum / len(human_members)
            debt = share_per_person - total_expenses
            
            # Выводим строку с данными пользователя
            pdf.cell(10, 10, str(i), 1, 0, "C")
            pdf.cell(30, 10, str(user_id), 1, 0, "C")
            pdf.cell(60, 10, full_name, 1, 0, "L")
            pdf.cell(45, 10, f"{total_expenses:.2f}", 1, 0, "R")
            pdf.cell(45, 10, f"{debt:.2f}", 1, 1, "R")
        
        # Сохраняем PDF напрямую в BytesIO объект
        output = io.BytesIO()
        
        # Создаем временный файл-дескриптор в памяти и получаем его имя
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as temp_file:
            temp_filename = temp_file.name
            
        # Сохраняем PDF во временный файл
        try:
            pdf.output(temp_filename)
            
            # Читаем файл в BytesIO
            with open(temp_filename, 'rb') as f:
                output.write(f.read())
                
            # Удаляем временный файл
            import os
            os.unlink(temp_filename)
            
            # Возвращаем BytesIO в начало для чтения
            output.seek(0)
            
            return output
            
        except Exception as e:
            logger.error(f"Ошибка сохранения PDF: {e}")
            # Если не удалось сохранить PDF с кириллицей, возвращаем простой PDF
            return generate_simple_pdf_report(group_id, expenses, members)
            
    except Exception as e:
        logger.error(f"Ошибка создания PDF отчета: {e}")
        logger.exception(e)  # Добавляем полный стек ошибки для отладки
        # В случае ошибки возвращаем самый простой PDF
        try:
            simple_pdf = FPDF()
            simple_pdf.add_page()
            simple_pdf.set_font("Arial", "B", 16)
            simple_pdf.cell(0, 10, "Otchet", 0, 1, "C") # Простая метка
            
            # Сохраняем самый простой PDF
            output = io.BytesIO()
            
            # Создаем временный файл для PDF
            import tempfile
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as backup_file:
                backup_filename = backup_file.name
            
            simple_pdf.output(backup_filename)
            
            # Читаем файл в BytesIO
            with open(backup_filename, 'rb') as f:
                output = io.BytesIO(f.read())
            
            # Удаляем временный файл
            import os
            os.unlink(backup_filename)
            
            # Возвращаем BytesIO в начало для чтения
            output.seek(0)
            return output
            
        except Exception as e2:
            logger.error(f"Критическая ошибка создания PDF: {e2}")
            return None

def generate_simple_pdf_report(group_id, expenses=None, members=None):
    """Создает простой PDF отчет без кириллицы для случаев, когда шрифты не доступны."""
    try:
        # Если данные не предоставлены, получаем их
        if expenses is None:
            expenses = get_group_expenses(group_id)
        if members is None:
            members = get_group_members(group_id)
        
        # Фильтруем ботов по имени пользователя (как правило, имена ботов заканчиваются на 'bot')
        human_members = [m for m in members if not m.get('username', '').lower().endswith('bot')]
        
        # Создаем PDF объект
        pdf = FPDF()
        pdf.add_page()
        
        # Заголовок
        pdf.set_font("Arial", "B", 16)
        pdf.cell(0, 10, "Expenses Report", 0, 1, "C")
        pdf.ln(5)
        
        # Добавляем информацию о отчете
        pdf.set_font("Arial", "", 10)
        pdf.cell(0, 10, f"Report date: {datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}", 0, 1, "L")
        pdf.cell(0, 10, f"Group ID: {group_id}", 0, 1, "L")
        pdf.cell(0, 10, f"Number of expenses: {len(expenses)}", 0, 1, "L")
        pdf.cell(0, 10, f"Number of human members: {len(human_members)}", 0, 1, "L")
        pdf.ln(10)
        
        # Добавляем информацию о том, что полный отчет доступен в Excel
        pdf.set_font("Arial", "B", 12)
        pdf.cell(0, 10, "Please use Excel report for full information with correct text display.", 0, 1, "C")
        
        # Сохраняем PDF напрямую в BytesIO объект
        output = io.BytesIO()
        
        # Создаем временный файл для PDF
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as temp_file:
            temp_filename = temp_file.name
        
        # Сохраняем PDF во временный файл
        pdf.output(temp_filename)
        
        # Читаем файл в BytesIO
        with open(temp_filename, 'rb') as f:
            output.write(f.read())
        
        # Удаляем временный файл
        import os
        os.unlink(temp_filename)
        
        # Возвращаем BytesIO в начало для чтения
        output.seek(0)
        return output
        
    except Exception as e:
        logger.error(f"Ошибка создания простого PDF отчета: {e}")
        return None
