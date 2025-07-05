import os
import logging
import asyncio
import sqlite3
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Tuple
import pytz
import telegram
from dotenv import load_dotenv
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

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния диалога
(
    SETTING_REMINDER_TEXT,
    SETTING_REMINDER_TIME,
    SETTING_REMINDER_FREQUENCY,
    SETTING_REMINDER_COMMENT
) = range(4)

# Часовой пояс по умолчанию
DEFAULT_TIMEZONE = 'Europe/Moscow'
load_dotenv(".env")


# Инициализация базы данных
def initialize_database():
    """Создает таблицы в базе данных, если они не существуют"""
    connection = sqlite3.connect('reminders.db')
    cursor = connection.cursor()

    # Таблица для хранения напоминаний
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
    )
    ''')

    # Таблица для хранения часовых поясов пользователей
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_timezones (
        user_id INTEGER PRIMARY KEY,
        timezone TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    connection.commit()
    connection.close()


initialize_database()


class ReminderBot:
    def __init__(self):
        """Инициализация бота"""

        if not os.path.exists('reminders.db'):
            open('reminders.db', 'w').close()

        self.application: Optional[Application] = None
        self.scheduler = AsyncIOScheduler(timezone=pytz.timezone(DEFAULT_TIMEZONE))

        # Основное меню
        self.main_menu_keyboard = ReplyKeyboardMarkup(
            [
                [KeyboardButton("➕ Добавить напоминание")],
                [KeyboardButton("📋 Список напоминаний"), KeyboardButton("❌ Удалить напоминание")],
                [KeyboardButton("🌍 Изменить часовой пояс"), KeyboardButton("🔄 Тест напоминания")]
            ],
            resize_keyboard=True
        )

        # Клавиатура для отмены
        self.cancel_keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("🔙 Отмена")]],
            resize_keyboard=True
        )

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
        """Сохраняет напоминание в базу данных"""
        connection = sqlite3.connect('reminders.db')
        cursor = connection.cursor()

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
                comment['content'] if comment and comment['type'] == 'text' else None,
                comment['file_id'] if comment and comment['type'] in ('photo', 'document') else None,
                comment.get('file_name') if comment and comment['type'] == 'document' else None
            )
        )

        connection.commit()
        connection.close()

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

    async def get_user_reminders(self, user_id: int) -> List[Dict]:
        """Получает все напоминания пользователя из базы данных"""
        connection = sqlite3.connect('reminders.db')
        cursor = connection.cursor()

        cursor.execute(
            'SELECT * FROM reminders WHERE user_id = ?',
            (user_id,)
        )
        reminders = cursor.fetchall()
        connection.close()

        result = []
        for reminder in reminders:
            _, job_id, text, time_str, frequency, freq_text, comment_type, comment_text, comment_file_id, comment_file_name, _ = reminder

            comment = None
            if comment_type:
                comment = {
                    'type': comment_type,
                    'content': comment_text,
                    'file_id': comment_file_id,
                    'file_name': comment_file_name
                }

            result.append({
                'job_id': job_id,
                'text': text,
                'time': time_str,
                'frequency': frequency,
                'frequency_text': freq_text,
                'comment': comment
            })

        return result

    async def load_all_reminders(self):
        """Загружает все напоминания из базы данных при запуске бота"""
        connection = sqlite3.connect('reminders.db')
        cursor = connection.cursor()

        cursor.execute('SELECT * FROM reminders')
        all_reminders = cursor.fetchall()
        connection.close()

        for reminder in all_reminders:
            user_id = reminder[0]
            job_id = reminder[1]
            text = reminder[2]
            time_str = reminder[3]
            frequency = reminder[4]
            freq_text = reminder[5]
            comment_type = reminder[6]
            comment_text = reminder[7]
            comment_file_id = reminder[8]
            comment_file_name = reminder[9]

            comment = None
            if comment_type:
                comment = {
                    'type': comment_type,
                    'content': comment_text,
                    'file_id': comment_file_id,
                    'file_name': comment_file_name
                }

            reminder_data = {
                'job_id': job_id,
                'text': text,
                'time': time_str,
                'frequency': frequency,
                'frequency_text': freq_text,
                'comment': comment
            }

            await self.schedule_reminder(user_id, reminder_data)

    # Основные обработчики команд
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        user = update.effective_user
        timezone = await self.get_user_timezone(user.id)

        await update.message.reply_text(
            f"Привет, {user.first_name}!\n"
            f"Я бот для управления напоминаниями.\n"
            f"Текущий часовой пояс: {timezone}\n\n"
            "Выберите действие в меню:",
            reply_markup=self.main_menu_keyboard
        )

    async def handle_main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик главного меню"""
        text = update.message.text

        if text == "➕ Добавить напоминание":
            await update.message.reply_text(
                "Введите текст напоминания:",
                reply_markup=self.cancel_keyboard
            )
            return SETTING_REMINDER_TEXT

        elif text == "📋 Список напоминаний":
            return await self.show_reminders_list(update)

        elif text == "❌ Удалить напоминание":
            return await self.show_delete_menu(update)

        elif text == "🌍 Изменить часовой пояс":
            return await self.show_timezone_menu(update)

        elif text == "🔄 Тест напоминания":
            return await self.send_test_reminder(update)

        else:
            await update.message.reply_text(
                "Неизвестная команда. Используйте кнопки меню.",
                reply_markup=self.main_menu_keyboard
            )

    # Обработчики создания напоминания
    async def set_reminder_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Устанавливает текст напоминания"""
        if update.message.text == "🔙 Отмена":
            await update.message.reply_text(
                "Создание напоминания отменено.",
                reply_markup=self.main_menu_keyboard
            )
            return ConversationHandler.END

        context.user_data['reminder_text'] = update.message.text

        await update.message.reply_text(
            "Введите время напоминания в формате ЧЧ:ММ (например, 14:30):",
            reply_markup=self.cancel_keyboard
        )
        return SETTING_REMINDER_TIME

    async def set_reminder_time(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Устанавливает время напоминания"""
        if update.message.text == "🔙 Отмена":
            await update.message.reply_text(
                "Создание напоминания отменено.",
                reply_markup=self.main_menu_keyboard
            )
            return ConversationHandler.END

        try:
            time_str = update.message.text
            datetime.strptime(time_str, "%H:%M")
            context.user_data['reminder_time'] = time_str

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
        """Устанавливает периодичность напоминания"""
        query = update.callback_query
        await query.answer()

        frequency_mapping = {
            'once': "Один раз",
            'daily': "Ежедневно",
            'weekly': "Еженедельно",
            'weekdays': "По будням (Пн-Пт)",
            'mon_wed_fri': "Пн, Ср, Пт",
            'tue_thu': "Вт, Чт"
        }

        frequency = query.data
        context.user_data['frequency'] = frequency
        context.user_data['frequency_text'] = frequency_mapping[frequency]

        keyboard = ReplyKeyboardMarkup(
            [
                [KeyboardButton("Пропустить")],
                [KeyboardButton("🔙 Отмена")]
            ],
            resize_keyboard=True
        )

        await query.edit_message_text(
            f"Периодичность: {frequency_mapping[frequency]}\n\n"
            "Вы можете прикрепить комментарий (текст, фото или файл) или нажмите 'Пропустить'."
        )
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="Ожидание комментария...",
            reply_markup=keyboard
        )
        return SETTING_REMINDER_COMMENT

    async def set_reminder_comment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Устанавливает комментарий к напоминанию"""
        user = update.effective_user

        if update.message.text == "🔙 Отмена":
            await update.message.reply_text(
                "Создание напоминания отменено.",
                reply_markup=self.main_menu_keyboard
            )
            return ConversationHandler.END

        comment = None
        if update.message.text != "Пропустить":
            if update.message.photo:
                photo: PhotoSize = update.message.photo[-1]
                comment = {
                    'type': 'photo',
                    'file_id': photo.file_id,
                    'content': update.message.caption
                }
            elif update.message.document:
                document: Document = update.message.document
                comment = {
                    'type': 'document',
                    'file_id': document.file_id,
                    'file_name': document.file_name,
                    'content': update.message.caption
                }
            else:
                comment = {
                    'type': 'text',
                    'content': update.message.text
                }

        # Создаем уникальный ID для задания
        job_id = f"reminder_{user.id}_{datetime.now().timestamp()}"

        # Сохраняем напоминание в базу данных
        await self.save_reminder_to_database(
            user_id=user.id,
            job_id=job_id,
            reminder_text=context.user_data['reminder_text'],
            reminder_time=context.user_data['reminder_time'],
            frequency=context.user_data['frequency'],
            frequency_text=context.user_data['frequency_text'],
            comment=comment
        )

        # Планируем напоминание
        reminder_data = {
            'job_id': job_id,
            'text': context.user_data['reminder_text'],
            'time': context.user_data['reminder_time'],
            'frequency': context.user_data['frequency'],
            'frequency_text': context.user_data['frequency_text'],
            'comment': comment
        }

        await self.schedule_reminder(user.id, reminder_data)

        # Формируем сообщение о успешном создании
        timezone = await self.get_user_timezone(user.id)
        message = (
            "✅ Напоминание успешно создано!\n\n"
            f"📝 Текст: {context.user_data['reminder_text']}\n"
            f"⏰ Время: {context.user_data['reminder_time']} ({timezone})\n"
            f"🔄 Периодичность: {context.user_data['frequency_text']}\n"
            f"💬 Комментарий: {self.format_comment(comment)}"
        )

        await update.message.reply_text(
            message,
            reply_markup=self.main_menu_keyboard
        )

        # Очищаем временные данные
        context.user_data.clear()
        return ConversationHandler.END

    def format_comment(self, comment: Optional[Dict]) -> str:
        """Форматирует комментарий для отображения"""
        if not comment:
            return "нет"

        if comment['type'] == 'text':
            return f"текст: {comment['content']}"
        elif comment['type'] == 'photo':
            return "фото" + (f" ({comment['content']})" if comment['content'] else "")
        elif comment['type'] == 'document':
            return f"документ: {comment.get('file_name', 'без названия')}" + \
                (f" ({comment['content']})" if comment['content'] else "")
        return "вложение"

    # Методы работы с напоминаниями
    async def schedule_reminder(self, user_id: int, reminder: Dict):
        """Планирует напоминание с помощью APScheduler"""
        try:
            hour, minute = map(int, reminder['time'].split(':'))
            timezone = pytz.timezone(await self.get_user_timezone(user_id))

            if reminder['frequency'] == 'once':
                # Для одноразовых напоминаний
                now = datetime.now(timezone)
                reminder_time = timezone.localize(
                    datetime.combine(now.date(), time(hour, minute))
                )

                if reminder_time < now:
                    reminder_time += timedelta(days=1)

                trigger = DateTrigger(reminder_time)
            else:
                # Для повторяющихся напоминаний
                frequency_map = {
                    'daily': '*',  # Каждый день
                    'weekly': 'sun-sat',  # Каждую неделю
                    'weekdays': 'mon-fri',  # По будням
                    'mon_wed_fri': 'mon,wed,fri',  # Пн, Ср, Пт
                    'tue_thu': 'tue,thu'  # Вт, Чт
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
                replace_existing=True
            )

        except Exception as error:
            logger.error(f"Ошибка при планировании напоминания: {error}")

    async def send_reminder(self, user_id: int, job_id: str):
        """Отправляет напоминание пользователю"""
        try:
            # Получаем напоминание из базы данных
            connection = sqlite3.connect('reminders.db')
            cursor = connection.cursor()

            cursor.execute(
                'SELECT * FROM reminders WHERE job_id = ?',
                (job_id,)
            )
            reminder = cursor.fetchone()
            connection.close()

            if not reminder:
                logger.error(f"Напоминание {job_id} не найдено в базе данных")
                return

            _, _, text, time_str, frequency, freq_text, comment_type, comment_text, comment_file_id, comment_file_name, _ = reminder

            # Формируем сообщение
            timezone = pytz.timezone(await self.get_user_timezone(user_id))
            current_time = datetime.now(timezone).strftime('%H:%M %Z')
            message = f"⏰ Напоминание: {text}\n🕒 Ваше время: {current_time}"

            # Отправляем сообщение в зависимости от типа комментария
            if comment_type:
                comment = {
                    'type': comment_type,
                    'content': comment_text,
                    'file_id': comment_file_id,
                    'file_name': comment_file_name
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

    async def show_delete_menu(self, update: Update):
        """Показывает меню для удаления напоминаний"""
        user = update.effective_user
        reminders = await self.get_user_reminders(user.id)

        if not reminders:
            await update.message.reply_text(
                "У вас нет активных напоминаний.",
                reply_markup=self.main_menu_keyboard
            )
            return

        keyboard = []
        for reminder in reminders:
            keyboard.append(
                [KeyboardButton(f"❌ Удалить {reminder['job_id']}")]
            )

        keyboard.append([KeyboardButton("🔙 Назад")])

        await update.message.reply_text(
            "Выберите напоминание для удаления:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )

    async def delete_reminder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Удаляет выбранное напоминание"""
        user = update.effective_user
        job_id = update.message.text.replace("❌ Удалить ", "").strip()

        # Удаляем из базы данных
        await self.delete_reminder_from_database(job_id)

        # Удаляем из планировщика
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

        await update.message.reply_text(
            "Выберите ваш часовой пояс:",
            reply_markup=InlineKeyboardMarkup(keyboard)
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

    async def run(self):
        """Запускает бота с правильным управлением жизненным циклом"""
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            raise ValueError("Не указан TELEGRAM_BOT_TOKEN в переменных окружения")

        # Создаем и инициализируем приложение
        self.application = Application.builder().token(token).build()
        await self.application.initialize()  # Явная инициализация

        # Настройка обработчиков
        self.setup_handlers()

        # Загружаем напоминания
        await self.load_all_reminders()

        # Запускаем планировщик
        self.scheduler.start()

        try:
            # Запускаем polling
            logger.info("Бот запущен")
            await self.application.start()
            async with self.application:
                await self.application.updater.start_polling()
                while True:
                    await asyncio.sleep(3600)

        except (KeyboardInterrupt, SystemExit):
            logger.info("Получен сигнал завершения...")
        except Exception as e:
            logger.error(f"Ошибка в основном цикле: {e}")
        finally:
            await self.shutdown()

    async def shutdown(self):
        logger.info("Остановка бота...")
        
        if hasattr(self, 'scheduler') and self.scheduler.running:
            self.scheduler.shutdown()
        
        if hasattr(self, 'application'):
            try:
                if self.application.running:
                    await self.application.stop()
                    await self.application.shutdown()
            except Exception as e:
                logger.error(f"Ошибка при остановке: {e}")
        
        logger.info("Бот успешно остановлен")

    async def ping(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды для проверки активности бота"""
        await update.message.reply_text("🟢 Бот активен")

    def setup_handlers(self):
        """Настройка всех обработчиков команд и сообщений"""
        # Создаем ConversationHandler для добавления напоминаний
        self.application.add_handler(CommandHandler('ping', self.ping))

        conv_handler = ConversationHandler(
            entry_points=[
                MessageHandler(filters.Regex(r'^➕ Добавить напоминание$'), self.handle_main_menu)
            ],
            states={
                SETTING_REMINDER_TEXT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_reminder_text)
                ],
                SETTING_REMINDER_TIME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_reminder_time)
                ],
                SETTING_REMINDER_FREQUENCY: [
                    CallbackQueryHandler(self.set_reminder_frequency)
                ],
                SETTING_REMINDER_COMMENT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_reminder_comment),
                    MessageHandler(filters.PHOTO, self.set_reminder_comment),
                    MessageHandler(filters.Document.ALL, self.set_reminder_comment),
                    MessageHandler(filters.Regex(r'^Пропустить$'), self.set_reminder_comment)
                ],
            },
            fallbacks=[
                MessageHandler(filters.Regex(r'^🔙 Отмена$'), self.set_reminder_comment)
            ]
        )

        # Основные обработчики
        self.application.add_handler(CommandHandler('start', self.start_command))
        self.application.add_handler(conv_handler)
        self.application.add_handler(MessageHandler(filters.TEXT, self.handle_main_menu))
        self.application.add_handler(CallbackQueryHandler(self.handle_timezone_selection))

        # Обработчик для кнопок удаления
        self.application.add_handler(MessageHandler(
            filters.Regex(r'^❌ Удалить '),
            self.delete_reminder
        ))


if __name__ == '__main__':
    bot = ReminderBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
