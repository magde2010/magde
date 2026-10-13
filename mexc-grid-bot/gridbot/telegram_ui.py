import os, asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from .config import config_from_env

def allowed(update):
    raw=os.getenv('TELEGRAM_ALLOWED_USER_IDS','').strip()
    if not raw or update.effective_user is None: return False
    try: return update.effective_user.id in {int(x.strip()) for x in raw.split(',') if x.strip()}
    except ValueError: return False
async def _deny(update):
    if update.effective_message: await update.effective_message.reply_text('غير مصرح. اضبط TELEGRAM_ALLOWED_USER_IDS في ملف .env الخاص.')
async def _service(update,context):
    if not allowed(update): await _deny(update); return None
    return context.application.bot_data['service']
async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return await _deny(update)
    await update.effective_message.reply_text('MEXC Grid Bot مستقل\n/status الحالة\n/run تشغيل الاستراتيجية\n/stop إيقاف فتح صفقات جديدة\n/open_orders الأوامر المحلية\n/emergency_stop [cancel] إيقاف طوارئ، وcancel لإلغاء أوامر هذا البوت فقط\n/clear_emergency إزالة الإيقاف بعد المراجعة\n/help المساعدة')
async def help_cmd(update,context): await start(update,context)
async def status(update,context):
    s=await _service(update,context)
    if s: await update.effective_message.reply_text(s.status_text())
async def run_strategy(update,context):
    s=await _service(update,context)
    if not s: return
    p=s.db.position()
    if p['emergency_stop'] or p['state']=='FROZEN': return await update.effective_message.reply_text('رفض التشغيل: يوجد Emergency Stop أو حالة FROZEN. عالج السبب أولًا.')
    s.db.update_position(running=1,state='ACTIVE'); s.db.event('strategy_started',{'by':update.effective_user.id})
    await update.effective_message.reply_text('تم تفعيل الاستراتيجية. ستظل LIVE مقفولة ما لم تكن الإعدادات الصريحة صحيحة.')
async def stop(update,context):
    s=await _service(update,context)
    if s: s.db.update_position(running=0,state='PAUSED'); s.db.event('strategy_paused',{'by':update.effective_user.id}); await update.effective_message.reply_text('تم إيقاف إنشاء أوامر استراتيجية جديدة. الأوامر القائمة لم تُلغَ.')
async def open_orders(update,context):
    s=await _service(update,context)
    if not s: return
    rows=s.db.orders(); rows=[o for o in rows if o['status'] in {'pending','open','unknown','partially_filled','cancel_pending'}]
    if not rows: return await update.effective_message.reply_text('لا توجد أوامر محلية مفتوحة.')
    lines=[f"{o['role']} L{o['level']} | {o['side']} | {o['status']} | qty={o['amount']} @ {o['price']} | id={o['exchange_order_id'] or 'غير مؤكد'}" for o in rows]
    await update.effective_message.reply_text('\n'.join(lines)[:3900])
async def emergency(update,context):
    s=await _service(update,context)
    if not s: return
    s.db.update_position(emergency_stop=1,running=0,state='EMERGENCY_STOP'); s.db.event('emergency_stop',{'by':update.effective_user.id,'cancel_requested':bool(context.args and context.args[0].lower()=='cancel')})
    msg='Emergency Stop مفعل: لن تُنشأ أوامر جديدة.'
    if context.args and context.args[0].lower()=='cancel':
        try: s._cancel_owned({'initial','dca','tp'}); msg+=' أُرسلت طلبات إلغاء الأوامر المسجلة لهذا البوت فقط.'
        except Exception: msg+=' تعذر تأكيد إلغاء كل الأوامر؛ الحالة مجمدة وتحتاج مراجعة.'
    else: msg+=' الأوامر القائمة لم تُلغَ. استخدم /emergency_stop cancel إذا أردت إلغاء أوامر هذا البوت المسجلة.'
    await update.effective_message.reply_text(msg)
async def clear_emergency(update,context):
    s=await _service(update,context)
    if not s: return
    if any(o['status']=='unknown' for o in s.db.open_orders()): return await update.effective_message.reply_text('لا يمكن إزالة الإيقاف: توجد أوامر غير مؤكدة. راجع MEXC وسجل الأوامر أولًا.')
    s.db.update_position(emergency_stop=0,running=0,state='PAUSED'); s.db.event('emergency_cleared',{'by':update.effective_user.id})
    await update.effective_message.reply_text('تمت إزالة Emergency Stop، لكن الاستراتيجية ما زالت متوقفة. استخدم /run للتشغيل.')
async def set_budget(update,context):
    s=await _service(update,context)
    if not s: return
    await update.effective_message.reply_text('الميزانية تُضبط عبر GRID_TOTAL_BUDGET في ملف .env ثم إعادة تشغيل البوت. لا أغيّرها أثناء وجود أوامر أو مركز مفتوح لتجنب تجاوز الحدود.')
async def _post_init(app): app.bot_data['monitor_task']=asyncio.create_task(app.bot_data['service'].run_forever())
async def _post_shutdown(app):
    task=app.bot_data.get('monitor_task')
    if task: task.cancel(); await asyncio.gather(task,return_exceptions=True)
def run_telegram(service):
    token=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
    if not token: raise RuntimeError('Set TELEGRAM_BOT_TOKEN in the private .env file')
    app=Application.builder().token(token).post_init(_post_init).post_shutdown(_post_shutdown).build(); app.bot_data['service']=service
    for cmd,fn in [('start',start),('help',help_cmd),('status',status),('run',run_strategy),('stop',stop),('open_orders',open_orders),('emergency_stop',emergency),('clear_emergency',clear_emergency),('set_budget',set_budget)]: app.add_handler(CommandHandler(cmd,fn))
    app.run_polling()
