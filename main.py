"""
Netease Music Enhanced Plugin for AstrBot
- Author: Azured
- Repo: https://github.com/Aoi-Karlin/astrbot_plugin_netease_music_pro_max
- Features: Interactive song selection, cover display, audio playback, and auto quality fallback.
"""

import re
import time
import base64
import aiohttp
import asyncio
import urllib.parse
from typing import Dict, Any, Optional, List

from astrbot.api import star, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Plain, Image, Record

# --- API Wrapper ---
class NeteaseMusicAPI:
    """
    A wrapper for the NeteaseCloudMusicApi to simplify interactions.
    Encapsulates API calls for searching, getting details, and fetching audio URLs.
    """

    def __init__(self, api_url: str, session: aiohttp.ClientSession, cookie: str = ""):
        self.base_url = api_url.rstrip("/")
        self.session = session
        self.cookie = cookie

    async def search_songs(self, keyword: str, limit: int) -> List[Dict[str, Any]]:
        """Search for songs by keyword."""
        url = f"{self.base_url}/search?keywords={urllib.parse.quote(keyword)}&limit={limit}&type=1"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            return data.get("result", {}).get("songs", [])

    async def get_song_details(self, song_id: int) -> Optional[Dict[str, Any]]:
        """Get detailed information for a single song."""
        url = f"{self.base_url}/song/detail?ids={str(song_id)}"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            songs = data.get("songs", [])
            return songs[0] if songs else None  # 安全检查，避免 IndexError

    async def get_audio_url(self, song_id: int, quality: str) -> Optional[str]:
        """
        Get the audio stream URL for a song with automatic quality fallback.
        """
        qualities_to_try = list(dict.fromkeys([quality, "exhigh", "higher", "standard"]))
        for q in qualities_to_try:
            encoded_cookie = urllib.parse.quote(self.cookie)
            url = f"{self.base_url}/song/url/v1?id={str(song_id)}&level={q}&cookie={encoded_cookie}"

            async with self.session.get(url) as r:
                r.raise_for_status()
                data = await r.json()
                # 修复：先检查列表是否为空，避免 IndexError
                data_list = data.get("data", [])
                if data_list:  # 确保列表不为空
                    audio_info = data_list[0]
                    if audio_info.get("url"):
                        return audio_info["url"]
        return None

    async def download_image(self, url: str) -> Optional[bytes]:
        """Download image data from a URL."""
        if not url:
            return None
        async with self.session.get(url) as r:
            if r.status == 200:
                return await r.read()
        return None


# --- Main Plugin Class ---
class Main(star.Star):
    """
    I changed the original one to Luo Tianyi (A Chinese VOCALOID Singer).
    """

    def __init__(self, context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config = config or {}
        self.config.setdefault("api_url", "http://127.0.0.1:3000")
        self.config.setdefault("quality", "exhigh")
        self.config.setdefault("search_limit", 5)

        # 修复：添加警告提示默认配置
        if self.config["api_url"] == "http://127.0.0.1:3000":
            logger.warning("Netease Music plugin: 使用默认 API URL (127.0.0.1:3000)，"
                           "如果您的 API 服务在其他地址，请在配置中修改 api_url")

        # Cookie 配置字段
        self.config.setdefault("music_u", "")
        self.config.setdefault("csrf_token", "")
        self.config.setdefault("music_r_u", "")

        # 正则触发词配置
        self.config.setdefault("regex_triggers", ["来一首", "播放", "听听", "点歌", "唱一首", "来首"])
        self.config.setdefault("command_prefixes", ["/", "!", "?", ".", "。"])
        self.config.setdefault("command_aliases", ["music", "听歌", "网易云"])

        # UX提示词配置
        self.config.setdefault("msg_no_keyword", "请告诉天依您想听什么歌 例如：/点歌 Lemon")
        self.config.setdefault("msg_searching", "")
        self.config.setdefault("msg_api_error", "API爆了...QAQ")
        self.config.setdefault("msg_no_results", "对不起...天依不记得有「{keyword}」这首歌... T_T")
        self.config.setdefault("msg_search_results", "天依找到了 {count} 首歌哦，想听哪个？")
        self.config.setdefault("msg_song_detail", "好的！请欣赏天依唱的第 {num} 首歌曲！\n\n♪ 歌名：{title}\n🎤 歌手：{artists}\n💿 专辑：{album}\n⏳ 时长：{duration}\n✨ 音质：{quality}\n\n请听~ ♪~")
        self.config.setdefault("msg_no_audio_url", "天依不太能唱这首歌呢...（版权/VIP原因）")
        self.config.setdefault("msg_play_error", "咳咳，额...天依有点忘了怎么唱这首歌了...")
        self.config.setdefault("msg_cache_expired", "搜索结果已经凉掉了哦，请重新点歌吧~")
        self.config.setdefault("msg_invalid_selection", "你在选什么呀..选曲名前面的数字（1-{max}）就好了呢...")
        self.config.setdefault("msg_init_error", "插件未正确初始化 QAQ，请联系管理员检查配置")

        # 构建动态正则表达式
        self.regex_pattern = self._build_regex_pattern()
        self.regex_compiled = re.compile(self.regex_pattern, re.IGNORECASE)

        self.waiting_users: Dict[str, Dict[str, Any]] = {}
        self.song_cache: Dict[str, List[Dict[str, Any]]] = {}

        # 占位符
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.api: Optional[NeteaseMusicAPI] = None
        self.cleanup_task: Optional[asyncio.Task] = None

    def _build_regex_pattern(self) -> str:
        """根据配置的触发词和指令前缀构建正则表达式"""
        triggers = self.config.get("regex_triggers", [])
        prefixes = self.config.get("command_prefixes", ["/", "!", "?", ".", "。"])

        if not triggers:
            # 如果没有配置触发词，返回一个匹配不到任何内容的正则
            return r"^$"

        # 对触发词进行正则转义
        escaped_triggers = [re.escape(trigger) for trigger in triggers]
        triggers_part = "|".join(escaped_triggers)

        # 对指令前缀进行正则转义（用于负向前瞻断言）
        # 构建字符集，例如：[\/!\?\.]
        escaped_prefixes = [re.escape(p) for p in prefixes]
        prefixes_part = "".join(escaped_prefixes)

        # 构建完整的正则表达式
        # (?![...]) 是负向前瞻断言，确保不匹配以指令前缀开头的消息
        # 触发词必须在句首（在负向前瞻断言之后）
        # 匹配：非指令前缀开头 + 触发词 + 任意内容 + 可选的结尾词
        if prefixes_part:
            pattern = rf"^(?![{prefixes_part}])(?:{triggers_part})\s*(.+?)(?:的歌|的歌曲|的音乐|歌|曲)?$"
        else:
            # 如果没有配置前缀，则不使用负向前瞻断言
            pattern = rf"^(?:{triggers_part})\s*(.+?)(?:的歌|的歌曲|的音乐|歌|曲)?$"

        return pattern

    # --- Lifecycle Hooks ---

    async def initialize(self):
        """Starts the background cleanup task and initializes session when the plugin is activated."""

        # 拼接 Cookie 字符串
        music_u = self.config.get("music_u", "").strip()
        csrf = self.config.get("csrf_token", "").strip()
        music_r_u = self.config.get("music_r_u", "").strip()

        # 构造完整的 Cookie 字符串
        full_cookie = f"MUSIC_U={music_u}; __csrf={csrf}; MUSIC_R_U={music_r_u};"

        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

        # 将拼接好的 full_cookie 传给 API
        self.api = NeteaseMusicAPI(self.config["api_url"], self.http_session, full_cookie)

        self.cleanup_task = asyncio.create_task(self._periodic_cleanup())
        logger.info("Netease Music plugin: Initialized successfully.")

    async def terminate(self):
        """Cleans up resources when the plugin is unloaded."""
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass

        # close session safely
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
            logger.info("Netease Music plugin: HTTP session closed.")

        # 修复：调用父类的 terminate 方法
        await super().terminate()

    def _get_user_key(self, event: AstrMessageEvent) -> str:
        """
        生成用户唯一标识，结合会话和发送者ID
        确保不同用户的搜索会话互不干扰
        """
        session_id = event.get_session_id()
        sender_id = event.get_sender_id()  # 使用官方方法，符合 Law of Demeter
        return f"{session_id}_{sender_id}"

    async def _periodic_cleanup(self):
        """
        A background task that runs periodically to clean up expired sessions.
        """
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                expired_sessions = []

                # 修复：使用 user_key 进行清理
                for user_key, user_session in self.waiting_users.items():
                    if user_session['expire'] < now:
                        expired_sessions.append((user_key, user_session['key']))

                if expired_sessions:
                    logger.info(f"Netease Music plugin: Cleaning up {len(expired_sessions)} expired session(s).")
                    for user_key, cache_key in expired_sessions:
                        if user_key in self.waiting_users:
                            del self.waiting_users[user_key]
                        if cache_key in self.song_cache:
                            del self.song_cache[cache_key]

            except Exception as e:
                logger.error(f"Netease Music plugin: Cleanup task error: {e!s}")
                # 继续运行，不让单次错误导致清理任务停止

    # --- Event Handlers ---

    @filter.command("点歌", alias=None, priority=100)
    async def cmd_handler(self, event: AstrMessageEvent):
        """Handles the '/点歌' command."""
        event.stop_event()

        # 从完整消息中提取关键词（去掉指令前缀）
        message_str = event.message_str.strip()

        # 移除指令前缀（/点歌 或 /music 等）
        # 获取所有可能的指令前缀
        command_names = ["点歌"]
        command_aliases = self.config.get("command_aliases", [])
        command_names.extend(command_aliases)

        keyword = message_str
        for cmd in command_names:
            # 匹配 /cmd 或 /cmd@bot 格式
            pattern = rf"^/\s*{re.escape(cmd)}(?:@\S+)?\s*(.*)$"
            match = re.match(pattern, message_str, re.IGNORECASE)
            if match:
                keyword = match.group(1).strip()
                break

        if not keyword:
            await event.send(MessageChain([Plain(self.config["msg_no_keyword"])]))
            return

        await self.search_and_show(event, keyword)

    @filter.regex(".*")
    async def natural_language_handler(self, event: AstrMessageEvent):
        """Handles song requests in natural language."""
        # 使用动态构建的正则表达式
        match = self.regex_compiled.match(event.message_str)
        if match:
            keyword = match.group(1).strip()
            if keyword:
                event.stop_event()  # 停止事件传播，避免触发 LLM
                await self.search_and_show(event, keyword)

    @filter.regex(r"^\d+$", priority=999)
    async def number_selection_handler(self, event: AstrMessageEvent):
        """Handles user's numeric choice from the search results."""
        # 修复：使用用户唯一Key，解决会话隔离问题
        user_key = self._get_user_key(event)

        if user_key not in self.waiting_users:
            return

        user_session = self.waiting_users[user_key]
        if time.time() > user_session["expire"]:
            # 缓存过期，发送提示消息
            await event.send(MessageChain([Plain(self.config["msg_cache_expired"])]))
            if user_key in self.waiting_users:
                del self.waiting_users[user_key]
            cache_key = user_session.get("key")
            if cache_key and cache_key in self.song_cache:
                del self.song_cache[cache_key]
            return

        try:
            num = int(event.message_str.strip())
        except ValueError:
            return

        # Obtain the actual length of the cached song list and perform precise boundary checks.
        cache_key = user_session["key"]
        songs = self.song_cache.get(cache_key)

        # Cache lost: no response and return
        if not songs:
            await event.send(MessageChain([Plain(self.config["msg_cache_expired"])]))
            if user_key in self.waiting_users:
                del self.waiting_users[user_key]
            return

        # Major fix: use len(songs) but not limit
        if not (1 <= num <= len(songs)):
            invalid_msg = self.config["msg_invalid_selection"].format(max=len(songs))
            await event.send(MessageChain([Plain(invalid_msg)]))
            return

        event.stop_event()
        await self.play_selected_song(event, cache_key, num)

        # only remove waiting when used play_selected_songs.
        if user_key in self.waiting_users:
            del self.waiting_users[user_key]

    # --- Core Logic ---

    async def search_and_show(self, event: AstrMessageEvent, keyword: str):
        """Searches for songs and displays the results to the user."""
        if not self.api:
            await event.send(MessageChain([Plain(self.config["msg_init_error"])]))
            logger.error("Netease Music plugin: API not initialized. Check if initialize() was called.")
            return

        # 发送搜索中提示（如果配置不为空）
        if self.config["msg_searching"]:
            await event.send(MessageChain([Plain(self.config["msg_searching"])]))

        try:
            songs = await self.api.search_songs(keyword, self.config["search_limit"])
        except Exception as e:
            logger.error(f"Netease Music plugin: API search failed. Error: {e!s}")
            await event.send(MessageChain([Plain(self.config["msg_api_error"])]))
            return

        if not songs:
            no_results_msg = self.config["msg_no_results"].format(keyword=keyword)
            await event.send(MessageChain([Plain(no_results_msg)]))
            return

        user_key = self._get_user_key(event)

        # 清理该用户的旧缓存，避免内存泄漏
        if user_key in self.waiting_users:
            old_cache_key = self.waiting_users[user_key].get("key")
            if old_cache_key and old_cache_key in self.song_cache:
                del self.song_cache[old_cache_key]

        cache_key = f"{user_key}_{int(time.time())}"
        self.song_cache[cache_key] = songs

        # 使用可配置的搜索结果标题
        results_title = self.config["msg_search_results"].format(count=len(songs))
        response_lines = [results_title]
        for i, song in enumerate(songs, 1):
            artists = " / ".join(a["name"] for a in song.get("artists", []))
            album = song.get("album", {}).get("name", "未知专辑")
            duration_ms = song.get("duration", 0)
            dur_str = f"{duration_ms // 60000}:{(duration_ms % 60000) // 1000:02d}"
            response_lines.append(f"{i}. {song['name']} - {artists} 《{album}》 [{dur_str}]")

        await event.send(MessageChain([Plain("\n".join(response_lines))]))

        self.waiting_users[user_key] = {"key": cache_key, "expire": time.time() + 60}

    async def play_selected_song(self, event: AstrMessageEvent, cache_key: str, num: int):
        """Plays the song selected by the user."""
        songs = self.song_cache.get(cache_key)

        if not songs:
            await event.send(MessageChain([Plain(self.config["msg_cache_expired"])]))
            return

        # Re-check
        if not (1 <= num <= len(songs)):
            invalid_msg = self.config["msg_invalid_selection"].format(max=len(songs))
            await event.send(MessageChain([Plain(invalid_msg)]))
            return

        # Confirm song
        selected_song = songs[num - 1]
        song_id = selected_song["id"]

        if cache_key in self.song_cache:
            del self.song_cache[cache_key]

        try:
            song_details = await self.api.get_song_details(song_id)
            if not song_details:
                raise ValueError("无法获取歌曲详细信息。")

            audio_url = await self.api.get_audio_url(song_id, self.config["quality"])
            if not audio_url:
                await event.send(MessageChain([Plain(self.config["msg_no_audio_url"])]))
                return

            title = song_details.get("name", "")
            artists = " / ".join(a["name"] for a in song_details.get("ar", []))
            album = song_details.get("al", {}).get("name", "未知专辑")
            cover_url = song_details.get("al", {}).get("picUrl", "")
            duration_ms = song_details.get("dt", 0)
            dur_str = f"{duration_ms // 60000}:{(duration_ms % 60000) // 1000:02d}"

            await self._send_song_messages(event, num, title, artists, album, dur_str, cover_url, audio_url)

        except Exception as e:
            logger.error(f"Netease Music plugin: Failed to play song {song_id}. Error: {e!s}")
            await event.send(MessageChain([Plain(self.config["msg_play_error"])]))

    async def _send_song_messages(self, event: AstrMessageEvent, num: int, title: str, artists: str, album: str,
                                  dur_str: str, cover_url: str, audio_url: str):
        """Constructs and sends the song info and audio messages."""
        # 使用可配置的歌曲详情模板
        detail_text = self.config["msg_song_detail"].format(
            num=num,
            title=title,
            artists=artists,
            album=album,
            duration=dur_str,
            quality=self.config['quality']
        )
        info_components = [Plain(detail_text)]

        # add None check
        if self.api:
            image_data = await self.api.download_image(cover_url)
            if image_data:
                info_components.append(Image.fromBase64(base64.b64encode(image_data).decode()))

        await event.send(MessageChain(info_components))
        await event.send(MessageChain([Record(file=audio_url)]))
