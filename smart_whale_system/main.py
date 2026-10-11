"""
نظام الذكي الجديد — نقطة التشغيل الرئيسية
Smart Whale System v1.0
"""
import logging
import time
import signal
import sys
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

import config
from database import init_db, set_system_running, is_system_running, get_open_positions
from strategy import SmartStrategy
from notifier import notify
from coinlobster import CoinLobsterClient
from mexc_trader import MexcTrader

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("smart_whale")

# Global
scheduler: BackgroundScheduler = None
strategy: SmartStrategy = None
_running = True


def shutdown(signum=None, frame=None):
    global _running
    logger.info("جاري إيقاف النظام...")
    _running = False
    set_system_running(False)
    if scheduler:
        scheduler.shutdown(wait=False)
    notify(f"🛑 تم إيقاف <b>{config.SYSTEM_NAME}</b>")
    sys.exit(0)


def check_environment():
    """فحص أن كل المتغيرات المطلوبة موجودة"""
    missing = []
    if not config.MEXC_API_KEY:
        missing.append("MEXC_API_KEY")
    if not config.MEXC_API_SECRET:
        missing.append("MEXC_API_SECRET")
    if not config.TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not config.ADMIN_TELEGRAM_ID:
        missing.append("ADMIN_TELEGRAM_ID")

    if missing:
        msg = f"❌ متغيرات ناقصة: {', '.join(missing)}"
        logger.error(msg)
        print(msg)
        sys.exit(1)

    # اختبار سريع للـ APIs
    cl = CoinLobsterClient()
    if not cl.health():
        logger.warning("CoinLobster authenticated radar غير متاح؛ Flow Engine سيستخدم الرادار العام كـfallback")

    mexc = MexcTrader()
    try:
        usdt = mexc.get_free_usdt()
        logger.info(f"رصيد USDT المتاح: {usdt:.2f}")
    except Exception as e:
        logger.error(f"فشل الاتصال بـ MEXC: {e}")
        sys.exit(1)


def start_system():
    """تشغيل النظام"""
    global strategy, scheduler

    logger.info("=" * 50)
    logger.info(f"بدء {config.SYSTEM_NAME} v{config.SYSTEM_VERSION}")
    logger.info("=" * 50)

    check_environment()
    init_db()
    set_system_running(True)

    strategy = SmartStrategy()

    # إشعار البدء
    free = strategy.mexc.get_free_usdt()
    notify(
        f"🚀 <b>تم تشغيل {config.SYSTEM_NAME}</b>\n\n"
        f"رأس المال المخصص: <b>{config.ALLOCATED_CAPITAL:.0f} USDT</b>\n"
        f"حجم الصفقة: <b>{config.POSITION_SIZE_USDT:.0f} USDT</b>\n"
        f"أقصى صفقات: <b>{config.MAX_OPEN_POSITIONS}</b>\n"
        f"مضاعف الحيتان المطلوب: <b>{config.MIN_MULTIPLE}x</b>\n"
        f"نافذة الرادار: <b>{config.RADAR_WINDOW}</b>\n"
        f"وقف الخسارة: <b>-{config.STOP_LOSS_PCT}%</b>\n"
        f"الرصيد الحر حالياً: <b>{free:.2f} USDT</b>\n\n"
        f"النظام شغال أوتوماتيك الآن ✅"
    )

    # Scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        strategy.run_cycle,
        trigger=IntervalTrigger(minutes=config.CHECK_INTERVAL_MINUTES),
        id="smart_cycle",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()

    # أول دورة فوراً
    logger.info("تشغيل أول دورة فحص...")
    strategy.run_cycle()

    # البقاء شغال
    logger.info(f"النظام يعمل — فحص كل {config.CHECK_INTERVAL_MINUTES} دقائق")
    try:
        while _running:
            time.sleep(30)
    except KeyboardInterrupt:
        shutdown()


def status_only():
    """طباعة الحالة فقط بدون تشغيل"""
    init_db()
    strategy = SmartStrategy()
    print(strategy.manual_status())
    positions = get_open_positions()
    for p in positions:
        print(f"  {p.coin}: entry={p.entry_price} amount={p.amount} sl={p.stop_loss} tp1={p.take_profit_1}")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        status_only()
    else:
        start_system()
