import datetime
import os
import telebot
import requests
import re

from collections import defaultdict
from dotenv import load_dotenv
from telebot import custom_filters, types
from telebot.storage import StateMemoryStorage
from telebot.handler_backends import StatesGroup, State

from otp_sum_checker import otpchksum

load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
LOGIN_URL = "https://org.fa.ru/login/"
SCHEDULE_URL = "https://org.fa.ru/ruzapi/schedule/group"
PROFILE_URL = "https://org.fa.ru/bitrix/vuz/api/profile/"
DISCIPLINES_LIST_URL = "https://org.fa.ru/bitrix/vuz/api/atlog/get_journals_by_contingent"
DISCIPLINE_URL = "https://org.fa.ru/bitrix/vuz/api/atlog/get_journal"
SEARCH_URL = 'https://org.fa.ru/ruzapi/search'

state_storage = StateMemoryStorage()

bot = telebot.TeleBot(BOT_TOKEN, state_storage=state_storage)

bot.set_my_commands([
    telebot.types.BotCommand("/start", "Запустить бота"),
    telebot.types.BotCommand("/menu", "Главное меню"),
    telebot.types.BotCommand("/login", "Войти в аккаунт"),
    telebot.types.BotCommand("/logout", "Выйти из аккаунта"),
    telebot.types.BotCommand("/schedule_group", "Показать расписание группы"),
    telebot.types.BotCommand("/schedule_teacher", "Показать расписание преподавателя"),
    telebot.types.BotCommand("/disciplines", "Список баллов и посещений"),
])


class Cache:
    def __init__(self, ttl=300):
        self.ttl = ttl
        self.cache = {}

    def get(self, key):
        if key in self.cache:
            value, timestamp = self.cache[key]
            if datetime.datetime.now() - timestamp < datetime.timedelta(seconds=self.ttl):
                return value
            else:
                del self.cache[key]
        return None

    def set(self, key, value):
        self.cache[key] = (value, datetime.datetime.now())


class UserStates(StatesGroup):
    waiting_login = State()
    waiting_password = State()
    waiting_code = State()
    waiting_group = State()
    waiting_teacher = State()
    final = State()


schedule_cache = Cache(ttl=3600)


def get_user_session(user_id, chat_id):
    with bot.retrieve_data(user_id, chat_id) as data:
        if 'session' not in data:
            data['session'] = requests.Session()
        return data['session']


def split_long_message(text):
    parts = []
    while text:
        if len(text) <= 4096:
            parts.append(text)
            break

        split_index = text.rfind('\n', 0, 4096)
        if split_index == -1:
            split_index = 4096

        parts.append(text[:split_index])
        text = text[split_index:].lstrip()

    return parts


def check_authorization(user_id, chat_id):
    user_session = get_user_session(user_id, chat_id)
    response = user_session.get(
        url=PROFILE_URL
    )
    pattern = r"<title>Unauthorized</title>"
    if re.findall(pattern, response.text):
        return False
    return True


def get_current_semester():
    today = datetime.date.today()
    year = today.year

    if 9 <= today.month <= 12:
        start_date = f"{year}-09-01"
        end_date = f"{year + 1}-02-09"

    elif 2 <= today.month <= 8:
        start_date = f"{year}-02-10"
        end_date = f"{year}-08-31"

    else:
        start_date = f"{year - 1}-09-01"
        end_date = f"{year}-02-09"

    return start_date, end_date


def current_quarter(offset=0):
    today = datetime.date.today()
    year = today.year

    if 9 <= today.month <= 10:
        base_year = year
        quarter = 1
    elif today.month == 11 or today.month == 12 or (today.month == 1 and today.day <= 9) or (
            today.month == 2 and today.day <= 9):
        if today.month >= 11:
            base_year = year
        else:
            base_year = year - 1
        quarter = 2
    elif 2 <= today.month <= 3 or (today.month == 4 and today.day < 1):
        base_year = year - 1
        quarter = 3
    else:
        base_year = year - 1
        quarter = 4

    total_quarters = base_year * 4 + (quarter - 1) + offset
    new_year = total_quarters // 4
    new_quarter = total_quarters % 4 + 1

    if new_quarter == 1:
        start_date = f"{new_year}-09-01"
        end_date = f"{new_year}-10-31"
    elif new_quarter == 2:
        start_date = f"{new_year}-11-01"
        end_date = f"{new_year + 1}-02-09"
    elif new_quarter == 3:
        start_date = f"{new_year + 1}-02-10"
        end_date = f"{new_year + 1}-03-31"
    else:
        start_date = f"{new_year + 1}-04-01"
        end_date = f"{new_year + 1}-08-31"

    return start_date, end_date, new_quarter


def login_account(message, user_login, user_password):
    user_session = get_user_session(message.from_user.id, message.chat.id)

    response = user_session.post(
        url=LOGIN_URL,
        data={
            "AUTH_FORM": "Y",
            "TYPE": "AUTH",
            "backurl": "/login/",
            "USER_LOGIN": user_login,
            "USER_PASSWORD": user_password
        },
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Referer": LOGIN_URL,
            "Content-Type": "application/x-www-form-urlencoded"
        },
        allow_redirects=True
    )
    pattern = r"Неверный логин или пароль"
    incorrect_data = re.findall(pattern, response.text)
    if incorrect_data:
        bot.send_message(message.from_user.id, 'Неверный логин или пароль')
        bot.send_message(message.from_user.id, 'Введи свой логин')
        bot.set_state(message.from_user.id, UserStates.waiting_login, message.chat.id)
        with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            data['user_password'] = None
            data['user_login'] = None
        return 'Invalid credentials'

    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['session'] = user_session
    pattern = r"[a-zA-Z0-9]+@[a-zA-Z0-9.-]+\.[a-zA-Z]+"
    user_email = re.search(pattern, response.text).group()
    pattern = r"'bitrix_sessid':'([a-zA-Z0-9]+)'"
    session_id = re.search(pattern, response.text).group(1)

    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['session_id'] = session_id
    bot.send_message(message.chat.id, f"Введи код, отправленный на {user_email}")


def send_long_message(bot, chat_id, text, parse_mode='HTML', reply_markup=None, message_id=None, prev_messages_id=None):
    if prev_messages_id:
        for msg_id in prev_messages_id:
            try:
                bot.delete_message(chat_id, msg_id)
            except Exception:
                pass

    parts = split_long_message(text)
    if message_id and len(parts) == 1:
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=parts[0],
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
            return [message_id]
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e):
                raise e
            return [message_id]

    sent_messages = []
    for i, part in enumerate(parts):
        if i == 0 and message_id:
            try:
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=part,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup if i == len(parts) - 1 else None
                )
                sent_messages.append(message_id)
                continue
            except telebot.apihelper.ApiTelegramException as e:
                if "message is not modified" not in str(e):
                    pass

        msg = bot.send_message(
            chat_id=chat_id,
            text=part,
            parse_mode=parse_mode,
            reply_markup=reply_markup if i == len(parts) - 1 else None
        )
        sent_messages.append(msg.message_id)

    return sent_messages


def create_schedule_keyboard(offset=0):
    markup = types.InlineKeyboardMarkup(row_width=3)

    markup.add(
        types.InlineKeyboardButton(
            "⬅️",
            callback_data=f'schedule_{offset - 1}'
        ),
        types.InlineKeyboardButton(
            "🔄",
            callback_data=f'schedule_{offset}'
        ),
        types.InlineKeyboardButton(
            "➡️",
            callback_data=f'schedule_{offset + 1}'
        )
    )
    return markup


def create_discipline_keyboard(discipline_id, quarter, offset=0):
    markup = types.InlineKeyboardMarkup(row_width=3)
    if quarter % 2:
        markup.add(
            types.InlineKeyboardButton(
                "🔄",
                callback_data=f'discipline_{discipline_id}_{offset}'
            ),
            types.InlineKeyboardButton(
                "➡️",
                callback_data=f'discipline_{discipline_id}_{offset + 1}'
            )
        )
    else:
        markup.add(
            types.InlineKeyboardButton(
                "⬅️",
                callback_data=f'discipline_{discipline_id}_{offset - 1}'
            ),
            types.InlineKeyboardButton(
                "🔄",
                callback_data=f'discipline_{discipline_id}_{offset}'
            ))
    return markup


def get_current_monday():
    today = datetime.datetime.now().date()
    return today - datetime.timedelta(days=today.weekday())


def format_date(date_str):
    return datetime.datetime.strptime(date_str, '%Y.%m.%d').strftime('%d.%m.%Y')


def get_week_dates(offset=0):
    today = datetime.datetime.now().date()
    monday = today - datetime.timedelta(days=today.weekday()) + datetime.timedelta(weeks=offset)
    sunday = monday + datetime.timedelta(days=6)
    return monday, sunday


def show_schedule(bot, chat_id, offset=0, message_id=None):
    with bot.retrieve_data(chat_id, chat_id) as data:
        group_id = data['group_id']
        user_session = data.get('session', requests.Session())
        previous_messages_ids = data.get('last_schedule_messages', [])

    cache_key = f"{group_id}_{offset}"
    cached_data = schedule_cache.get(cache_key)
    start_date, end_date = get_week_dates(offset)

    if cached_data:
        final_message, markup = cached_data
        new_message_ids = send_long_message(
            bot=bot,
            chat_id=chat_id,
            text=final_message,
            parse_mode='HTML',
            reply_markup=markup,
            message_id=message_id,
            prev_messages_id=previous_messages_ids
        )
        with bot.retrieve_data(chat_id, chat_id) as data:
            data['last_schedule_messages'] = new_message_ids[1:]
        return

    response = user_session.post(
        url=f'{SCHEDULE_URL}/{group_id}',
        params={
            "start": start_date,
            "finish": end_date,
            "lng": 1,
        },
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Referer": SCHEDULE_URL,
            "Content-Type": "application/x-www-form-urlencoded"
        },
        allow_redirects=True
    )
    markup = create_schedule_keyboard(offset)
    schedule_data = response.json()

    if not schedule_data:
        text = (f"Расписание на неделю ({get_current_monday() + datetime.timedelta(weeks=offset)}"
                f" - {get_current_monday() + datetime.timedelta(weeks=(1 + offset))}) отсутсвует"),
        if message_id:
            try:
                bot.edit_message_text(chat_id=chat_id,
                                      message_id=message_id,
                                      text=text,
                                      parse_mode='HTML',
                                      reply_markup=markup)
            except telebot.apihelper.ApiTelegramException as e:
                if "message is not modified" in str(e).lower():
                    pass
                else:
                    raise e
        else:
            bot.send_message(chat_id=chat_id,
                             text=text,
                             parse_mode='HTML',
                             reply_markup=markup)
    else:
        lessons_by_date = defaultdict(list)
        for lesson in schedule_data:
            formatted_date = format_date(lesson['date'])
            lessons_by_date[formatted_date].append({
                'discipline': lesson['discipline'],
                'kind_of_work': lesson['kindOfWork'],
                'lecturer': lesson['lecturer'],
                'begin': lesson['beginLesson'],
                'end': lesson['endLesson'],
                'auditorium': lesson['auditorium']
            })
        full_schedule_text = ""
        for date, lessons in sorted(lessons_by_date.items()):
            full_schedule_text += f"\n📅 <b>Дата: {date}</b>\n\n"
            for i, lesson in enumerate(lessons, 1):
                full_schedule_text += (
                    f"{i}. {lesson['discipline']} - {lesson['kind_of_work']}\n"
                    f"👤 Преподаватель: {lesson['lecturer']}\n"
                    f"⏰ Время: {lesson['begin']} - {lesson['end']}\n"
                    f"🚪 Аудитория {lesson['auditorium']}\n\n"
                )
        header = f"<b>Расписание на неделю ({start_date} - {end_date})</b>\n"
        final_message = header + full_schedule_text

        new_message_ids = send_long_message(
            bot=bot,
            chat_id=chat_id,
            text=final_message,
            parse_mode='HTML',
            reply_markup=markup,
            message_id=message_id,
            prev_messages_id=previous_messages_ids
        )
        with bot.retrieve_data(chat_id, chat_id) as data:
            data['last_schedule_messages'] = new_message_ids[1:]
        schedule_cache.set(cache_key, (final_message, markup))


def show_discipline_info(bot, chat_id, discipline_id, offset=0, message_id=None):
    start_date, end_date, quarter = current_quarter(offset)
    cache_key = f"{chat_id}_{discipline_id}_{offset}"
    cached_data = schedule_cache.get(cache_key)

    with bot.retrieve_data(chat_id, chat_id) as data:
        user_session = data.get('session', requests.Session())
        previous_messages_ids = data.get('last_discipline_messages', [])

    if cached_data:
        final_message, markup = cached_data
        new_message_ids = send_long_message(
            bot=bot,
            chat_id=chat_id,
            text=final_message,
            parse_mode='HTML',
            reply_markup=markup,
            message_id=message_id,
            prev_messages_id=previous_messages_ids
        )
        with bot.retrieve_data(chat_id, chat_id) as data:
            data['last_schedule_messages'] = new_message_ids[1:]
        return

    profile_response = user_session.get(url=PROFILE_URL)
    student_id = profile_response.json()[0]['id']
    response = user_session.post(url=DISCIPLINE_URL,
                                 json={
                                     "date_from": start_date,
                                     "date_to": end_date,
                                     'discipline_id': discipline_id,
                                     'kind_of_works': [],
                                     'student_id': student_id
                                 },
                                 headers={
                                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                   "Chrome/125.0.0.0 Safari/537.36",
                                     "Referer": DISCIPLINE_URL,
                                     "Content-Type": "application/x-www-form-urlencoded"
                                 },
                                 allow_redirects=True
                                 )
    markup = create_discipline_keyboard(discipline_id, quarter, offset)
    discipline_data = response.json()

    if discipline_data.get('error') == 1:
        text_data_not_found = f'<b>📅{start_date} - {end_date}\n Данные не найдены</b>'
        if message_id:
            try:
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text_data_not_found,
                    parse_mode='HTML',
                    reply_markup=markup
                )
            except telebot.apihelper.ApiTelegramException as e:
                if "message is not modified" in str(e).lower():
                    pass
                else:
                    raise e
        else:
            bot.send_message(
                chat_id,
                text_data_not_found,
                parse_mode='HTML',
                reply_markup=markup
            )
        return

    if str(student_id) not in discipline_data['rows']:
        text_student_not_found = f"⚠️ Данные для студента ID {student_id} не найдены"
        if message_id:
            try:
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text_student_not_found,
                    reply_markup=markup
                )
            except telebot.apihelper.ApiTelegramException as e:
                if "message is not modified" in str(e).lower():
                    pass
                else:
                    raise e
        else:
            bot.send_message(
                chat_id,
                text_student_not_found,
                reply_markup=markup
            )
        return

    student_data = discipline_data['rows'][str(student_id)]
    student_lessons = student_data.get('lessons', {})

    lessons_info = {}
    for lesson_id, lesson_data in student_lessons.items():
        attendance = lesson_data.get('attendance', {})
        visit_status = attendance.get('visit_status_id')

        total_mark = 0
        marks = lesson_data.get('marks', [])
        for mark in marks:
            total_mark += mark.get('mark_val', 0)

        lessons_info[int(lesson_id)] = {
            'visit_status': visit_status,
            'total_mark': total_mark,
            'marks': marks
        }

    total_lessons = len(discipline_data['lessons'])
    attended = sum(1 for lesson in lessons_info.values() if
                   (lesson.get('visit_status') == 2 or lesson.get('visit_status') is None))
    attendance_percent = (attended / total_lessons * 100) if total_lessons > 0 else 0
    header = f"Посещения и баллы за {quarter} ТКУ (Текущий контроль успеваемости)\n"
    footer = (f"📊Всего занятий: {total_lessons}\n"
              f"❌Отсутствовал: {total_lessons - attended}\n"
              f"📈Процент посещаемости: {attendance_percent:.1f}%\n"
              f"⭐Общая сумма баллов за ТКУ: {float(student_data.get('mark_sum', 0)):.1f}\n\n")

    lessons_text = ""
    for lesson in discipline_data['lessons']:
        lesson_id = lesson['id']
        lesson_data = lessons_info.get(lesson_id, {})

        visit_status = lesson_data.get('visit_status')
        if visit_status == 2 or visit_status is None:
            status = "✅ Присутствовал"
        elif visit_status == 4:
            status = "❌ Отсутствовал"
        else:
            status = f"❓ Неизвестный статус ({visit_status})"

        date = datetime.datetime.strptime(lesson['hold_at'], '%Y-%m-%d').strftime('%d.%m.%Y')
        start_time = datetime.datetime.strptime(lesson['start_at'], '%H:%M:%S').strftime('%H:%M')
        end_time = datetime.datetime.strptime(lesson['finish_at'], '%H:%M:%S').strftime('%H:%M')
        time_range = f"{date} {start_time}-{end_time}"

        total_mark = lesson_data.get('total_mark', 0)
        mark_text = f"{total_mark:.1f}" if isinstance(total_mark, (int, float)) else "0.0"

        lessons_text += (f"📅 Дата и время: {time_range}\n"
                         f"📚 Тип: {lesson.get('kind_of_work', 'Тип недоступен')}\n"
                         f"👤 Преподаватель: {lesson.get('profile_fio', 'Неизвестен')}\n"
                         f"👣 Статус посещения: {status}\n"
                         f"⭐ Баллы за занятие: {mark_text}\n"
                         f"{'-' * 40}\n")

    final_message = header + lessons_text + footer
    new_message_ids = send_long_message(
        bot=bot,
        chat_id=chat_id,
        text=final_message,
        parse_mode='HTML',
        reply_markup=markup,
        message_id=message_id,
        prev_messages_id=previous_messages_ids
    )
    schedule_cache.set(cache_key, (final_message, markup))
    with bot.retrieve_data(chat_id, chat_id) as data:
        data['last_discipline_messages'] = new_message_ids[1:]


@bot.message_handler(commands=['start', 'menu', 'cancel', 'disciplines', 'schedule', 'login'], state='*')
def handle_commands_anywhere(message):
    if message.text == '/start':
        start(message)
    elif message.text == '/start':
        login(message)
    elif message.text == '/menu':
        menu(message)
    elif message.text == '/login':
        login(message)
    elif message.text in ['Баллы и посещения', '/disciplines']:
        handle_disciplines_list(message)
    elif message.text in ['Расписание группы', '/schedule_group']:
        bot.set_state(message.from_user.id, UserStates.waiting_group, message.chat.id)
        handle_group_choose(message)
    elif message.text in ['Расписание преподавателя', '/schedule_teacher']:
        bot.set_state(message.from_user.id, UserStates.waiting_teacher, message.chat.id)
        handle_group_choose(message)
    elif message.text == '/cancel':
        bot.send_message(message.chat.id, "Текущее действие отменено.")


@bot.message_handler(commands=['start'])
def start(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if check_authorization(message.from_user.id, message.chat.id):
        markup.add(
            types.KeyboardButton('Расписание группы'),
            types.KeyboardButton('Баллы и посещения')
        )
    else:
        markup.add(
            types.KeyboardButton('Расписание группы'),
            types.KeyboardButton('Войти в аккаунт'),
        )
    bot.send_message(message.from_user.id, ('Добро пожаловать в FinBot\n'
                                            'Функционал Бота:\n'
                                            '1. Посмотреть Расписание\n'
                                            '2. Посмотреть Расписание преподавателя\n'
                                            '3. Посмотреть свои баллы(нужно войти в аккаунт)\n\n'
                                            'Выбери что ты хочешь сделать'),
                     reply_markup=markup)


@bot.message_handler(func=lambda message: message.text.lower() in ['/login', 'войти в аккаунт'])
def login(message):
    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        user_login = data.get('user_login')
        user_password = data.get('user_password')

    if user_login and user_password:
        if check_authorization(message.from_user.id, message.chat.id):
            bot.set_state(message.from_user.id, UserStates.final, message.chat.id)
            menu(message)
            return
        bot.set_state(message.from_user.id, UserStates.waiting_code, message.chat.id)
        login_account(message, user_login=user_login, user_password=user_password)
    elif user_login:
        bot.set_state(message.from_user.id, UserStates.waiting_password, message.chat.id)
        bot.send_message(message.from_user.id, 'Отлично, теперь введи пароль')
    else:
        markup = types.ReplyKeyboardRemove()
        bot.send_message(message.chat.id, 'Привет, введи свой логин', reply_markup=markup)
        bot.set_state(message.from_user.id, UserStates.waiting_login, message.chat.id)


@bot.message_handler(state=UserStates.waiting_login)
def process_login(message):
    if message.text.startswith('/'):
        return
    try:
        user_login = message.text
        if len(user_login) != 6:
            raise ValueError
        with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            data['user_login'] = user_login
        bot.send_message(message.chat.id, "Отлично, теперь введи пароль")
        bot.set_state(message.from_user.id, UserStates.waiting_password, message.chat.id)
    except ValueError:
        bot.send_message(message.chat.id, "Логин должен содердать 6 цифр")


@bot.message_handler(state=UserStates.waiting_password)
def process_password(message):
    if message.text.startswith('/'):
        return
    user_password = message.text
    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['user_password'] = user_password
        user_login = data['user_login']
    if login_account(message, user_login=user_login, user_password=user_password) == 'Invalid credentials':
        return
    bot.set_state(message.from_user.id, UserStates.waiting_code, message.chat.id)


@bot.message_handler(state=UserStates.waiting_code)
def process_code(message):
    if message.text.startswith('/'):
        return
    try:
        user_code = message.text
        if len(user_code) != 6:
            raise ValueError
        with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            session_id = data['session_id']
        user_code_checksum = otpchksum(message.text)

        user_session = get_user_session(message.from_user.id, message.chat.id)
        user_session.post(
            url=LOGIN_URL,
            data={
                "JS_VALID": "1",
                "TYPE": "OTP",
                "OTP_CODE": user_code,
                "OTP_CODE_CHECKSUM0": user_code_checksum,
                "sessid": session_id
            },
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Referer": LOGIN_URL,
                "Content-Type": "application/x-www-form-urlencoded"
            },
            allow_redirects=True
        )
        if not check_authorization(message.from_user.id, message.chat.id):
            bot.send_message(message.chat.id, "Неправильный код")
            return
        bot.set_state(message.from_user.id, UserStates.final, message.chat.id)
        menu(message)

    except ValueError:
        bot.send_message(message.chat.id, "Код должен содердать 6 цифр")


@bot.message_handler(commands=['menu'])
def menu(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if check_authorization(message.from_user.id, message.chat.id):
        markup.add(
            types.KeyboardButton('Расписание группы'),
            types.KeyboardButton('Расписание преподавателя'),
            types.KeyboardButton('Баллы и посещения')
        )
    else:
        markup.add(
            types.KeyboardButton('Расписание группы'),
            types.KeyboardButton('Расписание преподавателя'),
            types.KeyboardButton('Войти в аккаунт'),
        )
    bot.send_message(message.from_user.id,
                     'Выбери что ты хочешь сделать',
                     reply_markup=markup)


@bot.message_handler(func=lambda message: message.text.lower() == 'расписание группы'
                     or message.text == '/schedule_group')
def handle_group_choose(message):
    markup = types.ReplyKeyboardRemove()
    bot.send_message(message.chat.id, 'Введите название группы', reply_markup=markup)
    bot.set_state(message.from_user.id, UserStates.waiting_group, message.chat.id)


@bot.message_handler(func=lambda message: message.text.lower() == 'расписание преподавателя'
                     or message.text == '/schedule_teacher')
def handle_teacher_choose(message):
    markup = types.ReplyKeyboardRemove()
    bot.send_message(message.chat.id, 'Введите ФИО преподаваетля', reply_markup=markup)
    bot.set_state(message.from_user.id, UserStates.waiting_teacher, message.chat.id)


@bot.message_handler(state=[UserStates.waiting_group, UserStates.waiting_teacher])
def process_group_input(message):
    if message.text in ['Баллы и посещения', '/disciplines']:
        bot.set_state(message.from_user.id, UserStates.final, message.chat.id)
        handle_disciplines_list(message)
        return
    user_session = get_user_session(message.from_user.id, message.chat.id)
    state = bot.get_state(user_id=message.from_user.id, chat_id=message.chat.id)

    if state == 'UserStates:waiting_teacher':
        teacher_response = user_session.get(url=SEARCH_URL,
                                            params={
                                                'type': 'person',
                                                'term': message.text
                                        })
        schedule_data = teacher_response.json()
    else:
        group_response = user_session.get(url=SEARCH_URL,
                                          params={
                                              'type': 'group',
                                              'term': message.text
                                          })
        schedule_data = group_response.json()
    if not schedule_data:
        if state == 'UserStates:waiting_teacher':
            bot.send_message(message.from_user.id,
                             text='Преподаватель не найден, попробуй ввести снова')
        else:
            bot.send_message(message.from_user.id,
                             text='Группа не найдена, попробуй ввести снова')
        return
    with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
        data['group_id'] = schedule_data[0]['id']
    markup = types.InlineKeyboardMarkup(row_width=2)
    yes_btn = types.InlineKeyboardButton('Да, верно', callback_data="schedule_0")
    no_btn = types.InlineKeyboardButton('Нет, выбрать заново', callback_data="group_incorrect")
    markup.add(yes_btn, no_btn)
    if UserStates.waiting_group:
        bot.send_message(message.from_user.id,
                         text=f'Твоя группа это {schedule_data[0]['label']}, верно?',
                         reply_markup=markup)
    elif UserStates.waiting_teacher:
        bot.send_message(message.from_user.id,
                         text=f'Ты хочешь посмотреть расписание {schedule_data[0]['label']}, верно?',
                         reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == "group_incorrect")
def handle_group_incorrect(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)

    bot.send_message(call.message.chat.id, 'Пожалуйста, введите название группы ещё раз')
    bot.set_state(call.from_user.id, UserStates.waiting_group, call.message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('schedule_'))
def handle_schedule_navigation(call):
    try:
        offset_str = call.data.split('_')[-1]
        offset = int(offset_str)
        bot.answer_callback_query(call.id, "Загрузка...")
        show_schedule(
            bot,
            call.message.chat.id,
            offset,
            call.message.message_id
        )

    except Exception as e:
        bot.answer_callback_query(
            call.id,
            f"Ошибка: {str(e)}",
            show_alert=True
        )


@bot.message_handler(commands=['logout'])
def logout(message):
    if check_authorization(message.from_user.id, message.chat.id):
        with bot.retrieve_data(message.from_user.id, message.chat.id) as data:
            data.clear()
        bot.send_message(message.from_user.id, 'Вы успешно вышли из аккаунта')
    bot.send_message(message.from_user.id, 'Вы и так не авторизованы')


@bot.message_handler(func=lambda message: message.text.lower() == 'баллы и посещения'
                     or message.text == '/disciplines')
def handle_disciplines_list(message):
    if not check_authorization(message.from_user.id, message.chat.id):
        bot.send_message(message.from_user.id, 'Для этой функции нужно войти в аккаунт с помощью команды /login')
        return

    remove_msg = bot.send_message(
        chat_id=message.chat.id,
        text="Ожидайте, происходит загрузка",
        reply_markup=types.ReplyKeyboardRemove())

    user_session = get_user_session(message.from_user.id, message.chat.id)
    profile_response = user_session.get(url=PROFILE_URL)
    student_id = profile_response.json()[0]['id']
    start_date, end_date = get_current_semester()
    cache_key = f"{student_id}_{message.chat.id}"
    cached_data = schedule_cache.get(cache_key)

    if cached_data:
        final_text, markup = cached_data
        bot.send_message(
            chat_id=message.chat.id,
            text=final_text,
            parse_mode='HTML',
            reply_markup=markup
        )
        bot.delete_message(message.chat.id, remove_msg.message_id)
        return

    disciplines_response = user_session.post(url=DISCIPLINES_LIST_URL,
                                             json={
                                                 "date_from": start_date,
                                                 "date_to": end_date,
                                                 "student_id": student_id
                                             },
                                             headers={
                                                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                               "Chrome/125.0.0.0 Safari/537.36",
                                                 "Referer": DISCIPLINES_LIST_URL,
                                                 "Content-Type": "application/x-www-form-urlencoded"
                                             },
                                             allow_redirects=True
                                             )
    disciplines_data = disciplines_response.json()
    attendance_percent = disciplines_data.get("attendance_percent", "N/A")
    headers = (
        f"📊 <b>Ваша успеваемость</b>\n"
        f"👣 Посещаемость: <b>{attendance_percent}%</b>\n\n"
        f"📚 <b>Выберите дисциплину:</b>\n\n"
    )
    discipline_list = []
    buttons = []

    for discipline in disciplines_data['disciplines']:
        discipline_name = discipline.get('discipline_name', 'Без названия')
        teachers = [teacher['fio'] for teacher in discipline.get('teachers', [])]
        teacher_list = "\n".join([f"👤{t}" for t in teachers]) if teachers else "👤Преподаватель не указан"
        discipline_list.append(
            f"📖 <b>{discipline_name}</b>\n"
            f"{teacher_list}\n"
            "────────────\n"
        )
        btn_text = discipline_name
        if len(btn_text) > 18:
            btn_text = btn_text[:15] + "..."
        buttons.append(
            types.InlineKeyboardButton(btn_text, callback_data=f"discipline_{discipline['discipline_id']}_0"))

    full_disciplines_text = "".join(discipline_list)
    final_text = headers + full_disciplines_text
    markup = types.InlineKeyboardMarkup()
    row = []
    for i, button in enumerate(buttons):
        row.append(button)
        if (i + 1) % 2 == 0 or i == len(buttons) - 1:
            markup.row(*row)
            row = []
    bot.send_message(
        chat_id=message.chat.id,
        text=final_text,
        parse_mode='HTML',
        reply_markup=markup
    )
    bot.delete_message(message.chat.id, remove_msg.message_id)
    schedule_cache.set(cache_key, (final_text, markup))


@bot.callback_query_handler(func=lambda call: call.data.startswith('discipline_'))
def handle_discipline_by_id(call):
    try:
        if not check_authorization(call.from_user.id, call.message.chat.id):
            bot.send_message(call.message.chat.id, 'Сессия истекла, введи свои данные заново с помощью команды /start')
            raise RuntimeError
        offset = int(call.data.split('_')[-1])
        discipline_id = call.data.split('_')[1]
        bot.answer_callback_query(call.id, "Загрузка...")
        show_discipline_info(
            bot,
            call.message.chat.id,
            discipline_id,
            offset,
            message_id=call.message.message_id
        )

    except Exception as e:
        bot.answer_callback_query(
            call.id,
            f"Ошибка: {str(e)}",
        )


bot.add_custom_filter(custom_filters.StateFilter(bot))

if __name__ == '__main__':
    bot.infinity_polling()
