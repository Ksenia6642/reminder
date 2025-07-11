import os
import logging
import asyncio
import sqlite3
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Union
import pytz
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    PhotoSize,
    Document
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния диалога для ConversationHandler
(
    SETTING_REMINDER_TEXT,
    SETTING_REMINDER_TIME,
    SETTING_REMINDER_FREQUENCY,
    SETTING_REMINDER_COMMENT,
    SETTING_BATCH_REMINDERS,
    SETTING_BATCH_FREQUENCY,
    EDITING_REMINDER
) = range(7)  # Теперь у нас 7 состояний

# Часовой пояс по умолчанию
DEFAULT_TIMEZONE = 'Europe/Moscow'

# Загрузка переменных окружения
load_dotenv()


class ReminderBot:
    def __init__(self):
        """Инициализация бота с настройкой всех компонентов"""
        # Блокировка для безопасной работы с планировщиком
        self._scheduler_lock = asyncio.Lock()
        
        # Флаг состояния работы бота
        self._is_running = False
        
        # Основной объект приложения Telegram
        self.application = None
        
        # Планировщик напоминаний
        self.scheduler = AsyncIOScheduler(timezone=pytz.timezone(DEFAULT_TIMEZONE))
        
        # Инициализация клавиатур
        self._initialize_keyboards()
        
        # Проверка и создание базы данных
        self._initialize_database()

    async def start_batch_reminders(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Начало процесса добавления нескольких напоминаний"""
        await update.message.reply_text(
            "Отправьте список напоминаний в формате:\n"
            "ЧЧ:ММ Текст напоминания\n"
            "ЧЧ:ММ Текст напоминания\n\n"
            "Пример:\n"
            "05:00 Подъем\n"
            "06:00 Зарядка\n"
            "07:00 Английский",
            reply_markup=self.cancel_keyboard
        )
        return SETTING_BATCH_REMINDERS

    async def parse_batch_reminders(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Парсинг списка напоминаний"""
        if update.message.text == "🔙 Отмена":
            await self.cancel_conversation(update, context)
            return ConversationHandler.END
        
        try:
            reminders = []
            for line in update.message.text.split('\n'):
                if not line.strip():
                    continue
                time_part, *text_parts = line.strip().split()
                datetime.strptime(time_part, "%H:%M")  # Проверка формата времени
                text = ' '.join(text_parts)
                reminders.append({'time': time_part, 'text': text})
            
            if not reminders:
                raise ValueError("Не найдено ни одного напоминания")
            
            context.user_data['batch_reminders'] = reminders
            
            # Клавиатура выбора периодичности
            keyboard = [
                [InlineKeyboardButton("Ежедневно", callback_data='daily')],
                [InlineKeyboardButton("Еженедельно", callback_data='weekly')],
                [InlineKeyboardButton("По будням (Пн-Пт)", callback_data='weekdays')],
                [InlineKeyboardButton("Пн, Ср, Пт", callback_data='mon_wed_fri')],
                [InlineKeyboardButton("Вт, Чт", callback_data='tue_thu')]
            ]
            
            await update.message.reply_text(
                "Выберите периодичность для всех напоминаний:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return SETTING_BATCH_FREQUENCY
            
        except ValueError as e:
            await update.message.reply_text(
                f"Ошибка в формате: {str(e)}\n\n"
                "Пожалуйста, отправьте список в правильном формате:\n"
                "ЧЧ:ММ Текст напоминания\n"
                "ЧЧ:ММ Текст напоминания",
                reply_markup=self.cancel_keyboard
            )
            return SETTING_BATCH_REMINDERS

    async def set_batch_frequency(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Установка периодичности для группы напоминаний"""
        query = update.callback_query
        await query.answer()
        
        frequency_map = {
            'daily': "Ежедневно",
            'weekly': "Еженедельно",
            'weekdays': "По будням (Пн-Пт)",
            'mon_wed_fri': "Пн, Ср, Пт",
            'tue_thu': "Вт, Чт"
        }
        
        frequency = query.data
        context.user_data['batch_frequency'] = frequency
        context.user_data['batch_frequency_text'] = frequency_map[frequency]
        
        # Удаляем предыдущее сообщение с клавиатурой
        try:
            await query.message.delete()
        except Exception as e:
            logger.warning(f"Не удалось удалить сообщение: {e}")
        
        # Создаем все напоминания
        user_id = query.from_user.id
        created_count = 0
        
        for reminder in context.user_data['batch_reminders']:
            job_id = f"rem_{user_id}_{datetime.now().timestamp()}_{created_count}"
            
            # Сохраняем в базу данных
            await self.save_reminder_to_database(
                user_id=user_id,
                job_id=job_id,
                reminder_text=reminder['text'],
                reminder_time=reminder['time'],
                frequency=frequency,
                frequency_text=frequency_map[frequency],
                comment=None
            )
            
            # Планируем напоминание
            reminder_data = {
                'job_id': job_id,
                'text': reminder['text'],
                'time': reminder['time'],
                'frequency': frequency,
                'frequency_text': frequency_map[frequency],
                'comment': None
            }
            await self.schedule_reminder(user_id, reminder_data)
            created_count += 1
        
        await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ Успешно создано {created_count} напоминаний с периодичностью {frequency_map[frequency]}!\n\n"
                "Теперь вы можете отредактировать любое из них, добавив комментарий.",
            reply_markup=self.main_menu_keyboard
        )
        
        context.user_data.clear()
        return ConversationHandler.END

    async def start_edit_reminder(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Начало процесса редактирования напоминания"""
        await update.message.reply_text(
            "Введите ID напоминания, которое хотите отредактировать.\n"
            "Вы можете посмотреть ID через /list",
            reply_markup=self.cancel_keyboard
        )
        return EDITING_REMINDER

    async def edit_reminder(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Редактирование существующего напоминания"""
        if update.message.text == "🔙 Отмена":
            await self.cancel_conversation(update, context)
            return ConversationHandler.END
        
        job_id = update.message.text.strip()
        user_id = update.message.from_user.id
        
        # Проверяем, существует ли такое напоминание у пользователя
        connection = sqlite3.connect('reminders.db')
        cursor = connection.cursor()
        cursor.execute(
            'SELECT 1 FROM reminders WHERE user_id = ? AND job_id = ?',
            (user_id, job_id)
        )
        exists = cursor.fetchone()
        connection.close()
        
        if not exists:
            await update.message.reply_text(
                "Напоминание с таким ID не найдено. Пожалуйста, проверьте ID и попробуйте еще раз.",
                reply_markup=self.cancel_keyboard
            )
            return EDITING_REMINDER
        
        context.user_data['editing_job_id'] = job_id
        
        await update.message.reply_text(
            "Отправьте новый комментарий для напоминания (текст, фото или файл):",
            reply_markup=self.skip_keyboard
        )
        return SETTING_REMINDER_COMMENT

    async def reschedule_reminder(self, job_id: str):
        """Перепланирует напоминание с новыми параметрами"""
        reminder = await self.get_reminder_by_id(job_id)
        if not reminder:
            return
        
        user_id = int(job_id.split('_')[1])  # Извлекаем user_id из job_id
        
        # Удаляем старую задачу
        try:
            self.scheduler.remove_job(job_id)
        except:
            pass
        
        # Создаем новую задачу
        reminder_data = {
            'job_id': job_id,
            'text': reminder['text'],
            'time': reminder['time'],
            'frequency': reminder['frequency'],
            'frequency_text': reminder['frequency_text'],
            'comment': {
                'type': reminder['comment_type'],
                'content': reminder['comment_text'],
                'file_id': None,
                'file_name': reminder['comment_file_name']
            } if reminder['comment_type'] else None
        }
        
        await self.schedule_reminder(user_id, reminder_data)

    async def update_reminder_field(self, update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, new_value: any):
        """Обновляет указанное поле напоминания"""
        job_id = context.user_data['editing_job_id']
        
        connection = sqlite3.connect('reminders.db')
        cursor = connection.cursor()
        
        if field == 'text':
            cursor.execute(
                'UPDATE reminders SET reminder_text = ? WHERE job_id = ?',
                (new_value, job_id)
            )
        elif field == 'time':
            cursor.execute(
                'UPDATE reminders SET reminder_time = ? WHERE job_id = ?',
                (new_value, job_id)
            )
        elif field == 'frequency':
            cursor.execute(
                'UPDATE reminders SET frequency = ?, frequency_text = ? WHERE job_id = ?',
                (new_value['frequency'], new_value['frequency_text'], job_id)
            )
        elif field == 'comment':
            cursor.execute(
                '''
                UPDATE reminders SET 
                    comment_type = ?,
                    comment_text = ?,
                    comment_file_id = ?,
                    comment_file_name = ?
                WHERE job_id = ?
                ''',
                (
                    new_value['type'] if new_value else None,
                    new_value.get('content') if new_value else None,
                    new_value.get('file_id') if new_value else None,
                    new_value.get('file_name') if new_value else None,
                    job_id
                )
            )
        
        connection.commit()
        connection.close()
        
        # Обновляем задачу в планировщике
        if field in ['time', 'frequency']:
            await self.reschedule_reminder(job_id)

    async def update_reminder_comment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Обновление комментария для существующего напоминания"""
        user = update.effective_user
        job_id = context.user_data['editing_job_id']
        
        if update.message.text == "🔙 Отмена":
            await self.cancel_conversation(update, context)
            return ConversationHandler.END
        
        # Обработка вложений
        comment = None
        if update.message.text != "Пропустить":
            if update.message.photo:
                photo = update.message.photo[-1]
                comment = {
                    'type': 'photo',
                    'file_id': photo.file_id,
                    'caption': update.message.caption
                }
            elif update.message.document:
                document = update.message.document
                comment = {
                    'type': 'document',
                    'file_id': document.file_id,
                    'file_name': document.file_name,
                    'caption': update.message.caption
                }
            else:
                comment = {
                    'type': 'text',
                    'content': update.message.text
                }
        
        # Обновление в базе данных
        connection = sqlite3.connect('reminders.db')
        cursor = connection.cursor()
        
        cursor.execute(
            '''
            UPDATE reminders SET
                comment_type = ?,
                comment_text = ?,
                comment_file_id = ?,
                comment_file_name = ?
            WHERE job_id = ?
            ''',
            (
                comment['type'] if comment else None,
                comment.get('content') if comment else None,
                comment.get('file_id') if comment else None,
                comment.get('file_name') if comment else None,
                job_id
            )
        )
        
        connection.commit()
        connection.close()
        
        await update.message.reply_text(
            "✅ Напоминание успешно обновлено!",
            reply_markup=self.main_menu_keyboard
        )
        
        context.user_data.clear()
        return ConversationHandler.END

    # Методы работы с базой данных
    async def get_user_timezone(self, user_id: int) -> str:
        """Получает часовой пояс пользователя из базы данных"""
        # Проверяем доступ к файлу БД

        connection = sqlite3.connect('reminders.db')
        cursor = connection.cursor()

        cursor.execute(
            'SELECT timezone FROM user_timezones WHERE user_id = ?',
            (user_id,)
        )
        result = cursor.fetchone()
        connection.close()

        return result[0] if result else DEFAULT_TIMEZONE

    async def set_user_timezone(self, user_id: int, timezone: str):
        """Устанавливает часовой пояс пользователя в базе данных"""
        connection = sqlite3.connect('reminders.db')
        cursor = connection.cursor()

        cursor.execute(
            '''
            INSERT OR REPLACE INTO user_timezones (user_id, timezone)
            VALUES (?, ?)
            ''',
            (user_id, timezone)
        )

        connection.commit()
        connection.close()
    

    def _initialize_database(self):
        """Создание и проверка структуры базы данных"""
        if not os.path.exists('reminders.db'):
            logger.info("Создание новой базы данных reminders.db")
            
        conn = sqlite3.connect('reminders.db')
        cursor = conn.cursor()
        
        # Таблица напоминаний
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS reminders (
            user_id INTEGER NOT NULL,
            job_id TEXT PRIMARY KEY,
            reminder_text TEXT NOT NULL,
            reminder_time TEXT NOT NULL,
            frequency TEXT NOT NULL,
            frequency_text TEXT NOT NULL,
            comment_type TEXT,
            comment_text TEXT,
            comment_file_id TEXT,
            comment_file_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # Таблица часовых поясов пользователей
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_timezones (
            user_id INTEGER PRIMARY KEY,
            timezone TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        conn.commit()
        conn.close()
    
    def _initialize_keyboards(self):
        """Инициализация всех клавиатур бота"""
        # Основное меню
        self.main_menu_keyboard = ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Добавить напоминание"), KeyboardButton("📝 Добавить несколько")],
            [KeyboardButton("📋 Список напоминаний"), KeyboardButton("❌ Удалить напоминание")],
            [KeyboardButton("✏️ Редактировать"), KeyboardButton("🌍 Изменить часовой пояс")],
            [KeyboardButton("🔄 Тест напоминания")]
        ],
        resize_keyboard=True
    )

        # Клавиатура для отмены действий
        self.cancel_keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("🔙 Отмена")]],
            resize_keyboard=True
        )

        # Клавиатура для пропуска комментария
        self.skip_keyboard = ReplyKeyboardMarkup(
            [
                [KeyboardButton("Пропустить")],
                [KeyboardButton("🔙 Отмена")]
            ],
            resize_keyboard=True
        )

    async def load_all_reminders(self):
        """Загрузка с проверкой состояния"""
        if not self.scheduler.running:
            self.scheduler.start(paused=True)  # Временный старт для добавления задач
            
        connection = sqlite3.connect('reminders.db')
        cursor = connection.cursor()
        cursor.execute('SELECT * FROM reminders')
        
        for reminder in cursor.fetchall():
            try:
                # ... ваш существующий код ...
                self.scheduler.add_job(
                    self.send_reminder,
                    trigger=trigger,
                    args=[user_id, job_id],
                    id=job_id,
                    replace_existing=True,
                    misfire_grace_time=60  # Добавьте это
                )
            except Exception as e:
                logger.error(f"Failed to load reminder {job_id}: {str(e)}")
        
        if self.scheduler.state == 1:  # STATE_PAUSED
            self.scheduler.resume()

    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        user = update.effective_user
        timezone = await self.get_user_timezone(user.id)
        
        await update.message.reply_text(
            f"Привет, {user.first_name}!\n"
            f"Я бот для управления напоминаниями.\n"
            f"Текущий часовой пояс: {timezone}\n\n"
            "Используйте меню ниже или команды:\n"
            "/add - создать напоминание\n"
            "/list - список напоминаний\n"
            "/help - помощь",
            reply_markup=self.main_menu_keyboard
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /help"""
        help_text = """
    📚 <b>Справка по командам бота</b>:

    <b>Основные команды:</b>
    /start - Начало работы
    /help - Эта справка
    /status - Проверить состояние бота

    <b>Работа с напоминаниями:</b>
    /add - Создать новое напоминание
    /list - Показать все напоминания
    /delete [ID] - Удалить напоминание

    <b>Настройки:</b>
    /timezone - Изменить часовой пояс
    /test - Отправить тестовое напоминание

    ℹ Для создания напоминаний также можно использовать кнопки меню.
    """
        await update.message.reply_text(help_text, parse_mode='HTML')

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /status"""
        status_lines = [
            "🔄 <b>Статус бота</b>",
            f"• Состояние: {'работает ✅' if self._is_running else 'остановлен ❌'}",
            f"• Напоминаний в базе: {self._count_reminders()}",
            f"• Активных задач: {len(self.scheduler.get_jobs()) if hasattr(self, 'scheduler') else 0}",
            f"• Часовой пояс: {await self.get_user_timezone(update.effective_user.id)}"
        ]
        await update.message.reply_text("\n".join(status_lines), parse_mode='HTML')

    def _count_reminders(self) -> int:
        """Подсчет количества напоминаний в базе"""
        try:
            conn = sqlite3.connect('reminders.db')
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM reminders')
            count = cursor.fetchone()[0]
            conn.close()
            return count
        except:
            return 0

    async def list_reminders(self, update: Update):
        """Показывает список всех напоминаний пользователя с информацией о комментариях"""
        user = update.effective_user
        reminders = await self.get_user_reminders(user.id)
        
        if not reminders:
            await update.message.reply_text(
                "У вас пока нет напоминаний.",
                reply_markup=self.main_menu_keyboard
            )
            return
        
        message = ["📋 <b>Ваши напоминания</b>:\n"]
        for i, reminder in enumerate(reminders, 1):
            # Получаем информацию о комментарии
            comment_info = ""
            if reminder.get('comment_type'):
                if reminder['comment_type'] == 'text':
                    comment_text = reminder.get('comment_text', '')[:20] + "..." if len(reminder.get('comment_text', '')) > 20 else reminder.get('comment_text', '')
                    comment_info = f" | 💬: {comment_text}"
                elif reminder['comment_type'] == 'photo':
                    comment_info = " | 📷 Фото"
                    if reminder.get('comment_text'):
                        comment_info += f" ({reminder['comment_text'][:20]}...)"
                elif reminder['comment_type'] == 'document':
                    doc_name = reminder.get('comment_file_name', 'документ')
                    comment_info = f" | 📄 {doc_name}"
                    if reminder.get('comment_text'):
                        comment_info += f" ({reminder['comment_text'][:20]}...)"
            
            message.append(
                f"{i}. <b>{reminder['text']}</b>\n"
                f"   ⏰ {reminder['time']} ({reminder['frequency_text']}){comment_info}\n"
                f"   🆔 <code>{reminder['job_id']}</code>\n"
            )
        
        await update.message.reply_text(
            "\n".join(message),
            parse_mode='HTML',
            reply_markup=self.main_menu_keyboard
        )

    async def handle_main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик главного меню"""
        text = update.message.text
        
        if text == "➕ Добавить напоминание":
            return await self.start_reminder_creation(update, context)
        elif text == "📋 Список напоминаний":
            return await self.list_reminders(update)
        elif text == "❌ Удалить напоминание":
            return await self.show_delete_menu(update, context)
        elif text == "🌍 Изменить часовой пояс":
            return await self.show_timezone_menu(update)
        elif text == "🔄 Тест напоминания":
            return await self.send_test_reminder(update)
        else:
            await update.message.reply_text(
                "Пожалуйста, используйте кнопки меню или команды из /help",
                reply_markup=self.main_menu_keyboard
            )

    async def start_reminder_creation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Начало процесса создания напоминания"""
        await update.message.reply_text(
            "Введите текст напоминания:",
            reply_markup=self.cancel_keyboard
        )
        return SETTING_REMINDER_TEXT

    async def set_reminder_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Установка текста напоминания"""
        if update.message.text == "🔙 Отмена":
            await self.cancel_conversation(update, context)
            return ConversationHandler.END
        
        context.user_data['reminder'] = {
            'text': update.message.text,
            'user_id': update.message.from_user.id
        }
        
        await update.message.reply_text(
            "Введите время напоминания в формате ЧЧ:ММ (например, 14:30):",
            reply_markup=self.cancel_keyboard
        )
        return SETTING_REMINDER_TIME

    async def set_reminder_time(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Установка времени напоминания"""
        if update.message.text == "🔙 Отмена":
            await self.cancel_conversation(update, context)
            return ConversationHandler.END
        
        try:
            # Проверка формата времени
            datetime.strptime(update.message.text, "%H:%M")
            context.user_data['reminder']['time'] = update.message.text
            
            # Клавиатура выбора периодичности
            keyboard = [
                [InlineKeyboardButton("Один раз", callback_data='once')],
                [InlineKeyboardButton("Ежедневно", callback_data='daily')],
                [InlineKeyboardButton("Еженедельно", callback_data='weekly')],
                [InlineKeyboardButton("По будням (Пн-Пт)", callback_data='weekdays')],
                [InlineKeyboardButton("Пн, Ср, Пт", callback_data='mon_wed_fri')],
                [InlineKeyboardButton("Вт, Чт", callback_data='tue_thu')]
            ]
            
            await update.message.reply_text(
                "Выберите периодичность напоминания:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return SETTING_REMINDER_FREQUENCY
            
        except ValueError:
            await update.message.reply_text(
                "Неверный формат времени. Пожалуйста, введите время в формате ЧЧ:ММ (например, 09:30):",
                reply_markup=self.cancel_keyboard
            )
            return SETTING_REMINDER_TIME

    async def set_reminder_frequency(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Установка периодичности напоминания"""
        query = update.callback_query
        await query.answer()
        
        frequency_map = {
            'once': "Один раз",
            'daily': "Ежедневно",
            'weekly': "Еженедельно",
            'weekdays': "По будням (Пн-Пт)",
            'mon_wed_fri': "Пн, Ср, Пт",
            'tue_thu': "Вт, Чт"
        }
        
        frequency = query.data
        context.user_data['reminder']['frequency'] = frequency
        context.user_data['reminder']['frequency_text'] = frequency_map[frequency]
        
        # Удаляем предыдущее сообщение с клавиатурой
        try:
            await query.message.delete()
        except Exception as e:
            logger.warning(f"Не удалось удалить сообщение: {e}")
        
        # Отправляем новое сообщение без клавиатуры
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text=f"Периодичность: {frequency_map[frequency]}\n\n"
                "Вы можете прикрепить комментарий (текст, фото или файл) или нажмите 'Пропустить'.",
            reply_markup=self.skip_keyboard
        )
        return SETTING_REMINDER_COMMENT

    async def set_reminder_comment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Обработка комментария к напоминанию"""
        user = update.effective_user
        reminder = context.user_data['reminder']
        
        if update.message.text == "🔙 Отмена":
            await self.cancel_conversation(update, context)
            return ConversationHandler.END
        
        # Обработка вложений
        comment = None
        if update.message.text != "Пропустить":
            if update.message.photo:
                photo = update.message.photo[-1]
                comment = {
                    'type': 'photo',
                    'file_id': photo.file_id,
                    'caption': update.message.caption
                }
            elif update.message.document:
                document = update.message.document
                comment = {
                    'type': 'document',
                    'file_id': document.file_id,
                    'file_name': document.file_name,
                    'caption': update.message.caption
                }
            else:
                comment = {
                    'type': 'text',
                    'content': update.message.text
                }
        
        # Создание ID задачи
        job_id = f"rem_{user.id}_{datetime.now().timestamp()}"
        
        # Сохранение в базу данных
        await self.save_reminder_to_database(
            user_id=user.id,
            job_id=job_id,
            reminder_text=reminder['text'],
            reminder_time=reminder['time'],
            frequency=reminder['frequency'],
            frequency_text=reminder['frequency_text'],
            comment=comment
        )
        
        # Планирование напоминания
        reminder_data = {
            'job_id': job_id,
            'text': reminder['text'],
            'time': reminder['time'],
            'frequency': reminder['frequency'],
            'frequency_text': reminder['frequency_text'],
            'comment': comment
        }
        await self.schedule_reminder(user.id, reminder_data)
        
        # Формирование сообщения о успешном создании
        timezone = await self.get_user_timezone(user.id)
        message = (
            "✅ Напоминание успешно создано!\n\n"
            f"📝 Текст: {reminder['text']}\n"
            f"⏰ Время: {reminder['time']} ({timezone})\n"
            f"🔄 Периодичность: {reminder['frequency_text']}\n"
            f"💬 Комментарий: {self._format_comment(comment)}"
        )
        
        await update.message.reply_text(
            message,
            reply_markup=self.main_menu_keyboard
        )
        
        # Очистка временных данных
        context.user_data.clear()
        return ConversationHandler.END

    async def skip_comment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Пропуск добавления комментария"""
        return await self.set_reminder_comment(update, context)

    async def cancel_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Отмена создания напоминания"""
        context.user_data.clear()
        await update.message.reply_text(
            "Создание напоминания отменено.",
            reply_markup=self.main_menu_keyboard
        )
        return ConversationHandler.END

    def _format_comment(self, comment: Optional[Dict]) -> str:
        """Форматирование комментария для отображения"""
        if not comment:
            return "нет"
        
        if comment['type'] == 'text':
            return f"текст: {comment['content']}"
        elif comment['type'] == 'photo':
            return "фото" + (f" ({comment['caption']})" if comment.get('caption') else "")
        elif comment['type'] == 'document':
            return f"документ: {comment.get('file_name', 'без названия')}" + \
                (f" ({comment['caption']})" if comment.get('caption') else "")
        return "вложение"
   
    async def save_reminder_to_database(
        self,
        user_id: int,
        job_id: str,
        reminder_text: str,
        reminder_time: str,
        frequency: str,
        frequency_text: str,
        comment: Optional[Dict] = None
    ):
        """Сохранение напоминания в базу данных"""
        conn = sqlite3.connect('reminders.db')
        cursor = conn.cursor()
        
        cursor.execute(
            '''
            INSERT INTO reminders (
                user_id, job_id, reminder_text, reminder_time,
                frequency, frequency_text, comment_type,
                comment_text, comment_file_id, comment_file_name
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                user_id, job_id, reminder_text, reminder_time,
                frequency, frequency_text,
                comment['type'] if comment else None,
                comment.get('content') if comment else None,
                comment.get('file_id') if comment else None,
                comment.get('file_name') if comment else None
            )
        )
        
        conn.commit()
        conn.close()

    async def schedule_reminder(self, user_id: int, reminder: Dict):
        """Планирование напоминания в scheduler"""
        try:
            hour, minute = map(int, reminder['time'].split(':'))
            timezone = pytz.timezone(await self.get_user_timezone(user_id))
            
            if reminder['frequency'] == 'once':
                now = datetime.now(timezone)
                reminder_time = timezone.localize(datetime.combine(now.date(), time(hour, minute)))
                if reminder_time < now:
                    reminder_time += timedelta(days=1)
                trigger = DateTrigger(reminder_time)
            else:
                frequency_map = {
                    'daily': '*',
                    'weekly': 'sun-sat',
                    'weekdays': 'mon-fri',
                    'mon_wed_fri': 'mon,wed,fri',
                    'tue_thu': 'tue,thu'
                }
                trigger = CronTrigger(
                    hour=hour,
                    minute=minute,
                    day_of_week=frequency_map[reminder['frequency']],
                    timezone=timezone
                )
            
            self.scheduler.add_job(
                self.send_reminder,
                trigger=trigger,
                args=[user_id, reminder['job_id']],
                id=reminder['job_id'],
                replace_existing=True,
                misfire_grace_time=300
            )
        except Exception as e:
            logger.error(f"Ошибка планирования напоминания: {e}")
        
    async def send_reminder(self, user_id: int, job_id: str):
        """Отправляет напоминание пользователю"""
        try:
            # Получаем напоминание из базы данных
            connection = sqlite3.connect('reminders.db')
            cursor = connection.cursor()

            cursor.execute(
                'SELECT reminder_text, reminder_time, frequency, comment_type, comment_text, comment_file_id FROM reminders WHERE job_id = ?',
                (job_id,)
            )
            reminder = cursor.fetchone()
            connection.close()

            if not reminder:
                logger.error(f"Напоминание {job_id} не найдено в базе данных")
                return

            text, time_str, frequency, comment_type, comment_text, comment_file_id = reminder

            # Формируем сообщение
            timezone = pytz.timezone(await self.get_user_timezone(user_id))
            current_time = datetime.now(timezone).strftime('%H:%M %Z')
            message = f"⏰ Напоминание: {text}\n🕒 Ваше время: {current_time}"

            # Отправляем сообщение в зависимости от типа комментария
            if comment_type:
                comment = {
                    'type': comment_type,
                    'content': comment_text,
                    'file_id': comment_file_id
                }

                if comment['type'] == 'text':
                    await self.application.bot.send_message(
                        chat_id=user_id,
                        text=f"{message}\n\n💬 Комментарий: {comment['content']}"
                    )
                elif comment['type'] == 'photo':
                    await self.application.bot.send_photo(
                        chat_id=user_id,
                        photo=comment['file_id'],
                        caption=message + (f"\n\n💬 {comment['content']}" if comment['content'] else "")
                    )
                elif comment['type'] == 'document':
                    await self.application.bot.send_document(
                        chat_id=user_id,
                        document=comment['file_id'],
                        caption=message + (f"\n\n💬 {comment['content']}" if comment['content'] else "")
                    )
            else:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text=message
                )

            # Если напоминание одноразовое - удаляем его
            if frequency == 'once':
                await self.delete_reminder_from_database(job_id)

            logger.info(f"Напоминание {job_id} отправлено пользователю {user_id}")

        except telegram.error.Forbidden:
            logger.error(f"Пользователь {user_id} заблокировал бота")
            await self.delete_reminder_from_database(job_id)
        except Exception as error:
            logger.error(f"Ошибка при отправке напоминания {job_id}: {error}")

    async def show_reminders_list(self, update: Update):
        """Показывает список всех напоминаний пользователя"""
        user = update.effective_user
        reminders = await self.get_user_reminders(user.id)

        if not reminders:
            await update.message.reply_text(
                "У вас нет активных напоминаний.",
                reply_markup=self.main_menu_keyboard
            )
            return

        message = "📋 Ваши напоминания:\n\n"
        for i, reminder in enumerate(reminders, 1):
            message += (
                f"{i}. {reminder['text']}\n"
                f"   ⏰ Время: {reminder['time']}\n"
                f"   🔄 Периодичность: {reminder['frequency_text']}\n"
                f"   🆔 ID: {reminder['job_id']}\n\n"
            )

        await update.message.reply_text(
            message,
            reply_markup=self.main_menu_keyboard
        )

    async def show_delete_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает меню для удаления напоминаний"""
        user = update.effective_user
        reminders = await self.get_user_reminders(user.id)
        
        if not reminders:
            await update.message.reply_text(
                "У вас нет активных напоминаний.",
                reply_markup=self.main_menu_keyboard
            )
            return
        
        # Создаем клавиатуру с кнопками удаления
        keyboard = []
        for reminder in reminders:
            btn_text = f"{reminder['time']} - {reminder['text'][:20]}..."
            keyboard.append([InlineKeyboardButton(
                f"❌ {btn_text}", 
                callback_data=f"delete_{reminder['job_id']}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="delete_cancel")])
        
        await update.message.reply_text(
            "Выберите напоминание для удаления:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def handle_delete_reminder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает выбор напоминания для удаления"""
        query = update.callback_query
        await query.answer()
        
        if query.data == "delete_cancel":
            await query.message.delete()
            await query.message.reply_text(
                "Удаление отменено.",
                reply_markup=self.main_menu_keyboard
            )
            return
        
        if query.data.startswith("delete_"):
            job_id = query.data[7:]  # Убираем префикс "delete_"
            
            # Удаляем из базы данных
            await self.delete_reminder_from_database(job_id)
            
            # Удаляем из планировщика
            try:
                self.scheduler.remove_job(job_id)
            except Exception as e:
                logger.warning(f"Не удалось удалить задачу из планировщика: {e}")
            
            await query.message.delete()
            await query.message.reply_text(
                f"Напоминание успешно удалено! (ID: {job_id})",
                reply_markup=self.main_menu_keyboard
            )

    async def delete_reminder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Удалить выбранное напоминание"""
        user = update.effective_user
        job_id = update.message.text.replace("❌ Удалить ", "").strip()
        
        # Удаление из базы данных
        await self.delete_reminder_from_database(job_id)
        
        # Удаление из планировщика
        try:
            self.scheduler.remove_job(job_id)
        except:
            pass
        
        await update.message.reply_text(
            f"Напоминание {job_id} успешно удалено.",
            reply_markup=self.main_menu_keyboard
        )

    # Методы работы с часовыми поясами
    async def show_timezone_menu(self, update: Update):
        """Показывает меню выбора часового пояса"""
        keyboard = [
            [InlineKeyboardButton("Москва (MSK)", callback_data='Europe/Moscow')],
            [InlineKeyboardButton("Киев (EET)", callback_data='Europe/Kiev')],
            [InlineKeyboardButton("Лондон (GMT)", callback_data='Europe/London')],
            [InlineKeyboardButton("Нью-Йорк (EST)", callback_data='America/New_York')]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "Выберите ваш часовой пояс:",
            reply_markup=reply_markup
        )

    async def handle_timezone_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает выбор часового пояса"""
        query = update.callback_query
        await query.answer()

        user = query.from_user
        timezone = query.data

        await self.set_user_timezone(user.id, timezone)

        await query.edit_message_text(
            f"Часовой пояс изменен на: {timezone}",
            reply_markup=self.main_menu_keyboard
        )

    # Тестовые функции
    async def send_test_reminder(self, update: Update):
        """Отправляет тестовое напоминание"""
        user = update.effective_user
        timezone = await self.get_user_timezone(user.id)

        # Создаем тестовое напоминание
        job_id = f"test_{user.id}_{datetime.now().timestamp()}"
        reminder_data = {
            'job_id': job_id,
            'text': "ТЕСТОВОЕ НАПОМИНАНИЕ",
            'time': (datetime.now(pytz.timezone(timezone)) + timedelta(minutes=1)).strftime('%H:%M'),
            'frequency': 'once',
            'frequency_text': 'Один раз',
            'comment': None
        }

        # Сохраняем в базу данных
        await self.save_reminder_to_database(
            user_id=user.id,
            job_id=job_id,
            reminder_text=reminder_data['text'],
            reminder_time=reminder_data['time'],
            frequency=reminder_data['frequency'],
            frequency_text=reminder_data['frequency_text'],
            comment=None
        )

        # Планируем
        await self.schedule_reminder(user.id, reminder_data)

        await update.message.reply_text(
            f"Тестовое напоминание будет отправлено через 1 минуту.\n"
            f"Часовой пояс: {timezone}",
            reply_markup=self.main_menu_keyboard
        )

    # Обработчики ошибок
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает ошибки в боте"""
        logger.error("Ошибка в обработчике:", exc_info=context.error)

        if update and update.message:
            await update.message.reply_text(
                "Произошла ошибка. Пожалуйста, попробуйте позже.",
                reply_markup=self.main_menu_keyboard
            )


    async def _load_reminders_from_database(self):
        """Загрузка всех напоминаний из базы данных"""
        logger.info("Загрузка напоминаний из базы данных...")
        
        try:
            conn = sqlite3.connect('reminders.db')
            cursor = conn.cursor()
            cursor.execute('SELECT user_id, job_id, reminder_text, reminder_time, frequency FROM reminders')
            reminders = cursor.fetchall()
            
            loaded_count = 0
            
            for reminder in reminders:
                try:
                    user_id, job_id, text, time_str, frequency = reminder
                    
                    # Создание триггера для напоминания
                    hour, minute = map(int, time_str.split(':'))
                    timezone = pytz.timezone(await self.get_user_timezone(user_id))
                    
                    if frequency == 'once':
                        now = datetime.now(timezone)
                        reminder_time = timezone.localize(datetime.combine(now.date(), time(hour, minute)))
                        if reminder_time < now:
                            reminder_time += timedelta(days=1)
                        trigger = DateTrigger(reminder_time)
                    else:
                        frequency_map = {
                            'daily': '*',
                            'weekly': 'sun-sat',
                            'weekdays': 'mon-fri',
                            'mon_wed_fri': 'mon,wed,fri',
                            'tue_thu': 'tue,thu'
                        }
                        trigger = CronTrigger(
                            hour=hour,
                            minute=minute,
                            day_of_week=frequency_map[frequency],
                            timezone=timezone
                        )
                    
                    # Добавление задачи в планировщик
                    if not self.scheduler.get_job(job_id):
                        self.scheduler.add_job(
                            self.send_reminder,
                            trigger=trigger,
                            args=[user_id, job_id],
                            id=job_id,
                            replace_existing=True,
                            misfire_grace_time=300
                        )
                        loaded_count += 1
                        
                except Exception as e:
                    logger.error(f"Ошибка загрузки напоминания {job_id}: {e}")
            
            logger.info(f"Успешно загружено {loaded_count} напоминаний")
            
        except Exception as e:
            logger.error(f"Ошибка работы с базой данных: {e}")
        finally:
            conn.close()


    async def show_edit_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Показывает меню для выбора напоминания для редактирования"""
        user = update.effective_user
        reminders = await self.get_user_reminders(user.id)
        
        if not reminders:
            await update.message.reply_text(
                "У вас нет напоминаний для редактирования.",
                reply_markup=self.main_menu_keyboard
            )
            return
        
        # Создаем клавиатуру с кнопками редактирования
        keyboard = []
        for reminder in reminders:
            btn_text = f"{reminder['time']} - {reminder['text'][:20]}..."
            keyboard.append([InlineKeyboardButton(
                f"✏️ {btn_text}", 
                callback_data=f"edit_{reminder['job_id']}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="edit_cancel")])
        
        await update.message.reply_text(
            "Выберите напоминание для редактирования:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def handle_edit_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает выбор напоминания для редактирования"""
        query = update.callback_query
        await query.answer()
        
        if query.data == "edit_cancel":
            await query.message.delete()
            await query.message.reply_text(
                "Редактирование отменено.",
                reply_markup=self.main_menu_keyboard
            )
            return
        
        if query.data.startswith("edit_"):
            job_id = query.data[5:]  # Убираем префикс "edit_"
            
            # Получаем данные напоминания
            reminder = await self.get_reminder_by_id(job_id)
            
            if not reminder:
                await query.message.reply_text(
                    "Напоминание не найдено!",
                    reply_markup=self.main_menu_keyboard
                )
                return
            
            # Сохраняем ID редактируемого напоминания
            context.user_data['editing_job_id'] = job_id
            
            # Показываем меню выбора поля для редактирования
            keyboard = [
                [InlineKeyboardButton("✏️ Текст", callback_data=f"editfield_text_{job_id}")],
                [InlineKeyboardButton("⏰ Время", callback_data=f"editfield_time_{job_id}")],
                [InlineKeyboardButton("🔄 Периодичность", callback_data=f"editfield_freq_{job_id}")],
                [InlineKeyboardButton("💬 Комментарий", callback_data=f"editfield_comment_{job_id}")],
                [InlineKeyboardButton("🔙 Назад", callback_data="editfield_cancel")]
            ]
            
            await query.message.edit_text(
                f"Выберите что редактировать в напоминании:\n\n"
                f"📝 {reminder['text']}\n"
                f"⏰ {reminder['time']} ({reminder['frequency_text']})\n"
                f"💬 {self._format_comment(reminder)}",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )


    async def get_reminder_by_id(self, job_id: str) -> Optional[Dict]:
        """Получает напоминание по его ID"""
        connection = sqlite3.connect('reminders.db')
        cursor = connection.cursor()
        
        cursor.execute(
            '''SELECT 
                job_id, 
                reminder_text, 
                reminder_time, 
                frequency, 
                frequency_text,
                comment_type,
                comment_text,
                comment_file_name
            FROM reminders 
            WHERE job_id = ?''',
            (job_id,)
        )
        
        row = cursor.fetchone()
        connection.close()
        
        if not row:
            return None
        
        return {
            'job_id': row[0],
            'text': row[1],
            'time': row[2],
            'frequency': row[3],
            'frequency_text': row[4],
            'comment_type': row[5],
            'comment_text': row[6],
            'comment_file_name': row[7]
        }

    async def handle_edit_field_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает выбор поля для редактирования"""
        query = update.callback_query
        await query.answer()
        
        if query.data == "editfield_cancel":
            await query.message.delete()
            await self.show_edit_menu(query.message, context)
            return
        
        if query.data.startswith("editfield_"):
            parts = query.data.split('_')
            field = parts[1]
            job_id = parts[2]
            
            context.user_data['editing_field'] = field
            context.user_data['editing_job_id'] = job_id
            
            if field == "text":
                await query.message.edit_text(
                    "Введите новый текст напоминания:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Назад", callback_data="edit_back")]
                    ])
                )
                return SETTING_REMINDER_TEXT
            
            elif field == "time":
                await query.message.edit_text(
                    "Введите новое время в формате ЧЧ:ММ:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Назад", callback_data="edit_back")]
                    ])
                )
                return SETTING_REMINDER_TIME
            
            elif field == "freq":
                keyboard = [
                    [InlineKeyboardButton("Ежедневно", callback_data='daily')],
                    [InlineKeyboardButton("Еженедельно", callback_data='weekly')],
                    [InlineKeyboardButton("По будням (Пн-Пт)", callback_data='weekdays')],
                    [InlineKeyboardButton("Пн, Ср, Пт", callback_data='mon_wed_fri')],
                    [InlineKeyboardButton("Вт, Чт", callback_data='tue_thu')],
                    [InlineKeyboardButton("🔙 Назад", callback_data="edit_back")]
                ]
                await query.message.edit_text(
                    "Выберите новую периодичность:",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return SETTING_REMINDER_FREQUENCY
            
            elif field == "comment":
                await query.message.edit_text(
                    "Отправьте новый комментарий (текст, фото или файл):",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("❌ Удалить комментарий", callback_data="comment_delete")],
                        [InlineKeyboardButton("🔙 Назад", callback_data="edit_back")]
                    ])
                )
                return SETTING_REMINDER_COMMENT

    async def run(self):
        """Основной метод запуска бота"""
        if self._is_running:
            raise RuntimeError("Бот уже запущен!")
            
        self._is_running = True
        
        try:
            # Инициализация приложения Telegram
            self.application = Application.builder() \
                .token(os.getenv("TELEGRAM_BOT_TOKEN")) \
                .build()

            # Загрузка напоминаний из базы данных
            await self._load_reminders_from_database()

            # Настройка всех обработчиков команд
            self._setup_handlers()

            # Запуск планировщика
            async with self._scheduler_lock:
                if not self.scheduler.running:
                    self.scheduler.start()
                    logger.info("Планировщик напоминаний запущен")

            # Инициализация и запуск бота
            await self.application.initialize()
            await self.application.start()
            
            # Запуск long-polling
            await self.application.updater.start_polling(
                drop_pending_updates=True,
                timeout=20,
                connect_timeout=10
            )
            
            logger.info("Бот успешно запущен и готов к работе")
            
            # Основной цикл работы
            while self._is_running:
                await asyncio.sleep(5)
                
                # Периодическая проверка состояния планировщика
                if not self.scheduler.running:
                    logger.warning("Планировщик остановлен, перезапуск...")
                    try:
                        async with self._scheduler_lock:
                            self.scheduler.start()
                    except Exception as e:
                        logger.error(f"Ошибка перезапуска планировщика: {e}")

        except Exception as e:
            logger.critical(f"Критическая ошибка: {e}", exc_info=True)
        finally:
            await self._safe_shutdown()

    

    async def _safe_shutdown(self):
        """Безопасное выключение бота"""
        logger.info("Starting safe shutdown...")
        self._is_running = False
        
        # Остановка приложения
        if self.application:
            try:
                if self.application.running:
                    await self.application.stop()
                    await self.application.shutdown()
            except Exception as e:
                logger.error(f"Application shutdown error: {e}")

        # Остановка планировщика
        async with self._scheduler_lock:
            if hasattr(self, 'scheduler'):
                try:
                    if self.scheduler.running:
                        self.scheduler.shutdown(wait=False)
                except Exception as e:
                    logger.error(f"Scheduler shutdown error: {e}")
        
        logger.info("Shutdown completed")

    async def ping(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды для проверки активности бота"""
        await update.message.reply_text("🟢 Бот активен")

    async def get_user_reminders(self, user_id: int) -> List[Dict]:
        """Получает все напоминания пользователя из базы данных с информацией о комментариях"""
        connection = sqlite3.connect('reminders.db')
        cursor = connection.cursor()

        cursor.execute(
            '''SELECT 
                job_id, 
                reminder_text, 
                reminder_time, 
                frequency, 
                frequency_text,
                comment_type,
                comment_text,
                comment_file_name
            FROM reminders 
            WHERE user_id = ?''',
            (user_id,)
        )
        
        reminders = []
        for row in cursor.fetchall():
            reminders.append({
                'job_id': row[0],
                'text': row[1],
                'time': row[2],
                'frequency': row[3],
                'frequency_text': row[4],
                'comment_type': row[5],
                'comment_text': row[6],
                'comment_file_name': row[7]
            })
        
        connection.close()
        return reminders

    async def delete_reminder_from_database(self, job_id: str):
        """Удаляет напоминание из базы данных"""
        connection = sqlite3.connect('reminders.db')
        cursor = connection.cursor()

        cursor.execute(
            'DELETE FROM reminders WHERE job_id = ?',
            (job_id,)
        )
        
        connection.commit()
        connection.close()

    async def add_reminder_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /add для создания напоминания"""
        return await self.start_reminder_creation(update, context)

    async def list_reminders_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /list для показа списка напоминаний"""
        await self.list_reminders(update)

    async def delete_reminder_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /delete для удаления напоминания"""
        if not context.args:
            await update.message.reply_text(
                "Пожалуйста, укажите ID напоминания. Например: /delete rem_123456789",
                reply_markup=self.main_menu_keyboard
            )
            return
        
        job_id = context.args[0]
        await self.delete_reminder_from_database(job_id)
        
        try:
            self.scheduler.remove_job(job_id)
        except Exception as e:
            logger.warning(f"Не удалось удалить задачу из планировщика: {e}")
        
        await update.message.reply_text(
            f"Напоминание {job_id} успешно удалено.",
            reply_markup=self.main_menu_keyboard
        )

    async def timezone_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /timezone для изменения часового пояса"""
        await self.show_timezone_menu(update)

    async def test_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /test для тестового напоминания"""
        await self.send_test_reminder(update)

    async def handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик для текстовых сообщений, не попавших в другие обработчики"""
        text = update.message.text
        
        if text.startswith('❌ Удалить '):
            # Этот случай уже обрабатывается другим обработчиком
            return
        
        # Проверяем, возможно пользователь начал диалог, но не завершил его
        if 'reminder' in context.user_data:
            await update.message.reply_text(
                "Кажется, вы начали создание напоминания. Пожалуйста, завершите его или отмените.",
                reply_markup=self.cancel_keyboard
            )
            return
        
        await update.message.reply_text(
            "Я не понимаю эту команду. Пожалуйста, используйте кнопки меню или команды из /help",
            reply_markup=self.main_menu_keyboard
        )

    def _setup_handlers(self):
        """Настройка всех обработчиков команд и сообщений"""
        # ConversationHandler для создания одиночного напоминания
        single_reminder_conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler('add', self.add_reminder_command),
                MessageHandler(filters.Regex(r'^➕ Добавить напоминание$'), self.start_reminder_creation)
            ],
            states={
                SETTING_REMINDER_TEXT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_reminder_text)
                ],
                SETTING_REMINDER_TIME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_reminder_time)
                ],
                SETTING_REMINDER_FREQUENCY: [
                    CallbackQueryHandler(self.set_reminder_frequency, 
                                    pattern='^(once|daily|weekly|weekdays|mon_wed_fri|tue_thu)$')
                ],
                SETTING_REMINDER_COMMENT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_reminder_comment),
                    MessageHandler(filters.PHOTO, self.set_reminder_comment),
                    MessageHandler(filters.Document.ALL, self.set_reminder_comment),
                    MessageHandler(filters.Regex(r'^Пропустить$'), self.skip_comment)
                ]
            },
            fallbacks=[
                CommandHandler('cancel', self.cancel_conversation),
                MessageHandler(filters.Regex(r'^🔙 Отмена$'), self.cancel_conversation)
            ],
            allow_reentry=True
        )

        # ConversationHandler для массового добавления напоминаний
        batch_reminders_conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler('batch', self.start_batch_reminders),
                MessageHandler(filters.Regex(r'^📝 Добавить несколько$'), self.start_batch_reminders)
            ],
            states={
                SETTING_BATCH_REMINDERS: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.parse_batch_reminders)
                ],
                SETTING_BATCH_FREQUENCY: [
                    CallbackQueryHandler(self.set_batch_frequency,
                                    pattern='^(daily|weekly|weekdays|mon_wed_fri|tue_thu)$')
                ]
            },
            fallbacks=[
                CommandHandler('cancel', self.cancel_conversation),
                MessageHandler(filters.Regex(r'^🔙 Отмена$'), self.cancel_conversation)
            ],
            allow_reentry=True
        )

        # ConversationHandler для редактирования напоминаний
        edit_reminder_conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler('edit', self.start_edit_reminder),
                MessageHandler(filters.Regex(r'^✏️ Редактировать$'), self.start_edit_reminder)
            ],
            states={
                EDITING_REMINDER: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_reminder)
                ],
                SETTING_REMINDER_COMMENT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.update_reminder_comment),
                    MessageHandler(filters.PHOTO, self.update_reminder_comment),
                    MessageHandler(filters.Document.ALL, self.update_reminder_comment),
                    MessageHandler(filters.Regex(r'^Пропустить$'), self.skip_comment)
                ]
            },
            fallbacks=[
                CommandHandler('cancel', self.cancel_conversation),
                MessageHandler(filters.Regex(r'^🔙 Отмена$'), self.cancel_conversation)
            ],
            allow_reentry=True
        )

        # Регистрация всех обработчиков команд
        self.application.add_handler(CommandHandler('start', self.start_command))
        self.application.add_handler(CommandHandler('help', self.help_command))
        self.application.add_handler(CommandHandler('status', self.status_command))
        self.application.add_handler(CommandHandler('list', self.list_reminders_command))
        self.application.add_handler(CommandHandler('delete', self.delete_reminder_command))
        self.application.add_handler(CommandHandler('timezone', self.timezone_command))
        self.application.add_handler(CommandHandler('test', self.test_command))
        self.application.add_handler(CommandHandler('ping', self.ping))

        # Регистрация ConversationHandler
        self.application.add_handler(single_reminder_conv_handler)
        self.application.add_handler(batch_reminders_conv_handler)
        self.application.add_handler(edit_reminder_conv_handler)

        # Обработчики сообщений
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_main_menu))
        self.application.add_handler(MessageHandler(filters.Regex(r'^❌ Удалить '), self.delete_reminder))

        # Обработчики CallbackQuery
        self.application.add_handler(CallbackQueryHandler(
            self.handle_timezone_selection,
            pattern='^(Europe/.+|America/.+)$'
        ))

        # Обработчик ошибок
        self.application.add_error_handler(self.error_handler)

        self.application.add_handler(CallbackQueryHandler(
            self.handle_delete_reminder,
            pattern=r'^delete_.*'
        ))

        self.application.add_handler(CallbackQueryHandler(
            self.handle_edit_choice,
            pattern=r'^edit_.*'
        ))
        
        self.application.add_handler(CallbackQueryHandler(
            self.handle_edit_field_choice,
            pattern=r'^editfield_.*'
        ))
            
            
if __name__ == '__main__':
    # Создание и запуск бота
    bot = ReminderBot()
    
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске: {e}")