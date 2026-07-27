#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ximera Model Agency — Telegram bot.
Приём заявок от моделей + админ-панель (одобрить / отклонить / связаться).
Только стандартная библиотека Python — никаких зависимостей.
"""
import json, os, time, sys, urllib.request, urllib.error, subprocess, threading, html

TOKEN     = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID  = int(os.environ.get("ADMIN_ID", "0") or 0)
CLAIM_CODE= os.environ.get("CLAIM_CODE", "").strip()      # /claim <код> — назначить себя админом
RUN_SECS  = int(os.environ.get("RUN_SECONDS", "19200"))   # ~5ч20м, затем мягкий выход (для CI)
DB_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "db.json")
API       = f"https://api.telegram.org/bot{TOKEN}/"

if not TOKEN:
    print("BOT_TOKEN не задан", file=sys.stderr); sys.exit(1)

# ─────────────────────────── Telegram API ───────────────────────────
def api(method, **params):
    data = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(API + method, data=data,
                                 headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=65) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try: j = json.loads(body)
            except Exception: j = {"ok": False, "description": body}
            if e.code == 409:      # другой инстанс уже опрашивает
                print("409 Conflict — жду освобождения long polling…")
                time.sleep(5); continue
            if e.code == 429:
                time.sleep(int(j.get("parameters", {}).get("retry_after", 3)) + 1); continue
            return j
        except Exception as ex:
            if attempt == 2:
                print("api error:", method, ex); return {"ok": False}
            time.sleep(2)
    return {"ok": False}

def send(chat_id, text, kb=None, preview=False):
    return api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
               reply_markup=kb, disable_web_page_preview=not preview)

def edit(chat_id, mid, text, kb=None):
    return api("editMessageText", chat_id=chat_id, message_id=mid, text=text,
               parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

def cb_answer(cb_id, text=None, alert=False):
    return api("answerCallbackQuery", callback_query_id=cb_id, text=text, show_alert=alert)

def ikb(rows):   return {"inline_keyboard": rows}
def btn(t, d):   return {"text": t, "callback_data": d}
def url_btn(t,u):return {"text": t, "url": u}
def rkb(rows, once=True):
    return {"keyboard": [[{"text": t} for t in r] for r in rows],
            "resize_keyboard": True, "one_time_keyboard": once}
RKB_REMOVE = {"remove_keyboard": True}

# ─────────────────────────── База (JSON-файл) ───────────────────────────
DB = {"apps": [], "state": {}, "next_id": 1, "admin": ADMIN_ID}
_dirty = False

def db_load():
    global DB
    try:
        with open(DB_PATH, encoding="utf-8") as f:
            loaded = json.load(f)
        DB.update(loaded)
        if ADMIN_ID: DB["admin"] = ADMIN_ID
    except Exception:
        pass

def db_save():
    global _dirty
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DB_PATH)
    _dirty = True

def git_push_loop():
    """Раз в 2 минуты коммитим базу заявок обратно в репозиторий (если менялась)."""
    global _dirty
    if os.environ.get("GIT_PERSIST", "") != "1":
        return
    while True:
        time.sleep(120)
        if not _dirty: continue
        _dirty = False
        try:
            subprocess.run(["git", "add", "data/db.json"], check=False)
            r = subprocess.run(["git", "commit", "-m", "data: заявки [skip ci]"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                subprocess.run(["git", "push"], check=False, capture_output=True)
        except Exception as e:
            print("git persist error:", e)

def is_admin(uid): return DB.get("admin") and uid == DB["admin"]

# ─────────────────────────── Анкета ───────────────────────────
STEPS = [
    ("name",    "Как вас зовут? (имя и фамилия)"),
    ("age",     "Сколько вам полных лет?"),
    ("city",    "Из какого вы города?"),
    ("params",  "Ваши параметры: рост, вес, объёмы (например: 175 / 52 / 86-60-90)"),
    ("exp",     "Есть ли опыт съёмок или работы моделью? Опишите коротко (если нет — напишите «нет опыта»)"),
    ("contact", "Как с вами связаться? Укажите @username в Telegram или телефон"),
]
PHOTO_STEP = "photos"

WELCOME = (
    "✨ <b>Ximera Model Agency</b>\n\n"
    "Здравствуйте! Мы ищем новых лиц для сотрудничества.\n"
    "Заполните короткую анкету — это займёт пару минут. "
    "Мы рассмотрим её и свяжемся с вами, если подойдёте.\n\n"
    "Нажмите кнопку ниже, чтобы начать."
)

def start_form(uid):
    DB["state"][str(uid)] = {"step": 0, "data": {}, "photos": []}
    db_save()

def state_of(uid): return DB["state"].get(str(uid))

def ask_step(chat_id, st):
    i = st["step"]
    if i < len(STEPS):
        key, q = STEPS[i]
        send(chat_id, f"<b>Вопрос {i+1} из {len(STEPS)+1}</b>\n{q}", RKB_REMOVE)
    else:
        send(chat_id, f"<b>Вопрос {len(STEPS)+1} из {len(STEPS)+1}</b>\n"
                      "Пришлите 1–5 своих фотографий (портрет и в полный рост).\n"
                      "Когда закончите — нажмите «Готово».",
             rkb([["✅ Готово"]], once=False))

def app_card(a, for_admin=True):
    u = f"@{a['username']}" if a.get("username") else "—"
    status = {"pending": "🕓 Ожидает", "approved": "✅ Одобрена", "rejected": "❌ Отклонена"}[a["status"]]
    e = lambda s: html.escape(str(s or "—"))
    t = (f"🆕 <b>Заявка #{a['id']}</b>  ·  {status}\n\n"
         f"👤 <b>Имя:</b> {e(a['data'].get('name'))}\n"
         f"🎂 <b>Возраст:</b> {e(a['data'].get('age'))}\n"
         f"📍 <b>Город:</b> {e(a['data'].get('city'))}\n"
         f"📏 <b>Параметры:</b> {e(a['data'].get('params'))}\n"
         f"🎬 <b>Опыт:</b> {e(a['data'].get('exp'))}\n"
         f"📞 <b>Контакт:</b> {e(a['data'].get('contact'))}\n")
    if for_admin:
        t += f"🔗 <b>Telegram:</b> {e(u)}  ·  <code>{a['user_id']}</code>\n"
        t += f"🕒 {time.strftime('%d.%m.%Y %H:%M', time.localtime(a['ts']))}"
    return t

def admin_kb(a):
    rows = []
    if a["status"] == "pending":
        rows.append([btn("✅ Одобрить", f"ok:{a['id']}"), btn("❌ Отклонить", f"no:{a['id']}")])
    rows.append([url_btn("💬 Написать модели", f"tg://user?id={a['user_id']}")])
    return ikb(rows)

def send_app_to_admin(a):
    admin = DB.get("admin")
    if not admin:
        return
    photos = a.get("photos") or []
    if photos:
        media = [{"type": "photo", "media": fid} for fid in photos[:5]]
        api("sendMediaGroup", chat_id=admin, media=media)
    send(admin, app_card(a), admin_kb(a))

def finish_form(uid, chat_id, user):
    st = state_of(uid)
    if not st: return
    a = {"id": DB["next_id"], "user_id": uid, "username": user.get("username"),
         "data": st["data"], "photos": st.get("photos", []),
         "status": "pending", "ts": int(time.time())}
    DB["next_id"] += 1
    DB["apps"].append(a)
    DB["state"].pop(str(uid), None)
    db_save()
    send(chat_id,
         "✅ <b>Спасибо! Ваша анкета отправлена.</b>\n\n"
         "Мы внимательно рассмотрим её и свяжемся с вами в Telegram, если вы нам подойдёте.\n"
         "Обычно это занимает 1–3 дня.", RKB_REMOVE)
    send_app_to_admin(a)

# ─────────────────────────── Админ-панель ───────────────────────────
def stats():
    apps = DB["apps"]
    p = sum(1 for a in apps if a["status"] == "pending")
    ok = sum(1 for a in apps if a["status"] == "approved")
    no = sum(1 for a in apps if a["status"] == "rejected")
    return len(apps), p, ok, no

def panel_text():
    total, p, ok, no = stats()
    return ("🛠 <b>Админ-панель</b>\n\n"
            f"📥 Всего заявок: <b>{total}</b>\n"
            f"🕓 Ожидают решения: <b>{p}</b>\n"
            f"✅ Одобрено: <b>{ok}</b>\n"
            f"❌ Отклонено: <b>{no}</b>\n\n"
            "Выберите раздел:")

def panel_kb():
    return ikb([
        [btn("🕓 Ожидают", "list:pending"), btn("✅ Одобренные", "list:approved")],
        [btn("❌ Отклонённые", "list:rejected"), btn("📋 Все заявки", "list:all")],
        [btn("🔄 Обновить", "panel")],
    ])

def list_apps(chat_id, kind, mid=None):
    apps = [a for a in DB["apps"] if kind == "all" or a["status"] == kind]
    apps = list(reversed(apps))[:10]
    titles = {"pending": "🕓 Ожидают решения", "approved": "✅ Одобренные",
              "rejected": "❌ Отклонённые", "all": "📋 Все заявки"}
    if not apps:
        text, kb = f"<b>{titles[kind]}</b>\n\nПока пусто.", ikb([[btn("⬅️ Назад", "panel")]])
    else:
        text = f"<b>{titles[kind]}</b>  (последние {len(apps)})\n\nНажмите на заявку, чтобы открыть:"
        rows = [[btn(f"#{a['id']} · {a['data'].get('name','—')[:20]} · {a['data'].get('city','—')[:14]}",
                     f"show:{a['id']}")] for a in apps]
        rows.append([btn("⬅️ Назад", "panel")])
        kb = ikb(rows)
    if mid: edit(chat_id, mid, text, kb)
    else:   send(chat_id, text, kb)

def find_app(aid):
    for a in DB["apps"]:
        if a["id"] == aid: return a
    return None

# ─────────────────────────── Обработчики ───────────────────────────
def on_message(m):
    chat_id = m["chat"]["id"]
    user = m.get("from", {})
    uid = user.get("id")
    text = (m.get("text") or "").strip()

    # ── команды
    if text.startswith("/start"):
        DB["state"].pop(str(uid), None); db_save()
        send(chat_id, WELCOME, ikb([[btn("📝 Оставить заявку", "apply")]]))
        return
    if text.startswith("/id"):
        send(chat_id, f"Ваш Telegram ID: <code>{uid}</code>"); return
    if text.startswith("/claim") and CLAIM_CODE:
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip() == CLAIM_CODE:
            DB["admin"] = uid; db_save()
            send(chat_id, "✅ Вы назначены администратором.\nОткройте панель: /admin")
        else:
            send(chat_id, "Неверный код.")
        return
    if text.startswith("/admin"):
        if is_admin(uid): send(chat_id, panel_text(), panel_kb())
        else:             send(chat_id, "Эта команда доступна только администратору.")
        return
    if text.startswith("/help"):
        send(chat_id, "Напишите /start, чтобы оставить заявку." +
                      ("\n\nАдмин: /admin — панель заявок." if is_admin(uid) else ""))
        return

    st = state_of(uid)
    if not st:
        send(chat_id, "Чтобы оставить заявку, нажмите кнопку ниже 👇",
             ikb([[btn("📝 Оставить заявку", "apply")]]))
        return

    # ── шаг с фото
    if st["step"] >= len(STEPS):
        if m.get("photo"):
            fid = m["photo"][-1]["file_id"]
            if len(st["photos"]) < 5:
                st["photos"].append(fid); db_save()
                send(chat_id, f"📸 Фото принято ({len(st['photos'])}/5). "
                              "Пришлите ещё или нажмите «Готово».",
                     rkb([["✅ Готово"]], once=False))
            else:
                send(chat_id, "Достаточно фото. Нажмите «Готово».", rkb([["✅ Готово"]], once=False))
            return
        if text.lower().replace("✅", "").strip() in ("готово", "готова", "done"):
            if not st["photos"]:
                send(chat_id, "Пришлите хотя бы одно фото — без него анкету не рассматриваем.")
                return
            finish_form(uid, chat_id, user); return
        send(chat_id, "Пришлите фотографию или нажмите «Готово».",
             rkb([["✅ Готово"]], once=False))
        return

    # ── текстовые шаги
    if not text:
        send(chat_id, "Пожалуйста, ответьте текстом."); return
    key, _ = STEPS[st["step"]]
    if key == "age":
        digits = "".join(c for c in text if c.isdigit())
        if not digits or not (14 <= int(digits) <= 70):
            send(chat_id, "Укажите возраст числом (например: 21)."); return
        if int(digits) < 18:
            DB["state"].pop(str(uid), None); db_save()
            send(chat_id, "Спасибо за интерес! К сожалению, мы работаем только с совершеннолетними "
                          "(18+). Будем рады видеть вас позже.", RKB_REMOVE)
            return
        text = digits
    st["data"][key] = text[:400]
    st["step"] += 1
    db_save()
    ask_step(chat_id, st)

def on_callback(cq):
    cid   = cq["id"]
    data  = cq.get("data", "")
    msg   = cq.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    mid   = msg.get("message_id")
    uid   = cq["from"]["id"]

    if data == "apply":
        cb_answer(cid)
        start_form(uid)
        send(chat_id, "Отлично! Начинаем 👇")
        ask_step(chat_id, state_of(uid))
        return

    if not is_admin(uid):
        cb_answer(cid, "Недоступно", True); return

    if data == "panel":
        cb_answer(cid); edit(chat_id, mid, panel_text(), panel_kb()); return

    if data.startswith("list:"):
        cb_answer(cid); list_apps(chat_id, data.split(":")[1], mid); return

    if data.startswith("show:"):
        a = find_app(int(data.split(":")[1]))
        cb_answer(cid)
        if not a: return
        if a.get("photos"):
            api("sendMediaGroup", chat_id=chat_id,
                media=[{"type": "photo", "media": f} for f in a["photos"][:5]])
        send(chat_id, app_card(a), admin_kb(a))
        return

    if data.startswith(("ok:", "no:")):
        aid = int(data.split(":")[1])
        a = find_app(aid)
        if not a: cb_answer(cid, "Заявка не найдена", True); return
        approve = data.startswith("ok:")
        a["status"] = "approved" if approve else "rejected"
        db_save()
        cb_answer(cid, "Одобрено ✅" if approve else "Отклонено ❌")
        edit(chat_id, mid, app_card(a), admin_kb(a))
        # уведомление модели
        if approve:
            send(a["user_id"],
                 "🎉 <b>Ваша анкета одобрена!</b>\n\n"
                 "Мы рассмотрели вашу заявку — вы нам подходите. "
                 "В ближайшее время с вами свяжется представитель агентства здесь, в Telegram.")
        else:
            send(a["user_id"],
                 "Спасибо за вашу заявку!\n\n"
                 "К сожалению, сейчас мы не готовы предложить сотрудничество. "
                 "Это не оценка вас — просто под текущие проекты ищем другой типаж. "
                 "Будем рады вашей анкете в будущем.")
        if approve:
            send(chat_id, f"Напишите модели напрямую 👇",
                 ikb([[url_btn(f"💬 Открыть чат с {a['data'].get('name','моделью')}",
                               f"tg://user?id={a['user_id']}")]]))
        return

    cb_answer(cid)

# ─────────────────────────── Основной цикл ───────────────────────────
def main():
    db_load()
    me = api("getMe").get("result", {})
    print("Бот запущен:", me.get("username"), "| админ:", DB.get("admin"))
    api("setMyCommands", commands=[
        {"command": "start", "description": "Оставить заявку"},
        {"command": "admin", "description": "Панель администратора"},
        {"command": "id",    "description": "Узнать свой Telegram ID"},
    ])
    threading.Thread(target=git_push_loop, daemon=True).start()

    offset = None
    started = time.time()
    while time.time() - started < RUN_SECS:
        r = api("getUpdates", offset=offset, timeout=50,
                allowed_updates=["message", "callback_query"])
        if not r.get("ok"):
            time.sleep(3); continue
        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
            try:
                if "message" in upd:            on_message(upd["message"])
                elif "callback_query" in upd:   on_callback(upd["callback_query"])
            except Exception as e:
                print("handler error:", repr(e))
    print("Плановое завершение — воркфлоу перезапустит бота.")

if __name__ == "__main__":
    main()
