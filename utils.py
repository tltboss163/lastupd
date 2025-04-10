import re
import logging

# Configure logging
logger = logging.getLogger(__name__)

async def is_admin(update, context):
    """Check if the user is an admin in the chat."""
    try:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        
        # Get chat administrators
        chat_admins = await context.bot.get_chat_administrators(chat_id)
        admin_ids = [admin.user.id for admin in chat_admins]
        
        return user_id in admin_ids
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return False

def extract_username_and_amount(args):
    """Extract username and amount from /send command arguments."""
    try:
        # Check for format: /send @username 500
        if len(args) >= 2:
            username = args[0]
            amount_str = args[1]
            
            # Clean username
            if username.startswith('@'):
                username = username[1:]
            
            # Convert amount to float
            try:
                amount = float(amount_str.replace(',', '.'))
                if amount <= 0:
                    return None, None
                return username, amount
            except ValueError:
                return None, None
        
        # Alternative format with amount after username
        elif len(args) == 1:
            # Try to find username and amount in one argument
            match = re.match(r'@?(\w+)\s+(\d+(?:\.\d+)?)', args[0])
            if match:
                username = match.group(1)
                amount = float(match.group(2).replace(',', '.'))
                if amount <= 0:
                    return None, None
                return username, amount
        
        return None, None
    except Exception as e:
        logger.error(f"Error extracting username and amount: {e}")
        return None, None

def format_currency(amount):
    """Format amount as currency."""
    return f"{amount:.2f} руб."

def format_date(date_string):
    """Format ISO date string to a readable format."""
    from datetime import datetime
    try:
        date = datetime.fromisoformat(date_string)
        return date.strftime('%d.%m.%Y %H:%M')
    except Exception as e:
        logger.error(f"Error formatting date: {e}")
        return date_string
