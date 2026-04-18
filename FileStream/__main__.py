import sys
import asyncio
import logging
import traceback
import logging.handlers as handlers
from FileStream.config import Telegram, Server
from aiohttp import web
from pyrogram import idle

from FileStream.bot import FileStream
from FileStream.server import web_server
from FileStream.bot.clients import initialize_clients

logging.basicConfig(
    level=logging.INFO,
    datefmt="%d/%m/%Y %H:%M:%S",
    format='[%(asctime)s] {%(pathname)s:%(lineno)d} %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(stream=sys.stdout),
              handlers.RotatingFileHandler("streambot.log", mode="a", maxBytes=104857600, backupCount=2, encoding="utf-8")],)

logging.getLogger("aiohttp").setLevel(logging.ERROR)
logging.getLogger("pyrogram").setLevel(logging.ERROR)
logging.getLogger("aiohttp.web").setLevel(logging.ERROR)

server = web.AppRunner(web_server())

loop = asyncio.get_event_loop()

async def start_services():
    print()
    if Telegram.SECONDARY:
        print("------------------ Starting as Secondary Server ------------------")
    else:
        print("------------------- Starting as Primary Server -------------------")
    print()
    print("-------------------- Initializing Telegram Bot --------------------")


    await FileStream.start()
    bot_info = await FileStream.get_me()
    FileStream.id = bot_info.id
    FileStream.username = bot_info.username
    FileStream.fname=bot_info.first_name

    # ── Set bot command menus ──────────────────────────────────
    try:
        from pyrogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
        # Public menu — only /start visible to all users
        await FileStream.set_bot_commands(
            commands=[
                BotCommand("start", "Start the bot and get help"),
            ],
            scope=BotCommandScopeDefault()
        )
        # Owner private chat — all admin commands visible
        await FileStream.set_bot_commands(
            commands=[
                BotCommand("start",           "Start the bot"),
                BotCommand("status",          "Bot status, users & link stats"),
                BotCommand("broadcast",       "Broadcast a message to all users"),
                BotCommand("broadcaststatus", "Check active broadcast progress"),
                BotCommand("ban",             "Ban a user — /ban <user_id>"),
                BotCommand("unban",           "Unban a user — /unban <user_id>"),
                BotCommand("del",             "Delete a file link — /del <file_id>"),
            ],
            scope=BotCommandScopeChat(chat_id=Telegram.OWNER_ID)
        )
        print("✅ Bot commands set (public: /start | admin: all commands)")
    except Exception as e:
        print(f"⚠️  Could not set bot commands: {e}")
    # ────────────────────────────────────────────────────────────
    print("------------------------------ DONE ------------------------------")
    print()
    print("---------------------- Initializing Clients ----------------------")
    await initialize_clients()
    print("------------------------------ DONE ------------------------------")
    print()
    print("--------------------- Initializing Web Server ---------------------")
    await server.setup()
    await web.TCPSite(server, "0.0.0.0", Server.PORT).start()
    print("------------------------------ DONE ------------------------------")
    print()
    print("------------------------- Service Started -------------------------")
    print("                        bot =>> {}".format(bot_info.first_name))
    if bot_info.dc_id:
        print("                        DC ID =>> {}".format(str(bot_info.dc_id)))
    print(" URL =>> {}".format(Server.URL))
    print("------------------------------------------------------------------")
    await idle()

async def cleanup():
    await server.cleanup()
    await FileStream.stop()

if __name__ == "__main__":
    try:
        loop.run_until_complete(start_services())
    except KeyboardInterrupt:
        pass
    except Exception as err:
        logging.error(traceback.format_exc())
    finally:
        loop.run_until_complete(cleanup())
        loop.stop()
        print("------------------------ Stopped Services ------------------------")