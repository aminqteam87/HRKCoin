import os
import re
import sqlite3
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.exceptions import TelegramBadRequest

# ================= Configuration Variables =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
SUPPORT_ID = "@Imcivilian"
HRK_RATE = 10000  # تومان
FEE_PERCENT = 0.075  # 7.5% کمیسیون ربات در بازی‌های گروهی (PvP)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()
router = Router()
dp.include_router(router)

# ================= Database Setup =================
def init_db():
    conn = sqlite3.connect('casino.db')
    c = conn.cursor()
    # جدول کاربران
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, username TEXT, phone TEXT, balance REAL DEFAULT 0, 
                  referrer INTEGER, pay_count INTEGER DEFAULT 0, withdraw_count INTEGER DEFAULT 0)''')
    # جدول کانال‌های اجباری
    c.execute('''CREATE TABLE IF NOT EXISTS channels (channel_id TEXT PRIMARY KEY)''')
    # جدول موقت برای بازی‌های گروهی
    c.execute('''CREATE TABLE IF NOT EXISTS active_games 
                 (game_id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, p1_id INTEGER, p2_id INTEGER, 
                  bet REAL, dice_count INTEGER, status TEXT, p1_score INTEGER DEFAULT 0, p2_score INTEGER DEFAULT 0,
                  p1_thrown INTEGER DEFAULT 0, p2_thrown INTEGER DEFAULT 0, message_id INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

def db_query(query, args=(), fetchone=False, fetchall=False, commit=False):
    conn = sqlite3.connect('casino.db')
    c = conn.cursor()
    c.execute(query, args)
    res = None
    if fetchone: res = c.fetchone()
    if fetchall: res = c.fetchall()
    if commit: 
        conn.commit()
        res = c.lastrowid
    conn.close()
    return res

init_db()

# ================= FSM States =================
class WithdrawFSM(StatesGroup):
    type = State()
    amount = State()
    destination = State()
    memo = State()

class AdminFSM(StatesGroup):
    add_channel = State()
    remove_channel = State()
    charge_user = State()
    charge_amount = State()

# ================= Keyboards =================
def main_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🏛 حساب کاربری"), KeyboardButton(text="💰 کیف پول")],
        [KeyboardButton(text="🤝 زیرمجموعه گیری"), KeyboardButton(text="⚖️ آموزش و مقررات")]
    ], resize_keyboard=True)

def admin_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ افزودن کانال"), KeyboardButton(text="➖ حذف کانال")],
        [KeyboardButton(text="💵 شارژ کاربر")]
    ], resize_keyboard=True)

# ================= Helper Functions =================
async def check_channels(user_id):
    channels = db_query("SELECT channel_id FROM channels", fetchall=True)
    if not channels: return True
    for (ch,) in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch, user_id=user_id)
            if member.status in ['left', 'kicked']: return False
        except TelegramBadRequest:
            pass
    return True

async def process_winner(chat_id, p1_id, p2_id, score1, score2, bet):
    pool = bet * 2
    rake = pool * FEE_PERCENT
    win_amount = pool - rake
    
    p1_name = (await bot.get_chat(p1_id)).first_name
    p2_name = "🤖 ربات" if not p2_id else (await bot.get_chat(p2_id)).first_name
    
    win_id = None
    if score1 > score2:
        db_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (win_amount, p1_id), commit=True)
        winner, win_score, lose_score = p1_name, score1, score2
        win_id = p1_id
    elif score2 > score1:
        if p2_id: 
            db_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (win_amount, p2_id), commit=True)
        winner, win_score, lose_score = p2_name, score2, score1
        win_id = p2_id
    else:
        db_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (bet, p1_id), commit=True)
        if p2_id: 
            db_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (bet, p2_id), commit=True)
        await bot.send_message(chat_id, f"🤝 **نتیجه نهایی: مساوی!**\n\nامتیازها: **{score1}** - **{score2}**\n💰 مبلغ شرط به حساب هر دو بازیکن برگشت داده شد.", parse_mode="Markdown")
        return

    text = f"🎉 **کاربر {winner} برنده مسابقه شد!**\n\n" \
           f"📊 امتیاز نهایی: **{win_score}** مجموعاً در برابر **{lose_score}**\n" \
           f"💰 مبلغ **{win_amount:.2f} HRK** به حساب برنده واریز شد (کارمزد کسر گردید)."
    
    if win_id:
        ref = db_query("SELECT referrer FROM users WHERE user_id=?", (win_id,), fetchone=True)
        if ref and ref[0]:
            ref_bonus = bet * 0.20
            db_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (ref_bonus, ref[0]), commit=True)
            
    await bot.send_message(chat_id, text, parse_mode="Markdown")

# ================= Private Chat Handlers =================

@router.message(CommandStart(), F.chat.type == 'private')
async def start_cmd(message: types.Message, command: Command):
    args = command.args
    referrer = int(args) if args and args.isdigit() else None
    if referrer == message.from_user.id: referrer = None
    
    user = db_query("SELECT phone FROM users WHERE user_id=?", (message.from_user.id,), fetchone=True)
    
    if not user:
        db_query("INSERT OR IGNORE INTO users (user_id, username, referrer) VALUES (?, ?, ?)", 
                 (message.from_user.id, message.from_user.username, referrer), commit=True)
        
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 ارسال شماره تماس", request_contact=True)]], resize_keyboard=True)
        await message.answer("👋 **سلام! برای استفاده از امکانات ربات ابتدا باید شماره تماس خود را تایید کنید:**", reply_markup=kb, parse_mode="Markdown")
    elif user and not user[0]:
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 ارسال شماره تماس", request_contact=True)]], resize_keyboard=True)
        await message.answer("⚠️ **لطفاً با کلیک روی دکمه زیر شماره تماس خود را تایید کنید تا حساب فعال شود:**", reply_markup=kb, parse_mode="Markdown")
    else:
        db_query("UPDATE users SET username=? WHERE user_id=?", (message.from_user.username, message.from_user.id), commit=True)
        is_joined = await check_channels(message.from_user.id)
        if not is_joined:
            await message.answer("❌ **ابتدا باید در کانال‌های اسپانسر عضو شوید و سپس دوباره /start را ارسال کنید.**", parse_mode="Markdown")
            return
        await message.answer("🎮 **به پلتفرم پیشرفته بازی HRK خوش آمدید!**", reply_markup=main_menu(), parse_mode="Markdown")

@router.message(F.contact, F.chat.type == 'private')
async def contact_handler(message: types.Message):
    if message.contact.user_id == message.from_user.id:
        db_query("UPDATE users SET phone=? WHERE user_id=?", (message.contact.phone_number, message.from_user.id), commit=True)
        await message.answer("✅ **ثبت نام شما با موفقیت تکمیل شد.**", reply_markup=main_menu(), parse_mode="Markdown")
    else:
        await message.answer("⚠️ **لطفاً شماره اختصاصی خودتان را ارسال کنید.**", parse_mode="Markdown")

@router.message(F.text == "🏛 حساب کاربری", F.chat.type == 'private')
async def account_info(message: types.Message):
    user = db_query("SELECT username, balance, pay_count, withdraw_count FROM users WHERE user_id=?", (message.from_user.id,), fetchone=True)
    if user:
        text = f"👤 **اطلاعات حساب کاربری شما:**\n\n" \
               f"🆔 آیدی عددی: `{message.from_user.id}`\n" \
               f"📣 یوزرنیم: @{user[0] or 'ندارد'}\n" \
               f"💰 موجودی کیف‌پول: **{user[1]:.2f} HRK**\n" \
               f"📥 تعداد شارژ: `{user[2]}`\n" \
               f"📤 تعداد تسویه: `{user[3]}`"
        await message.answer(text, parse_mode="Markdown")

@router.message(F.text == "💰 کیف پول", F.chat.type == 'private')
async def wallet_menu(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 واریز (شارژ)", callback_data="wallet_deposit"),
         InlineKeyboardButton(text="📤 برداشت (تسویه)", callback_data="wallet_withdraw")]
    ])
    await message.answer("💳 **بخش مدیریت مالی و کیف پول**\nعملیات مورد نظر خود را انتخاب کنید:", reply_markup=kb, parse_mode="Markdown")

@router.message(F.text == "🤝 زیرمجموعه گیری", F.chat.type == 'private')
async def ref_menu(message: types.Message):
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    refs = db_query("SELECT COUNT(*) FROM users WHERE referrer=?", (message.from_user.id,), fetchone=True)
    ref_count = refs[0] if refs else 0
    text = f"🔥 **سیستم کسب درآمد و زیرمجموعه‌گیری**\n\n" \
           f"👥 تعداد کاربران دعوت شده توسط شما: **{ref_count} نفر**\n\n" \
           f"🎁 با دعوت از دوستان خود، **20%** از کل سود بازی‌های آن‌ها را بدون کسر از خودشان، به صورت مادام‌العمر دریافت کنید!\n\n" \
           f"🔗 لینک دعوت اختصاصی شما:\n`{link}`"
    await message.answer(text, parse_mode="Markdown")

@router.message(F.text == "⚖️ آموزش و مقررات", F.chat.type == 'private')
async def rules_menu(message: types.Message):
    text = f"📚 **آموزش و مقررات پلتفرم بازی HRK**\n\n" \
           f"🔹 **قوانین مالی:**\n" \
           f"🔸 نرخ هر کوین HRK در سیستم معادل **{HRK_RATE:,} تومان** می‌باشد.\n" \
           f"🔸 برای شارژ حساب یا برداشت موجودی، می‌توانید از طریق دکمه «کیف پول» اقدام کرده و با پشتیبانی در ارتباط باشید.\n\n" \
           f"🎮 **راهنمای جامع بازی‌های گروهی (فقط تاس):**\n" \
           f"شما می‌توانید ربات را به گروه‌های خود اضافه کرده و با دستورات زیر به ۳ حالت مختلف بازی کنید:\n\n" \
           f"**۱. بازی کلاسیک رقابتی (بیشترین مجموع):**\n" \
           f"📝 `dice [مبلغ]` 👈 مثال: `dice 30` (یک تاس، شرط ۳۰ کوین)\n" \
           f"📝 `[تعداد] dice [مبلغ]` 👈 مثال: `3 dice 50` (سه تاس، شرط ۵۰ کوین)\n\n" \
           f"**۲. بازی زوج و فرد (Even / Odd):**\n" \
           f"📝 `even [مبلغ]` 👈 مثال: `even 20`\n" \
           f"📝 `odd [مبلغ]` 👈 مثال: `odd 20`\n\n" \
           f"**۳. بازی حدس عدد (Guess):**\n" \
           f"📝 `guess [مبلغ] [عدد]` 👈 مثال: `guess 10 4`"
    await message.answer(text, parse_mode="Markdown")

# --- Wallet Sub-Menus ---
@router.callback_query(F.data == "wallet_deposit")
async def deposit_info(call: types.CallbackQuery):
    text = f"📥 **شارژ حساب کاربری**\n\nنرخ توکن: **{HRK_RATE:,} تومان**\n" \
           f"💬 برای واریز ریالی یا ارزی (TON) به پشتیبانی پیام دهید. پس از واریز، حساب شما مستقیماً توسط ادمین شارژ خواهد شد."
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎧 ارتباط با پشتیبانی", url=f"https://t.me/{SUPPORT_ID.replace('@','')}")]])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "wallet_withdraw")
async def withdraw_start(call: types.CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 برداشت ارزی (TON)", callback_data="wd_ton"),
         InlineKeyboardButton(text="💳 برداشت ریالی", callback_data="wd_irt")]
    ])
    await call.message.edit_text("📤 نوع برداشت خود را انتخاب کنید:", reply_markup=kb)

@router.callback_query(F.data.in_(["wd_ton", "wd_irt"]))
async def withdraw_type(call: types.CallbackQuery, state: FSMContext):
    w_type = "تون کوین (TON)" if call.data == "wd_ton" else "ریالی"
    await state.update_data(type=call.data)
    user = db_query("SELECT balance FROM users WHERE user_id=?", (call.from_user.id,), fetchone=True)
    text = f"شما درخواست برداشت **{w_type}** دارید.\nموجودی: **{user[0]:.2f} HRK**\n\nمقدار کوین برای برداشت را ارسال کنید:"
    await call.message.edit_text(text, parse_mode="Markdown")
    await state.set_state(WithdrawFSM.amount)

@router.message(WithdrawFSM.amount)
async def withdraw_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        user = db_query("SELECT balance FROM users WHERE user_id=?", (message.from_user.id,), fetchone=True)
        if amount <= 0 or amount > user[0]:
            await message.answer("❌ مبلغ نامعتبر است یا موجودی کافی نیست.")
            return
        await state.update_data(amount=amount)
        data = await state.get_data()
        msg = "💳 شماره کارت 16 رقمی و نام صاحب حساب:" if data['type'] == 'wd_irt' else "💎 آدرس کیف پول TON:"
        await message.answer(msg)
        await state.set_state(WithdrawFSM.destination)
    except ValueError:
        await message.answer("❌ لطفاً عدد ارسال کنید.")

@router.message(WithdrawFSM.destination)
async def withdraw_dest(message: types.Message, state: FSMContext):
    await state.update_data(destination=message.text)
    data = await state.get_data()
    if data['type'] == 'wd_ton':
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="نیاز نیست")]], resize_keyboard=True)
        await message.answer("📝 در صورت نیاز به Memo آن را بفرستید، در غیر اینصورت دکمه زیر را بزنید:", reply_markup=kb)
        await state.set_state(WithdrawFSM.memo)
    else:
        await finish_withdraw(message, state)

@router.message(WithdrawFSM.memo)
async def withdraw_memo(message: types.Message, state: FSMContext):
    await state.update_data(memo=message.text if message.text != "نیاز نیست" else "ندارد")
    await finish_withdraw(message, state)

async def finish_withdraw(message: types.Message, state: FSMContext):
    data = await state.get_data()
    amount, dest, memo = data['amount'], data['destination'], data.get('memo', 'ندارد')
    w_type = "ریالی" if data['type'] == 'wd_irt' else "ارزی (TON)"
    
    db_query("UPDATE users SET balance = balance - ?, withdraw_count = withdraw_count + 1 WHERE user_id=?", (amount, message.from_user.id), commit=True)
    
    req_text = f"🚨 **درخواست برداشت**\n\n👤 آیدی: `{message.from_user.id}`\nنوع: {w_type}\nمبلغ: {amount} HRK\nمقصد: `{dest}`\nممو: `{memo}`"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تایید و واریز شد", callback_data=f"approve_{message.from_user.id}_{amount}"),
         InlineKeyboardButton(text="❌ رد درخواست", callback_data=f"reject_{message.from_user.id}_{amount}")]
    ])
    
    await bot.send_message(ADMIN_ID, req_text, reply_markup=kb, parse_mode="Markdown")
    await message.answer("✅ درخواست شما ثبت و برای ادمین ارسال شد.", reply_markup=main_menu())
    await state.clear()

# ================= Admin Logic =================
@router.message(Command("panel"), F.from_user.id == ADMIN_ID)
async def admin_panel(message: types.Message):
    await message.answer("👨‍💻 پنل مدیریت:", reply_markup=admin_menu())

@router.message(F.text == "➕ افزودن کانال", F.from_user.id == ADMIN_ID)
async def admin_add_ch(message: types.Message, state: FSMContext):
    await message.answer("آیدی کانال را با @ بفرستید:")
    await state.set_state(AdminFSM.add_channel)

@router.message(AdminFSM.add_channel, F.from_user.id == ADMIN_ID)
async def admin_add_ch_exec(message: types.Message, state: FSMContext):
    db_query("INSERT OR IGNORE INTO channels (channel_id) VALUES (?)", (message.text,), commit=True)
    await message.answer(f"✅ کانال {message.text} اضافه شد.", reply_markup=admin_menu())
    await state.clear()

@router.message(F.text == "➖ حذف کانال", F.from_user.id == ADMIN_ID)
async def admin_rem_ch(message: types.Message, state: FSMContext):
    chs = db_query("SELECT channel_id FROM channels", fetchall=True)
    text = "کانال‌ها:\n" + "\n".join([c[0] for c in chs]) + "\n\nآیدی جهت حذف:" if chs else "لیست خالی است."
    await message.answer(text)
    await state.set_state(AdminFSM.remove_channel)

@router.message(AdminFSM.remove_channel, F.from_user.id == ADMIN_ID)
async def admin_rem_ch_exec(message: types.Message, state: FSMContext):
    db_query("DELETE FROM channels WHERE channel_id=?", (message.text,), commit=True)
    await message.answer(f"✅ کانال {message.text} حذف شد.", reply_markup=admin_menu())
    await state.clear()

@router.message(F.text == "💵 شارژ کاربر", F.from_user.id == ADMIN_ID)
async def admin_charge_ask(message: types.Message, state: FSMContext):
    await message.answer("شناسه کاربر را ارسال کنید.\n(می‌توانید **آیدی عددی** یا **یوزرنیم با @** بفرستید):")
    await state.set_state(AdminFSM.charge_user)

@router.message(AdminFSM.charge_user, F.from_user.id == ADMIN_ID)
async def admin_charge_u(message: types.Message, state: FSMContext):
    target = message.text.strip()
    
    if target.startswith("@"):
        username = target.replace("@", "")
        user_record = db_query("SELECT user_id FROM users WHERE username=?", (username,), fetchone=True)
        if not user_record:
            await message.answer("❌ کاربری با این یوزرنیم در دیتابیس ربات یافت نشد.")
            return
        u_id = user_record[0]
    elif target.isdigit():
        u_id = int(target)
        user_record = db_query("SELECT user_id FROM users WHERE user_id=?", (u_id,), fetchone=True)
        if not user_record:
            await message.answer("❌ این آیدی عددی در دیتابیس وجود ندارد.")
            return
    else:
        await message.answer("❌ فرمت نامعتبر! لطفاً آیدی عددی یا یوزرنیم (همراه با @) بفرستید.")
        return

    await state.update_data(user_id=u_id)
    await message.answer("مقدار HRK برای شارژ را ارسال کنید:")
    await state.set_state(AdminFSM.charge_amount)

@router.message(AdminFSM.charge_amount, F.from_user.id == ADMIN_ID)
async def admin_charge_exec(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        data = await state.get_data()
        u_id = data['user_id']
        
        db_query("UPDATE users SET balance = balance + ?, pay_count = pay_count + 1 WHERE user_id=?", (amount, u_id), commit=True)
        await message.answer(f"✅ کاربر با آیدی `{u_id}` به مبلغ **{amount} HRK** شارژ شد.", reply_markup=admin_menu(), parse_mode="Markdown")
        
        notif_text = f"🎉 **موجودی شما افزایش یافت!**\n\n" \
                     f"مبلغ **{amount} HRK** توسط تیم پشتیبانی به کیف پول شما واریز شد.\n" \
                     f"اکنون می‌توانید در بازی‌ها شرکت کنید. 🎲"
        try: 
            await bot.send_message(u_id, notif_text, parse_mode="Markdown")
        except Exception:
            await message.answer("⚠️ مبلغ شارژ شد اما کاربر ربات را بلاک کرده است.")
            
        await state.clear()
    except ValueError:
        await message.answer("❌ لطفاً یک عدد معتبر ارسال کنید.")

@router.callback_query(F.data.startswith("approve_"), F.from_user.id == ADMIN_ID)
async def admin_approve_req(call: types.CallbackQuery):
    _, u_id, amount = call.data.split("_")
    await call.message.edit_text(call.message.text + "\n\n✅ تایید شد.")
    try: await bot.send_message(int(u_id), f"✅ درخواست برداشت **{amount} HRK** شما تایید و واریز شد.", parse_mode="Markdown")
    except: pass

@router.callback_query(F.data.startswith("reject_"), F.from_user.id == ADMIN_ID)
async def admin_reject_req(call: types.CallbackQuery):
    _, u_id, amount = call.data.split("_")
    db_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (float(amount), int(u_id)), commit=True)
    await call.message.edit_text(call.message.text + "\n\n❌ رد شد (مبلغ برگشت داده شد).")
    try: await bot.send_message(int(u_id), f"❌ درخواست برداشت **{amount} HRK** رد شد و مبلغ به کیف پول شما برگشت.", parse_mode="Markdown")
    except: pass

# ================= Group Game Logic =================

@router.message(F.chat.type.in_({'group', 'supergroup'}), F.text == "بازی ها")
async def group_games_menu(message: types.Message):
    text = f"🎮 **راهنمای جامع بازی‌های پلتفرم HRK**\n\n" \
           f"🥇 **حالت کلاسیک (رقابتی):**\n" \
           f"🎲 `dice 30` (۱ تاس با شرط ۳۰ کوین)\n" \
           f"🎲 `3 dice 50` (۳ تاس با شرط ۵۰ کوین)\n\n" \
           f"🥈 **حالت حدس عدد (ضریب برد ۵ برابر):**\n" \
           f"🎯 `guess 10 4` (شرط ۱۰ کوین روی عدد ۴)\n\n" \
           f"🥉 **حالت زوج و فرد (ضریب ۱.۸ برابر):**\n" \
           f"☯️ `even 20` | `odd 20`\n\n" \
           f"💰 برای مشاهده موجودی دستور `wallet` را ارسال کنید."
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="ثبت‌نام / شارژ 💳", url=f"https://t.me/{(await bot.get_me()).username}")]])
    await message.reply(text, reply_markup=kb)

@router.message(F.chat.type.in_({'group', 'supergroup'}), F.text.lower().in_(["wallet", "balance"]))
async def group_balance(message: types.Message):
    user = db_query("SELECT balance FROM users WHERE user_id=?", (message.from_user.id,), fetchone=True)
    if user:
        await message.reply(f"💰 **موجودی شما:** **{user[0]:.2f} HRK**\n🇮🇷 **معادل ریالی:** **{(user[0] * HRK_RATE):,.0f} تومان**", parse_mode="Markdown")
    else:
        await message.reply("⚠️ **ابتدا در پیوی ربات /start را بزنید تا حساب شما ایجاد شود.**")

# ---- 1. حالت کلاسیک (لابی و پرتاب دستی) ----
@router.message(F.chat.type.in_({'group', 'supergroup'}), F.text.regexp(r'^(\d+ )?dice (\d+(\.\d+)?)$', flags=re.IGNORECASE))
async def group_dice_init(message: types.Message):
    match = re.match(r'^(\d+ )?dice (\d+(\.\d+)?)$', message.text, re.IGNORECASE)
    dice_count = int(match.group(1).strip()) if match.group(1) else 1
    bet = float(match.group(2))
    
    if dice_count > 5:
        await message.reply("❌ **حداکثر تعداد تاس مجاز 5 عدد می‌باشد.**")
        return
        
    user = db_query("SELECT balance FROM users WHERE user_id=?", (message.from_user.id,), fetchone=True)
    if not user or user[0] < bet:
        await message.reply("❌ **موجودی حساب شما کافی نیست یا هنوز در پیوی ربات عضو نشده‌اید.**")
        return

    game_id = db_query("INSERT INTO active_games (chat_id, p1_id, bet, dice_count, status) VALUES (?, ?, ?, ?, 'waiting')",
                       (message.chat.id, message.from_user.id, bet, dice_count), commit=True)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 بازی با ربات", callback_data=f"pbot_{game_id}"),
         InlineKeyboardButton(text="👤 ورود به لابی (کاربر)", callback_data=f"puser_{game_id}")]
    ])
    msg = await message.reply(f"🎲 **اتاق بازی کلاسیک ایجاد شد!**\n\n👤 طراح: {message.from_user.first_name}\n💰 مبلغ شرط: **{bet} HRK**\n🎯 تعداد تاس: **{dice_count}**\n\n⏳ منتظر حریف...", reply_markup=kb, parse_mode="Markdown")
    db_query("UPDATE active_games SET message_id=? WHERE game_id=?", (msg.message_id, game_id), commit=True)

# ---- تسک انقضای ۳۰ ثانیه‌ای پرتاب تاس ----
async def task_throw_timeout(game_id: int, player_no: int):
    await asyncio.sleep(30)
    game = db_query("SELECT chat_id, p1_id, p2_id, bet, p1_thrown, p2_thrown, status, message_id FROM active_games WHERE game_id=?", (game_id,), fetchone=True)
    if not game or game[6] != 'playing': return

    chat_id, p1_id, p2_id, bet, p1_thrown, p2_thrown, _, msg_id = game
    
    if player_no == 1 and not p1_thrown:
        # اگر کاربر ۱ نینداخت و بازی با ربات بود یا PvP
        db_query("UPDATE active_games SET status='timeout' WHERE game_id=?", (game_id,), commit=True)
        # برگشت پول به بازیکن ۲ در صورت وجود
        if p2_id: db_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (bet, p2_id), commit=True)
        db_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (bet, p1_id), commit=True) # برگشت به ۱ جهت لغو
        try: await bot.edit_message_text("⏱ **زمان پرتاب تاس بازیکن اول به پایان رسید و بازی لغو شد.**", chat_id, msg_id, parse_mode="Markdown")
        except: pass

    elif player_no == 2 and not p2_thrown:
        db_query("UPDATE active_games SET status='timeout' WHERE game_id=?", (game_id,), commit=True)
        # اگر حریف ربات بود که خودش میندازه (تایم اوت نداره)، پس اینجا حالت PvP هست که بازیکن ۲ نینداخته
        db_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (bet, p1_id), commit=True)
        db_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (bet, p2_id), commit=True)
        try: await bot.edit_message_text("⏱ **زمان پرتاب تاس بازیکن دوم به پایان رسید و بازی لغو شد.**", chat_id, msg_id, parse_mode="Markdown")
        except: pass

@router.callback_query(F.data.startswith("pbot_"))
async def play_vs_bot_init(call: types.CallbackQuery):
    game_id = int(call.data.split("_")[1])
    game = db_query("SELECT p1_id, bet, dice_count, status FROM active_games WHERE game_id=?", (game_id,), fetchone=True)
    
    if not game or game[3] != 'waiting':
        return await call.answer("⚠️ این بازی منقضی یا شروع شده است.", show_alert=True)
    if call.from_user.id != game[0]:
        return await call.answer("❌ فقط سازنده بازی می‌تواند حالت ربات را فعال کند.", show_alert=True)

    await call.answer()
    db_query("UPDATE active_games SET status='playing' WHERE game_id=?", (game_id,), commit=True)
    db_query("UPDATE users SET balance = balance - ? WHERE user_id=?", (game[1], game[0]), commit=True)

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎲 پرتاب تاس شما", callback_data=f"throw1_{game_id}")]])
    await call.message.edit_text(f"🎲 **بازی با ربات آغاز شد!**\n\n👤 {call.from_user.first_name} **۳۰ ثانیه** فرصت داری تا تاس خودت رو پرتاب کنی:", reply_markup=kb, parse_mode="Markdown")
    asyncio.create_task(task_throw_timeout(game_id, 1))

@router.callback_query(F.data.startswith("puser_"))
async def play_vs_user_join(call: types.CallbackQuery):
    game_id = int(call.data.split("_")[1])
    game = db_query("SELECT p1_id, bet, dice_count, status FROM active_games WHERE game_id=?", (game_id,), fetchone=True)
    
    if not game or game[3] != 'waiting':
        return await call.answer("⚠️ این لابی پر شده یا منقضی گردیده است.", show_alert=True)
    if call.from_user.id == game[0]:
        return await call.answer("❌ شما نمی‌توانید به بازی خودتان ملحق شوید!", show_alert=True)

    p2_id = call.from_user.id
    user2 = db_query("SELECT balance FROM users WHERE user_id=?", (p2_id,), fetchone=True)
    if not user2 or user2[0] < game[1]:
        return await call.answer("❌ موجودی کافی نیست. ابتدا در پیوی شارژ کنید.", show_alert=True)

    await call.answer("شما به لابی بازی متصل شدید!")
    p1_id, bet, d_count = game[0], game[1], game[2]
    
    db_query("UPDATE active_games SET status='playing', p2_id=? WHERE game_id=?", (p2_id, game_id), commit=True)
    db_query("UPDATE users SET balance = balance - ? WHERE user_id=?", (bet, p1_id), commit=True)
    db_query("UPDATE users SET balance = balance - ? WHERE user_id=?", (bet, p2_id), commit=True)

    p1_name = (await bot.get_chat(p1_id)).first_name
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎲 پرتاب تاس (نوبت پلیر اول)", callback_data=f"throw1_{game_id}")]])
    await call.message.edit_text(f"⚔️ **دوئل آغاز شد!**\n\n👥 بازیکنان: **{p1_name}** vs **{call.from_user.first_name}**\n💰 شرط: **{bet} HRK**\n\n⏱ {p1_name} ۳۰ ثانیه فرصت داری تاس بندازی:", reply_markup=kb, parse_mode="Markdown")
    asyncio.create_task(task_throw_timeout(game_id, 1))

@router.callback_query(F.data.startswith("throw1_"))
async def throw_player_one(call: types.CallbackQuery):
    game_id = int(call.data.split("_")[1])
    game = db_query("SELECT p1_id, p2_id, bet, dice_count, status, p1_thrown FROM active_games WHERE game_id=?", (game_id,), fetchone=True)
    
    if not game or game[4] != 'playing': return await call.answer("بازی جاری نیست.")
    if call.from_user.id != game[0]: return await call.answer("❌ نوبت شما نیست!", show_alert=True)
    if game[5] == 1: return await call.answer("شما قبلاً تاس انداخته‌اید.")

    await call.answer("در حال پرتاب...")
    db_query("UPDATE active_games SET p1_thrown=1 WHERE game_id=?", (game_id,), commit=True)
    
    p1_score = 0
    await call.message.answer(f"👤 **پرتاب تاس {call.from_user.first_name}:**")
    for _ in range(game[3]):
        d = await call.message.answer_dice(emoji="🎲")
        p1_score += d.dice.value
        await asyncio.sleep(2.5)

    db_query("UPDATE active_games SET p1_score=? WHERE game_id=?", (p1_score, game_id), commit=True)
    
    p2_id = game[1]
    if not p2_id: # بازی با ربات
        await call.message.answer(f"🤖 **حالا نوبت ربات است:**")
        bot_score = 0
        for _ in range(game[3]):
            d = await call.message.answer_dice(emoji="🎲")
            bot_score += d.dice.value
            await asyncio.sleep(2.5)
        await process_winner(call.message.chat.id, game[0], None, p1_score, bot_score, game[2])
    else: # بازی PvP با کاربر دیگر
        p2_name = (await bot.get_chat(p2_id)).first_name
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎲 پرتاب تاس حریف", callback_data=f"throw2_{game_id}")]])
        await call.message.answer(f"⏱ مجموع امتیاز شما: **{p1_score}**\n\nنوبت بازیکن دوم (**{p2_name}**) است که تاس خود را پرتاب کند (۳۰ ثانیه فرصت):", reply_markup=kb, parse_mode="Markdown")
        asyncio.create_task(task_throw_timeout(game_id, 2))

@router.callback_query(F.data.startswith("throw2_"))
async def throw_player_two(call: types.CallbackQuery):
    game_id = int(call.data.split("_")[1])
    game = db_query("SELECT p1_id, p2_id, bet, dice_count, status, p2_thrown, p1_score FROM active_games WHERE game_id=?", (game_id,), fetchone=True)
    
    if not game or game[4] != 'playing': return await call.answer("بازی فعال نیست.")
    if call.from_user.id != game[1]: return await call.answer("❌ این دکمه مخصوص بازیکن دوم است!", show_alert=True)
    if game[5] == 1: return await call.answer("شما قبلاً تاس انداخته‌اید.")

    await call.answer("در حال پرتاب...")
    db_query("UPDATE active_games SET p2_thrown=1 WHERE game_id=?", (game_id,), commit=True)
    
    p2_score = 0
    await call.message.answer(f"👤 **پرتاب تاس {call.from_user.first_name}:**")
    for _ in range(game[3]):
        d = await call.message.answer_dice(emoji="🎲")
        p2_score += d.dice.value
        await asyncio.sleep(2.5)

    db_query("UPDATE active_games SET p2_score=?, status='finished' WHERE game_id=?", (p2_score, game_id), commit=True)
    await process_winner(call.message.chat.id, game[0], game[1], game[6], p2_score, game[2])

# ---- 2. حالت زوج و فرد ----
@router.message(F.chat.type.in_({'group', 'supergroup'}), F.text.regexp(r'^(even|odd) (\d+(\.\d+)?)$', flags=re.IGNORECASE))
async def group_dice_even_odd(message: types.Message):
    match = re.match(r'^(even|odd) (\d+(\.\d+)?)$', message.text, re.IGNORECASE)
    choice = match.group(1).lower()
    bet = float(match.group(2))
    
    user_id = message.from_user.id
    user = db_query("SELECT balance FROM users WHERE user_id=?", (user_id,), fetchone=True)
    if not user or user[0] < bet:
        await message.reply("❌ **موجودی حساب شما کافی نیست.**")
        return

    db_query("UPDATE users SET balance = balance - ? WHERE user_id=?", (bet, user_id), commit=True)
    mode_fa = "زوج" if choice == "even" else "فرد"
    await message.reply(f"🎲 شما مبلغ **{bet} HRK** روی **{mode_fa}** بودن تاس شرط بستید.\n\nدر حال پرتاب...", parse_mode="Markdown")
    
    d = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(3.5)
    val = d.dice.value
    
    is_even = (val % 2 == 0)
    user_won = (is_even and choice == "even") or (not is_even and choice == "odd")
    
    if user_won:
        win_amount = bet * 1.8
        db_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (win_amount, user_id), commit=True)
        await message.reply(f"🎉 **برنده شدی!** تاس روی عدد **{val}** نشست.\n💰 مبلغ **{win_amount:.2f} HRK** به حسابت واریز شد.", parse_mode="Markdown")
    else:
        await message.reply(f"💥 **باختی!** تاس روی عدد **{val}** نشست (طرح پیش‌بینی اشتباه بود).", parse_mode="Markdown")

# ---- 3. حالت حدس عدد ----
@router.message(F.chat.type.in_({'group', 'supergroup'}), F.text.regexp(r'^guess (\d+(\.\d+)?) ([1-6])$', flags=re.IGNORECASE))
async def group_dice_guess(message: types.Message):
    match = re.match(r'^guess (\d+(\.\d+)?) ([1-6])$', message.text, re.IGNORECASE)
    bet = float(match.group(1))
    target_num = int(match.group(3))
    
    user_id = message.from_user.id
    user = db_query("SELECT balance FROM users WHERE user_id=?", (user_id,), fetchone=True)
    if not user or user[0] < bet:
        await message.reply("❌ **موجودی حساب شما کافی نیست.**")
        return

    db_query("UPDATE users SET balance = balance - ? WHERE user_id=?", (bet, user_id), commit=True)
    await message.reply(f"🎯 شما مبلغ **{bet} HRK** روی عدد **{target_num}** شرط بستید.\n\nدر حال پرتاب...", parse_mode="Markdown")
    
    d = await message.answer_dice(emoji="🎲")
    await asyncio.sleep(3.5)
    val = d.dice.value
    
    if val == target_num:
        win_amount = bet * 5.0
        db_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (win_amount, user_id), commit=True)
        await message.reply(f"🔥 **جکپات! دقیق حدس زدی!**\n💰 مبلغ **{win_amount:.2f} HRK** (۵ برابر شرط) به حسابت واریز شد.", parse_mode="Markdown")
    else:
        await message.reply(f"💥 **باختی!** تاس روی عدد **{val}** نشست.", parse_mode="Markdown")

# ================= FastAPI App Execution =================

@app.on_event("startup")
async def on_startup():
    if WEBHOOK_URL:
        clean_url = WEBHOOK_URL.rstrip('/')
        await bot.set_webhook(f"{clean_url}/webhook")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    update_data = await request.json()
    update = types.Update(**update_data)
    await dp.feed_update(bot, update)
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
async def serve_root():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
