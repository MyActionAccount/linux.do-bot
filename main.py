# -*- coding: utf-8 -*-
import os
import time
import random
import logging
import platform
import requests
import html
import io
import telegram
import asyncio
from datetime import datetime
from configparser import ConfigParser
from tabulate import tabulate
from playwright.sync_api import sync_playwright, TimeoutError
from config import reply_generator
from telegram.ext import Application
from telegram import Bot

# I stumbled upon this site thinking it might be a promising open-source Linux community. After exploring a bit, it seems like it's still in its early stages and doesn't quite live up to the 'community' label yet. There’s no shortage of overconfident individuals here, but it feels more like an amateurish forum rather than a serious place for Linux enthusiasts.

# 创建一个 StringIO 对象用于捕获日志
log_stream = io.StringIO()

# 创建日志记录器
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# 创建控制台输出的处理器
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

# 创建 log_stream 处理器
stream_handler = logging.StreamHandler(log_stream)
stream_handler.setLevel(logging.INFO)

# 创建格式化器
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# 为处理器设置格式化器
console_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

# 将处理器添加到日志记录器中
logger.addHandler(console_handler)
logger.addHandler(stream_handler)

# 自动判断运行环境
IS_GITHUB_ACTIONS = 'GITHUB_ACTIONS' in os.environ
IS_SERVER = platform.system() == "Linux" and not IS_GITHUB_ACTIONS

# 从配置文件或环境变量中读取配置信息
def load_config():
    config = ConfigParser()
    if IS_SERVER:
        config_file = './config/config.ini'
    elif IS_GITHUB_ACTIONS:
        config_file = None
    else:
        config_file = 'config/config.ini'
    
    if config_file and os.path.exists(config_file):
        config.read(config_file)
    
    return config

config = load_config()

USERNAME = os.getenv("LINUXDO_USERNAME", config.get('credentials', 'username', fallback=None))
PASSWORD = os.getenv("LINUXDO_PASSWORD", config.get('credentials', 'password', fallback=None))
LIKE_PROBABILITY = float(os.getenv("LIKE_PROBABILITY", config.get('settings', 'like_probability', fallback='0.02')))
REPLY_PROBABILITY = float(os.getenv("REPLY_PROBABILITY", config.get('settings', 'reply_probability', fallback='0')))
COLLECT_PROBABILITY = float(os.getenv("COLLECT_PROBABILITY", config.get('settings', 'collect_probability', fallback='0.02')))
HOME_URL = config.get('urls', 'home_url', fallback="https://linux.do/")
CONNECT_URL = config.get('urls', 'connect_url', fallback="https://connect.linux.do/")
USE_TELEGRAM = os.environ.get('USE_TELEGRAM', 'false').lower() == 'true'
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", config.get('telegram', 'bot_token', fallback=None))
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", config.get('telegram', 'chat_id', fallback=None))
MAX_TOPICS = int(os.getenv("MAX_TOPICS", config.get('settings', 'max_topics', fallback='10')))

# 检查必要配置
missing_configs = []

if not USERNAME:
    missing_configs.append("USERNAME")
if not PASSWORD:
    missing_configs.append("PASSWORD")
if USE_TELEGRAM and not TELEGRAM_BOT_TOKEN:
    missing_configs.append("TELEGRAM_BOT_TOKEN")
if USE_TELEGRAM and not TELEGRAM_CHAT_ID:
    missing_configs.append("TELEGRAM_CHAT_ID")

if missing_configs:
    logging.error(f"缺少必要配置: {', '.join(missing_configs)},请在环境变量或配置文件中设置。")
    exit(1)
    
class NotificationManager:
    def __init__(self, use_telegram, bot_token, chat_id):
        self.use_telegram = use_telegram
        self.bot_token = bot_token
        self.chat_id = chat_id
    
    def send_message(self, content, summary):
        logging.debug(f"Attempting to send Telegram message. USE_TELEGRAM: {self.use_telegram}, Bot Token: {'Set' if self.bot_token else 'Not Set'}, Chat ID: {self.chat_id}")
        if self.use_telegram:
            try:
                loop = asyncio.get_event_loop()
                messages = self.split_long_message(content)
                for i, message in enumerate(messages, 1):
                    if loop.is_running():
                        loop.create_task(self.async_send_message(message, f"{summary} (Part {i}/{len(messages)})"))
                    else:
                        loop.run_until_complete(self.async_send_message(message, f"{summary} (Part {i}/{len(messages)})"))
            except Exception as e:
                logging.error(f"发送Telegram消息时出错: {str(e)}", exc_info=True)
        else:
            logging.info("Telegram功能未启用，跳过发送消息。")
            
    async def async_send_message(self, content, summary):
        try:
            bot = telegram.Bot(self.bot_token)
            await bot.send_message(chat_id=self.chat_id, text=content, parse_mode='HTML')
            logging.info(f"Telegram消息发送成功: {summary}")
        except telegram.error.BadRequest as e:
            logging.error(f"发送Telegram消息时出错 (BadRequest): {str(e)}")
        except Exception as e:
            logging.error(f"发送Telegram消息时出错: {str(e)}")
        
    def split_long_message(self, content, max_length=4000):
        messages = []
        current_message = ""
        for line in content.split('\n'):
            if len(current_message) + len(line) + 1 > max_length:
                messages.append(current_message.strip())
                current_message = line + '\n'
            else:
                current_message += line + '\n'
        if current_message:
            messages.append(current_message.strip())
        return messages
    
class LinuxDoBrowser:
    def __init__(self) -> None:
        logging.info("启动 Playwright...")
        self.pw = sync_playwright().start()
        logging.info("以无头模式启动 Firefox...")
        self.browser = self.pw.firefox.launch(headless=True)
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        logging.info(f"导航到 {HOME_URL}...")
        self.page.goto(HOME_URL)
        logging.info("初始化完成。")
        self.notification_manager = NotificationManager(USE_TELEGRAM, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        self.browsed_count = 0
        self.like_count = 0
        self.browsed_articles = []
        self.liked_articles = []
        self.skip_articles = []
        self.skip_count = 0
        self.replied_articles = []
        self.reply_count = 0
        self.collected_articles = []
        self.collect_count = 0
    
    def load_messages(self, filename):
        """从指定的文件加载消息并返回消息列表。"""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(script_dir, filename)
        with open(file_path, 'r', encoding='utf-8') as file:
            messages = file.readlines()
        return [message.strip() for message in messages if message.strip()]

    def get_random_message(self, messages):
        """从列表中选择一个随机消息。"""
        return random.choice(messages)

    def login(self) -> bool:
        try:
            logging.info("尝试登录...")
            self.page.click(".login-button .d-button-label")
            time.sleep(2)
            self.page.fill("#login-account-name", USERNAME)
            time.sleep(2)
            self.page.fill("#login-account-password", PASSWORD)
            time.sleep(2)
            self.page.click("#login-button")
            time.sleep(10)  # 等待页面加载完成
            user_ele = self.page.query_selector("#current-user")
            if not user_ele:
                logging.error("登录失败")
                # 发送Telegram消息
                login_message = f"Linux.do 登录失败\n用户名: {USERNAME}\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                self.notification_manager.send_message(login_message, "Linux.do 登录通知")
                return False
            else:
                logging.info("登录成功")
                # 发送Telegram消息
                login_message = f"Linux.do 登录成功\n用户名: {USERNAME}\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                self.notification_manager.send_message(login_message, "Linux.do 登录通知")
                return True
        except TimeoutError:
            logging.error("登录失败：页面加载超时或元素未找到")
            return False

    def click_topic(self):
        try:
            logging.info("开始处理主题...")
            # 随机滚动页面
            self.visit_article_and_scroll(self.page)
            # 加载主题
            topics = self.page.query_selector_all("#list-area .title")
            total_topics = len(topics)
            logging.info(f"共找到 {total_topics} 个主题。")

            # 限制处理的最大主题数
            if total_topics > MAX_TOPICS:
                logging.info(f"处理主题数超过最大限制 {MAX_TOPICS}，仅处理前 {MAX_TOPICS} 个主题。")
                topics = topics[:MAX_TOPICS]

            for idx, topic in enumerate(topics):

                article_title = topic.text_content().strip()

                article_url = HOME_URL + topic.get_attribute("href")

                # 使用 Playwright 的方法来查找父元素
                parent_element = topic.evaluate_handle(
                    "(element) => element.closest('tr')"
                )

                # 使用 Playwright 的方法来查找元素
                is_pinned = parent_element.query_selector_all(".topic-statuses .pinned")

                if is_pinned:
                    self.skip_articles.append({"title": article_title, "url": article_url})
                    self.skip_count += 1
                    logging.info(f"跳过置顶的帖子：{article_title}")
                    continue

                logging.info(f"打开第 {idx + 1}/{len(topics)} 个主题 ：{article_title} ... ")
                
                page = self.context.new_page()
                
                try:
                    # 访问文章页面
                    page.goto(article_url)
                    # 访问文章数累加
                    self.browsed_count += 1
                    # 访问文章数信息记录
                    self.browsed_articles.append({"title": article_title, "url": article_url})
                    # 等待页面完全加载
                    time.sleep(3)  
                    # 随机滚动页面
                    self.visit_article_and_scroll(page)
                    if random.random() < LIKE_PROBABILITY:
                        self.click_like(page)
                        self.liked_articles.append({"title": article_title, "url": article_url})
                        self.like_count += 1
                    if random.random() < REPLY_PROBABILITY:
                        reply_message = self.click_reply(page)
                        if reply_message:
                            self.replied_articles.append(
                                {"title": article_title, "url": article_url, "reply": reply_message})
                            self.reply_count += 1
                    if random.random() < COLLECT_PROBABILITY:
                        self.click_collect(page)
                        self.collected_articles.append({"title": article_title, "url": article_url})
                        self.collect_count += 1

                except TimeoutError:
                    logging.warning(f"打开主题 ： {article_title} 超时，跳过该主题。")
                finally:
                    time.sleep(3)  # 等待一段时间，防止操作过快导致出错
                    page.close()
                    logging.info(f"已关闭第 {idx + 1}/{len(topics)} 个主题 ： {article_title} ...")

            # 打印跳过的文章信息
            logging.info(f"一共跳过了 {self.skip_count} 篇文章。")
            if self.skip_count > 0:
                logging.info("--------------跳过的文章信息-----------------")
                logging.info("\n%s",tabulate(self.skip_articles, headers="keys", tablefmt="pretty"))

            # 打印浏览的文章信息
            logging.info(f"一共浏览了 {self.browsed_count} 篇文章。")
            if self.browsed_count > 0:
                logging.info("--------------浏览的文章信息-----------------")
                logging.info("\n%s",tabulate(self.browsed_articles, headers="keys", tablefmt="pretty"))

            # 打印点赞的文章信息
            logging.info(f"一共点赞了 {self.like_count} 篇文章。")
            if self.like_count > 0:
                logging.info("--------------点赞的文章信息-----------------")
                logging.info("\n%s",tabulate(self.liked_articles, headers="keys", tablefmt="pretty"))

           # 打印回复的文章信息
            logging.info(f"一共回复了 {self.reply_count} 篇文章。")
            if self.reply_count > 0:
                logging.info("--------------回复的文章信息-----------------")
                logging.info("\n%s",tabulate(self.replied_articles, headers="keys", tablefmt="pretty"))

            # 打印加入书签的文章信息
            logging.info(f"一共加入书签了 {self.collect_count} 篇文章。")
            if self.collect_count > 0:
                logging.info("--------------加入书签的文章信息-----------------")
                logging.info("\n%s", tabulate(self.collected_articles, headers="keys", tablefmt="pretty"))

        except Exception as e:
            logging.error(f"处理主题时出错: {e}")

    def run(self):
        start_time = datetime.now()
        logging.info(f"开始执行时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        try:
            logging.info("开始运行自动化流程...")
            if not self.login():
                return
            self.click_topic()
            self.print_connect_info()
            self.logout()
        except Exception as e:
            logging.error(f"运行过程中出错: {e}")
            login_message = f"Linux.do 运行过程中出错\n用户名: {USERNAME}\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n错误: {e}"
            self.notification_manager.send_message(login_message, "Linux.do 运行过程中出错")
        finally:
            end_time = datetime.now()
            logging.info(f"结束执行时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
            self.context.close()
            self.browser.close()
            self.pw.stop()

            try:
                if USE_TELEGRAM:
                    elapsed_time = end_time - start_time
                    summary = f"Linux.do保活脚本 {end_time.strftime('%Y-%m-%d %H:%M:%S')}"
                    
                    # 获取并转义日志内容
                    # 创建浏览和点赞信息的字符串
                    browsed_info = f"一共浏览了 {self.browsed_count} 篇文章。\n"
                    if self.browsed_count > 0:
                        browsed_info += "--------------浏览的文章信息-----------------\n"
                        browsed_info += tabulate(self.browsed_articles, headers="keys", tablefmt="pretty")
                    
                    liked_info = f"\n\n一共点赞了 {self.like_count} 篇文章。\n"
                    if self.like_count > 0:
                        liked_info += "--------------点赞的文章信息-----------------\n"
                        liked_info += tabulate(self.liked_articles, headers="keys", tablefmt="pretty")
                    
                    # 合并信息
                    article_info = browsed_info + liked_info
                    # 创建 HTML 格式的内容
                    content = (
                        f"<b>Linux.do保活脚本 {end_time.strftime('%Y-%m-%d %H:%M:%S')}</b>\n\n"
                        f"<b>账号:</b> {USERNAME}\n"
                        f"<b>开始执行时间:</b> {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"<b>结束执行时间:</b> {end_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"<b>总耗时:</b> {elapsed_time}\n\n"
                        f"<b>文章信息:</b>\n<pre>{html.escape(article_info)}</pre>"
                    )
                    
                    self.notification_manager.send_message(content, summary)
            except Exception as e:
                logging.error(f"发送Telegram最终报告时出错: {str(e)}", exc_info=True)

    def print_connect_info(self):
        try:
            logging.info(f"导航到 {CONNECT_URL}...")
            self.page.goto(CONNECT_URL)
            time.sleep(2)
            logging.info(f"当前页面URL: {self.page.url}")
            time.sleep(2)
            rows = self.page.query_selector_all("table tr")
            info = []
            for row in rows:
                cells = row.query_selector_all("td")
                if len(cells) >= 3:
                    project = cells[0].text_content().strip()
                    current = cells[1].text_content().strip()
                    requirement = cells[2].text_content().strip()
                    info.append([project, current, requirement])

            logging.info("--------------Connect Info 在过去 💯 天内-----------------")
            logging.info("\n%s", tabulate(info, headers=["项目", "当前", "要求"], tablefmt="pretty"))
        except TimeoutError:
            logging.error("连接信息页面加载超时")
        except Exception as e:
            logging.error(f"打印连接信息时出错: {e}")

    def click_like(self, page):
        try:
            page.wait_for_selector(".discourse-reactions-reaction-button button", timeout=2000)
            like_button = page.locator(".discourse-reactions-reaction-button").first
            if like_button:
                like_button.click()
                logging.info("文章已点赞")
            else:
                logging.info("未找到点赞按钮")
        except TimeoutError:
            logging.warning("点赞按钮定位超时")
        except Exception as e:
            logging.error(f"点赞操作失败: {e}")

    def click_reply(self, page):
        try:
            # 加载消息
            random_message = reply_generator.get_random_reply()

            # 选择一条随机消息
            page.wait_for_selector(".reply.create.btn-icon-text", timeout=2000)
            reply_button = page.locator(".reply.create.btn-icon-text").first
            if reply_button:
                reply_button.click()
                logging.info("回复按钮已点击")

                # 等待文本区域可见
                page.wait_for_selector(".d-editor-input", timeout=2000)
                text_area = page.locator(".d-editor-input").first
                if text_area:
                    # 在文本区域中键入随机消息
                    text_area.fill(random_message)
                    logging.info(f"回复内容: {random_message}")

                    # 点击提交按钮
                    page.wait_for_selector(".save-or-cancel .btn-primary.create", timeout=2000)
                    submit_button = page.locator(".save-or-cancel .btn-primary.create").first
                    if submit_button:
                        time.sleep(2)
                        submit_button.click()
                        logging.info("回复已提交")
                        return random_message  # 返回实际的回复内容
                    else:
                        logging.warning("未找到提交按钮")
                else:
                    logging.warning("未找到回复文本框")
            else:
                logging.info("未找到回复按钮")
            return None  # 如果回复失败，返回 None

        except TimeoutError:
            logging.warning("元素定位超时")
            return None
        except Exception as e:
            logging.error(f"回复操作失败: {e}")
            return None

    def click_collect(self, page):
        try:
            # 等待并点击书签按钮
            page.wait_for_selector(".btn.bookmark-menu-trigger", timeout=2000)  # 增加等待时间
            bookmark_button = page.locator(".btn.bookmark-menu-trigger").first
            if bookmark_button:
                # 等待几秒钟以确保加入书签操作已完成
                time.sleep(2)
                bookmark_button.click()
                logging.info("帖子已加入书签")
            else:
                logging.warning("未找到书签按钮")

        except TimeoutError:
            logging.warning("书签按钮定位超时")
        except Exception as e:
            logging.error(f"加入书签操作失败: {e}")

    def visit_article_and_scroll(self, page):
        try:
            # 随机滚动页面5到10秒
            scroll_duration = random.randint(5, 10)
            logging.info(f"随机滚动页面 {scroll_duration} 秒...")
            scroll_end_time = time.time() + scroll_duration

            while time.time() < scroll_end_time:
                scroll_distance = random.randint(300, 600)  # 每次滚动的距离，随机选择
                page.mouse.wheel(0, scroll_distance)
                time.sleep(random.uniform(0.5, 1.5))  # 随机等待0.5到1.5秒再滚动

            logging.info("页面滚动完成")

        except Exception as e:
            logging.error(f"滚动页面时出错: {e}")

    def logout(self):
        try:
            logging.info(f"导航到 {HOME_URL}...")
            self.page.goto(HOME_URL)
            time.sleep(2)

            # 点击用户菜单按钮以显示下拉菜单
            logging.info("尝试找到并点击用户菜单按钮...")
            self.page.wait_for_selector("#current-user .icon", timeout=2000)
            user_menu_button = self.page.locator("#current-user .icon").first
            if user_menu_button:
                user_menu_button.click()
                logging.info("成功点击用户菜单按钮")
            else:
                logging.info("未找到用户菜单按钮")
                return

            time.sleep(2)  # 确保菜单展开

            # 点击“个人资料”标签
            logging.info("尝试找到并点击个人资料标签...")
            self.page.wait_for_selector("#user-menu-button-profile", timeout=2000)
            profile_tab_button = self.page.locator("#user-menu-button-profile").first
            if profile_tab_button:
                profile_tab_button.click()
                logging.info("成功点击个人资料标签")
            else:
                logging.info("未找到个人资料标签")
                return

            time.sleep(2)  # 确保页面加载个人资料内容

            # 定位并点击退出按钮
            logging.info("尝试找到并点击退出按钮...")
            self.page.wait_for_selector(".logout .btn", timeout=2000)
            logout_button = self.page.locator(".logout .btn").first
            if logout_button:
                logout_button.click()
                logging.info("成功点击退出按钮")
            else:
                logging.info("未找到退出按钮")

        except TimeoutError:
            logging.warning("定位按钮超时")
        except Exception as e:
            logging.error(f"操作失败: {e}")

if __name__ == "__main__":
    ldb = LinuxDoBrowser()
    ldb.run()
