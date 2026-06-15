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
HRK_RATE = 10000 # تومان
FEE_PERCENT = 0.075 # 7.5% کمیسیون ربات

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()
router = Router()
dp.include_router(router)

# ================= Database Setup =================
def init_db():
    conn = sqlite3.connect('casino.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, username TEXT, phone TEXT, balance REAL DEFAULT 0, 
                  referrer INTEGER, pay_count INTEGER DEFAULT 0, withdraw_count INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS channels (channel_id TEXT PRIMARY KEY)''')
    # جدول موقت برای بازی‌های گروهی
    c.execute('''CREATE TABLE IF NOT EXISTS active_games 
                 (game_id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, p1_id INTEGER, p2_id INTEGER, 
                  bet REAL, dice_count INTEGER, status TEXT)''')
    conn.commit()
    conn.close()

def db_query(query, args=(), fetchone=False, fetchall=False, commit=False):
    conn = sqlite3.connect('casino.db')
    c = conn.cursor()
    c.execute(query, args)
    res = None
    if fetchone: res = c.fetchone()
    if fetchall: res = c.fetchall()
    if commit: conn.commit()
    conn.close()
    return res

init_db()

# ================= FSM States =================
class WithdrawFSM(StatesGroup):
    type = State() # ton or irt
    amount = State()
    destination = State() # card or wallet
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

# ================= Middleware / Helpers =================
async def check_channels(user_id):
    channels = db_query("SELECT channel_id FROM channels", fetchall=True)
    if not channels: return True
    for (ch,) in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch, user_id=user_id)
            if member.status in ['left', 'kicked']: return False
        except TelegramBadRequest:
            pass # ربات در کانال ادمین نیست
    return True

# ================= Private Chat Handlers =================

@router.message(CommandStart(), F.chat.type == 'private')
async def start_cmd(message: types.Message, command: Command):
    args = command.args
    referrer = int(args) if args and args.isdigit() else None
    
    user = db_query("SELECT phone FROM users WHERE user_id=?", (message.from_user.id,), fetchone=True)
    if not user:
        db_query("INSERT OR IGNORE INTO users (user_id, username, referrer) VALUES (?, ?, ?)", 
                 (message.from_user.id, message.from_user.username, referrer), commit=True)
        
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 ارسال شماره تماس", request_contact=True)]], resize_keyboard=True)
        await message.answer("👋 سلام! برای استفاده از امکانات ربات ابتدا باید شماره تماس خود را تایید کنید:", reply_markup=kb)
    else:
        is_joined = await check_channels(message.from_user.id)
        if not is_joined:
            await message.answer("❌ ابتدا باید در کانال‌های اسپانسر عضو شوید و سپس دوباره /start را ارسال کنید.")
            return
        await message.answer("🎮 به ربات بازی HRK خوش آمدید!", reply_markup=main_menu())

@router.message(F.contact, F.chat.type == 'private')
async def contact_handler(message: types.Message):
    if message.contact.user_id == message.from_user.id:
        db_query("UPDATE users SET phone=? WHERE user_id=?", (message.contact.phone_number, message.from_user.id), commit=True)
        await message.answer("✅ ثبت نام تکمیل شد.", reply_markup=main_menu())
    else:
        await message.answer("⚠️ لطفاً شماره خودتان را ارسال کنید.")

@router.message(F.text == "🏛 حساب کاربری", F.chat.type == 'private')
async def account_info(message: types.Message):
    user = db_query("SELECT username, balance, pay_count, withdraw_count FROM users WHERE user_id=?", (message.from_user.id,), fetchone=True)
    if user:
        text = f"👤 **اطلاعات حساب شما:**\n\n" \
               f"آیدی عددی: `{message.from_user.id}`\n" \
               f"یوزرنیم: @{user[0] or 'ندارد'}\n" \
               f"موجودی: **{user[1]:.2f} HRK**\n" \
               f"تعداد واریز: {user[2]}\n" \
               f"تعداد برداشت: {user[3]}"
        await message.answer(text, parse_mode="Markdown")

@router.message(F.text == "💰 کیف پول", F.chat.type == 'private')
async def wallet_menu(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 واریز (شارژ)", callback_data="wallet_deposit"),
         InlineKeyboardButton(text="📤 برداشت (تسویه)", callback_data="wallet_withdraw")]
    ])
    await message.answer("💳 **بخش کیف پول**\nعملیات مورد نظر را انتخاب کنید:", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "wallet_deposit")
async def deposit_info(call: types.CallbackQuery):
    text = f"📥 **شارژ حساب کاربری**\n\n" \
           f"نرخ هر توکن: **{HRK_RATE:,} تومان**\n" \
           f"شما می‌توانید مبلغ خود را به صورت **ریالی** یا **کریپتو (TON)** واریز کنید.\n\n" \
           f"💬 برای دریافت شماره کارت یا آدرس ولت و افزایش شارژ، به پشتیبانی پیام دهید:"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎧 ارتباط با پشتیبانی", url=f"https://t.me/{SUPPORT_ID.replace('@','')}")]])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

# --- Withdraw Flow ---
@router.callback_query(F.data == "wallet_withdraw")
async def withdraw_start(call: types.CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 برداشت ارزی (TON)", callback_data="wd_ton"),
         InlineKeyboardButton(text="💳 برداشت ریالی", callback_data="wd_irt")]
    ])
    await call.message.edit_text("📤 نوع برداشت خود را انتخاب کنید:", reply_markup=kb)

@router.callback_query(F.data.in_(["wd_ton", "wd_irt"]))
async def withdraw_type(call: types.CallbackQuery, state: FSMContext):
    w_type = "تون کوین (TON)" if call.data == "wd_ton" else "ریالی (کارت بانکی)"
    await state.update_data(type=call.data)
    user = db_query("SELECT balance FROM users WHERE user_id=?", (call.from_user.id,), fetchone=True)
    text = f"شما درخواست برداشت **{w_type}** را دارید.\n" \
           f"موجودی فعلی شما: **{user[0]:.2f} HRK**\n\n" \
           f"مقدار کوین (HRK) که قصد برداشت دارید را به صورت عدد لاتین ارسال کنید:"
    await call.message.edit_text(text, parse_mode="Markdown")
    await state.set_state(WithdrawFSM.amount)

@router.message(WithdrawFSM.amount)
async def withdraw_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        user = db_query("SELECT balance FROM users WHERE user_id=?", (message.from_user.id,), fetchone=True)
        if amount <= 0 or amount > user[0]:
            await message.answer("❌ مبلغ نامعتبر است یا موجودی کافی نیست. مجدد ارسال کنید:")
            return
        
        await state.update_data(amount=amount)
        data = await state.get_data()
        
        if data['type'] == 'wd_irt':
            await message.answer("💳 لطفاً شماره کارت 16 رقمی خود را به همراه نام صاحب حساب ارسال کنید:")
        else:
            await message.answer("💎 لطفاً آدرس کیف پول TON خود را ارسال کنید:")
        await state.set_state(WithdrawFSM.destination)
    except ValueError:
        await message.answer("❌ لطفاً فقط عدد ارسال کنید.")

@router.message(WithdrawFSM.destination)
async def withdraw_dest(message: types.Message, state: FSMContext):
    await state.update_data(destination=message.text)
    data = await state.get_data()
    
    if data['type'] == 'wd_ton':
        kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="نیاز نیست")]], resize_keyboard=True)
        await message.answer("📝 در صورت نیاز به Memo / Comment آن را ارسال کنید، در غیر این صورت دکمه زیر را بزنید:", reply_markup=kb)
        await state.set_state(WithdrawFSM.memo)
    else:
        await finish_withdraw(message, state)

@router.message(WithdrawFSM.memo)
async def withdraw_memo(message: types.Message, state: FSMContext):
    await state.update_data(memo=message.text if message.text != "نیاز نیست" else "ندارد")
    await finish_withdraw(message, state)

async def finish_withdraw(message: types.Message, state: FSMContext):
    data = await state.get_data()
    amount = data['amount']
    dest = data['destination']
    memo = data.get('memo', 'ندارد')
    w_type = "ریالی" if data['type'] == 'wd_irt' else "ارزی (TON)"
    
    # کم کردن موجودی و ثبت درخواست
    db_query("UPDATE users SET balance = balance - ?, withdraw_count = withdraw_count + 1 WHERE user_id=?", (amount, message.from_user.id), commit=True)
    
    req_text = f"🚨 **درخواست برداشت جدید**\n\n" \
               f"👤 کاربر: `{message.from_user.id}`\n" \
               f"نوع: {w_type}\nمبلغ: {amount} HRK\nمقصد: `{dest}`\nممو: `{memo}`"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تایید و واریز شد", callback_data=f"approve_{message.from_user.id}_{amount}"),
         InlineKeyboardButton(text="❌ رد درخواست", callback_data=f"reject_{message.from_user.id}_{amount}")]
    ])
    
    await bot.send_message(ADMIN_ID, req_text, reply_markup=kb, parse_mode="Markdown")
    await message.answer("✅ درخواست برداشت شما با موفقیت ثبت شد و پس از بررسی ادمین به حساب شما واریز خواهد شد.", reply_markup=main_menu())
    await state.clear()

# --- Admin Panel ---
@router.message(Command("panel"), F.from_user.id == ADMIN_ID)
async def admin_panel(message: types.Message):
    await message.answer("👨‍💻 پنل مدیریت:", reply_markup=admin_menu())

@router.message(F.text == "💵 شارژ کاربر", F.from_user.id == ADMIN_ID)
async def admin_charge_ask(message: types.Message, state: FSMContext):
    await message.answer("آیدی عددی کاربر مورد نظر را ارسال کنید:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(AdminFSM.charge_user)

@router.message(AdminFSM.charge_user, F.from_user.id == ADMIN_ID)
async def admin_charge_user(message: types.Message, state: FSMContext):
    await state.update_data(user_id=int(message.text))
    await message.answer("مقدار HRK برای شارژ را ارسال کنید:")
    await state.set_state(AdminFSM.charge_amount)

@router.message(AdminFSM.charge_amount, F.from_user.id == ADMIN_ID)
async def admin_charge_exec(message: types.Message, state: FSMContext):
    data = await state.get_data()
    amount = float(message.text)
    u_id = data['user_id']
    
    db_query("UPDATE users SET balance = balance + ?, pay_count = pay_count + 1 WHERE user_id=?", (amount, u_id), commit=True)
    await message.answer(f"✅ حساب کاربر {u_id} به مبلغ {amount} HRK شارژ شد.", reply_markup=admin_menu())
    try:
        await bot.send_message(u_id, f"🎉 حساب کاربری شما مبلغ **{amount} HRK** شارژ شد!", parse_mode="Markdown")
    except: pass
    await state.clear()

# ================= Group Game Logic =================

@router.message(F.chat.type.in_({'group', 'supergroup'}), F.text.lower().in_(["wallet", "balance"]))
async def group_balance(message: types.Message):
    user = db_query("SELECT balance FROM users WHERE user_id=?", (message.from_user.id,), fetchone=True)
    if user:
        bal = user[0]
        await message.reply(f"💰 Balance: **{bal:.2f} HRK**\n🇮🇷 IRR: **{(bal * HRK_RATE):,.0f} Toman**", parse_mode="Markdown")
    else:
        await message.reply("Register in bot first: /start")

# Match English commands: "dice 30" or "3 dice 30"
@router.message(F.chat.type.in_({'group', 'supergroup'}), F.text.regexp(r'^(\d+ )?dice (\d+(\.\d+)?)$', flags=re.IGNORECASE))
async def group_dice_init(message: types.Message):
    match = re.match(r'^(\d+ )?dice (\d+(\.\d+)?)$', message.text, re.IGNORECASE)
    dice_count = int(match.group(1).strip()) if match.group(1) else 1
    bet = float(match.group(2))
    
    user = db_query("SELECT balance FROM users WHERE user_id=?", (message.from_user.id,), fetchone=True)
    if not user or user[0] < bet:
        await message.reply("❌ Insufficient balance or not registered.")
        return

    db_query("INSERT INTO active_games (chat_id, p1_id, bet, dice_count, status) VALUES (?, ?, ?, ?, 'waiting')",
             (message.chat.id, message.from_user.id, bet, dice_count), commit=True)
    game_id = db_query("SELECT last_insert_rowid()", fetchone=True)[0]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Play with Bot", callback_data=f"playbot_{game_id}"),
         InlineKeyboardButton(text="👤 Join Game (User)", callback_data=f"playuser_{game_id}")]
    ])
    
    text = f"🎲 **Game Created!**\nPlayer: {message.from_user.first_name}\nBet: {bet} HRK\nMode: {dice_count} Dice\n\nChoose opponent:"
    await message.reply(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data.startswith("playbot_"))
async def play_vs_bot(call: types.CallbackQuery):
    game_id = int(call.data.split("_")[1])
    game = db_query("SELECT p1_id, bet, dice_count, status FROM active_games WHERE game_id=?", (game_id,), fetchone=True)
    
    if not game or game[3] != 'waiting':
        await call.answer("Game already started or expired.", show_alert=True)
        return
    if call.from_user.id != game[0]:
        await call.answer("Only the creator can start with the bot.", show_alert=True)
        return

    db_query("UPDATE active_games SET status='playing' WHERE game_id=?", (game_id,), commit=True)
    await call.message.edit_text("🎲 Rolling vs Bot...")
    
    p1_id, bet, d_count = game[0], game[1], game[2]
    db_query("UPDATE users SET balance = balance - ? WHERE user_id=?", (bet, p1_id), commit=True)
    
    p1_score = 0
    bot_score = 0
    
    await call.message.answer(f"👤 {call.from_user.first_name}'s turn:")
    for _ in range(d_count):
        d = await call.message.answer_dice(emoji="🎲")
        p1_score += d.dice.value
        await asyncio.sleep(2)
        
    await call.message.answer(f"🤖 Bot's turn:")
    for _ in range(d_count):
        d = await call.message.answer_dice(emoji="🎲")
        bot_score += d.dice.value
        await asyncio.sleep(2)

    await process_winner(call.message, p1_id, None, p1_score, bot_score, bet)

async def process_winner(message, p1_id, p2_id, score1, score2, bet):
    pool = bet * 2
    rake = pool * FEE_PERCENT
    win_amount = pool - rake
    
    p1_name = (await bot.get_chat(p1_id)).first_name
    p2_name = "Bot" if not p2_id else (await bot.get_chat(p2_id)).first_name
    
    if score1 > score2:
        db_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (win_amount, p1_id), commit=True)
        winner, win_score, lose_score = p1_name, score1, score2
        win_id = p1_id
    elif score2 > score1:
        if p2_id: db_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (win_amount, p2_id), commit=True)
        winner, win_score, lose_score = p2_name, score2, score1
        win_id = p2_id
    else:
        db_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (bet, p1_id), commit=True)
        if p2_id: db_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (bet, p2_id), commit=True)
        await message.answer(f"🤝 **Draw!**\nScores: {score1}-{score2}\nBets refunded.", parse_mode="Markdown")
        return

    text = f"🎉 **{winner} Won!**\n\n" \
           f"Scores: {win_score} - {lose_score}\n" \
           f"💰 Won Amount: **{win_amount:.2f} HRK** (Fee deducted)"
    
    # Referral Bonus Logic
    if win_id:
        ref = db_query("SELECT referrer FROM users WHERE user_id=?", (win_id,), fetchone=True)
        if ref and ref[0]:
            ref_bonus = bet * 0.20
            db_query("UPDATE users SET balance = balance + ? WHERE user_id=?", (ref_bonus, ref[0]), commit=True)
            
    await message.answer(text, parse_mode="Markdown")

# ================= FastAPI App Execution =================

@app.on_event("startup")
async def on_startup():
    if WEBHOOK_URL:
        await bot.set_webhook(f"{WEBHOOK_URL}/webhook")

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
