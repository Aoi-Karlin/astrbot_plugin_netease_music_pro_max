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

# --- Constants ---
# 含义：忽略大小写，不以指令前缀开头，匹配点歌关键词
REGEX_PATTERN = r"(?i)^(?![\/!\?\.。])(来.?一首|播放|听.?听|点歌|唱.?一首|来.?首)\s*([^\s].+?)(的歌|的歌曲|的音乐|歌|曲)?$"


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
            return data["songs"][0] if data.get("songs") else None

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
                # 修改：先检查列表是否为空
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

        # 添加警告
        if self.config["api_url"] == "http://127.0.0.1:3000":
            logger.warning("Netease Music plugin: 使用默认 API URL (127.0.0.1:3000)，"
                           "如果您的 API 服务在其他地址，请在配置中修改 api_url")

        self.config.setdefault("music_u", "")
        self.config.setdefault("csrf_token", "")
        self.config.setdefault("music_r_u", "")
        # -------------------------------------------

        self.waiting_users: Dict[str, Dict[str, Any]] = {}
        self.song_cache: Dict[str, List[Dict[str, Any]]] = {}

        # 占位符
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.api: Optional[NeteaseMusicAPI] = None
        self.cleanup_task: Optional[asyncio.Task] = None

    # --- Lifecycle Hooks ---

    async def initialize(self):
        """Starts the background cleanup task and initializes session when the plugin is activated."""

        # --- 修改点：拼接 Cookie 字符串 ---
        # 自动加上键名和分号，用户只需提供值
        music_u = self.config.get("music_u", "").strip()
        csrf = self.config.get("csrf_token", "").strip()
        music_r_u = self.config.get("music_r_u", "").strip()

        # 构造完整的 Cookie 字符串
        full_cookie = f"MUSIC_U={music_u}; __csrf={csrf}; MUSIC_R_U={music_r_u};"
        # -------------------------------

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

        # 添加：调用父类的 terminate 方法
        await super().terminate()

    async def _periodic_cleanup(self):
        """
        A background task that runs periodically to clean up expired sessions.
        """
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                expired_sessions = []

                for session_id, user_session in self.waiting_users.items():
                    if user_session['expire'] < now:
                        expired_sessions.append((session_id, user_session['key']))

                if expired_sessions:
                    logger.info(f"Netease Music plugin: Cleaning up {len(expired_sessions)} expired session(s).")
                    for session_id, cache_key in expired_sessions:
                        if session_id in self.waiting_users:
                            del self.waiting_users[session_id]
                        if cache_key not in self.song_cache:
                            continue
                        del self.song_cache[cache_key]

            except Exception as e:
                logger.error(f"Netease Music plugin: Cleanup task error: {e!s}")
                # 继续运行，不让单次错误导致清理任务停止

    # --- Event Handlers ---

    @filter.command("点歌", alias={"music", "听歌", "网易云"}, priority=100)
    async def cmd_handler(self, event: AstrMessageEvent, keyword: str = ""):
        """Handles the '/点歌' command."""
        event.stop_event()

        if not keyword.strip():
            await event.send(MessageChain([Plain("请告诉天依您想听什么歌 例如：/点歌 Lemon")]))
            return
        await self.search_and_show(event, keyword.strip())

    # use REGEX_PATTERN instead
    @filter.regex(REGEX_PATTERN)
    async def natural_language_handler(self, event: AstrMessageEvent):
        """Handles song requests in natural language."""
        # FIXED as DRY
        match = re.search(REGEX_PATTERN, event.message_str)
        if match:
            keyword = match.group(2).strip()
            if keyword:
                await self.search_and_show(event, keyword)

    @filter.regex(r"^\d+$", priority=999)
    async def number_selection_handler(self, event: AstrMessageEvent):
        """Handles user's numeric choice from the search results."""
        user_key = f"{event.get_session_id()}_{event.get_sender_id()}"
        if user_key not in self.waiting_users:
            return
        user_session = self.waiting_users[user_key]

        user_session = self.waiting_users[session_id]
        if time.time() > user_session["expire"]:
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
            return

        # Major fix: use len(songs) but not limit
        if not (1 <= num <= len(songs)):
            return

        event.stop_event()
        await self.play_selected_song(event, cache_key, num)

        # only remove waiting when used play_selected_songs.
        self.waiting_users.pop(user_key, None)

    # --- Core Logic ---

    async def search_and_show(self, event: AstrMessageEvent, keyword: str):
        """Searches for songs and displays the results to the user."""
        if not self.api:
            await event.send(MessageChain([Plain("插件未正确初始化 QAQ")]))
            return

        try:
            songs = await self.api.search_songs(keyword, self.config["search_limit"])
        except Exception as e:
            logger.error(f"Netease Music plugin: API search failed. Error: {e!s}")
            await event.send(MessageChain([Plain(f"API爆了...QAQ")]))
            return

        if not songs:
            await event.send(MessageChain([Plain(f"对不起...天依不记得有「{keyword}」这首歌... T_T")]))
            return

        cache_key = f"{event.get_session_id()}_{int(time.time())}"
        self.song_cache[cache_key] = songs

        response_lines = [f"天依找到了 {len(songs)} 首歌哦，想听哪个？"]
        for i, song in enumerate(songs, 1):
            artists = " / ".join(a["name"] for a in song.get("artists", []))
            album = song.get("album", {}).get("name", "未知专辑")
            duration_ms = song.get("duration", 0)
            dur_str = f"{duration_ms // 60000}:{(duration_ms % 60000) // 1000:02d}"
            response_lines.append(f"{i}. {song['name']} - {artists} 《{album}》 [{dur_str}]")

        await event.send(MessageChain([Plain("\n".join(response_lines))]))

        user_key = f"{event.get_session_id()}_{event.get_sender_id()}"
        self.waiting_users[user_key] = {"key": cache_key, "expire": time.time() + 60}

    async def play_selected_song(self, event: AstrMessageEvent, cache_key: str, num: int):
        """Plays the song selected by the user."""
        songs = self.song_cache.get(cache_key)

        if not songs:
            await event.send(MessageChain([Plain("搜索结果已经凉掉了哦，请重新点歌吧~")]))
            return

        # Re-check
        if not (1 <= num <= len(songs)):
            await event.send(MessageChain([Plain(f"你在选什么呀..选曲名前面的数字（1-{len(songs)}）就好了呢...")]))
            # use return to avoid mistakes
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
                await event.send(MessageChain([Plain(f"天依不太能唱这首歌呢...（版权/VIP原因）")]))
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
            await event.send(MessageChain([Plain(f"咳咳，额...天依有点忘了怎么唱这首歌了...")]))

        # Removed 'finally' to avoid cache cleared too fast.

    async def _send_song_messages(self, event: AstrMessageEvent, num: int, title: str, artists: str, album: str,
                                  dur_str: str, cover_url: str, audio_url: str):
        """Constructs and sends the song info and audio messages."""
        detail_text = f"""好的！请欣赏天依唱的第 {num} 首歌曲！

♪ 歌名：{title}
🎤 歌手：{artists}
💿 专辑：{album}
⏳ 时长：{dur_str}
✨ 音质：{self.config['quality']}

请听~ ♪~
"""
        info_components = [Plain(detail_text)]

        # add None check
        if self.api:
            image_data = await self.api.download_image(cover_url)
            if image_data:
                info_components.append(Image.fromBase64(base64.b64encode(image_data).decode()))

        await event.send(MessageChain(info_components))
        await event.send(MessageChain([Record(file=audio_url)]))