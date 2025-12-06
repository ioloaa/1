import re
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import os
import subprocess
subprocess.run(["python", "-m", "pip", "install", "-U", "yt-dlp"], stdout=subprocess.DEVNULL)
import random
from typing import Optional
import time  # для кеша/таймингов

# ---------- Настройки ----------
FFMPEG_PATH = r"C:\Users\38097\OneDrive\Рабочий стол\DiscordBot\Butler N(Музыкальный бот)\ffmpeg-8.0-full_build\bin\ffmpeg.exe"

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin',
    'options': '-vn -bufsize 64k',
    'executable': FFMPEG_PATH
}


YTDL_FORMAT_OPTIONS = {
    'format': 'bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'ignoreerrors': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0'
}

# ---------- Настройки сервера ----------
GUILD_ID = 1428351430246400172 # подставь свой ID если нужно

# ---------- Интенты ----------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

ytdl = yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS)

# ---------- Глобальные структуры ----------
guild_players: dict[int, 'GuildMusic'] = {}

def get_player(guild_id: int) -> 'GuildMusic':
    if guild_id not in guild_players:
        guild_players[guild_id] = GuildMusic()
    return guild_players[guild_id]

# ---------- КЕШ ----------
def normalize_url(u: str) -> str:
    return u.replace("youtu.be/", "www.youtube.com/watch?v=").split("&")[0]

track_cache: dict[str, dict] = {}

# ---------- RP-фразы ----------
N_PHRASES_RU = {
    "add": [
        "Уу… я добавил **{track}** в очередь.",
        "Э-э… кажется, **{track}** теперь играет.",
        "Ой, я немного нервничаю… но трек **{track}** в плейлисте!",
        "Я стараюсь… вот, **{track}** готов к проигрыванию.",
    ],
    "pause": [
        "Пауза… мне немного скучно, но ладно.",
        "Э-э… остановил трек. Я пытался сделать хорошо.",
    ],
    "resume": [
        "Уу… снова запускаю. Музыка продолжает играть!",
        "▶️ Кажется, всё снова работает.",
    ],
    "stop": [
        "Уу… всё замолчало.",
        "Я остановил **{track}**… странно без музыки.",
    ],
    "skip": [
        "⏭ Пропускаем этот трек.",
        "Уу… перелистываю. Посмотрим, что дальше.",
    ],
    "loop_on": [
        "🔁 Теперь трек **{track}** будет повторяться снова и снова.",
    ],
    "loop_off": [
        "⏹ Повтор отключен.",
    ],
    "error": [
        "Ой… кажется, что-то пошло не так.",
        "Уу… я пытался, честно, но трек **{track}** не играет.",
    ],
}

def make_n_reply_ru(track_name: str, event: str) -> str:
    phrases = N_PHRASES_RU.get(event, [])
    if not phrases:
        return ""
    template = random.choice(phrases)
    return template.format(track=track_name)

# ---------- Трек ---------- 
class Track:
    def __init__(self, title: str, source: str, requested_by=None, duration: Optional[int] = None, filepath: Optional[str] = None):
        self.title = title or "Неизвестный трек"
        self.source = source                  # ссылка или путь
        self.requested_by = requested_by      # кто заказал
        self.duration = duration              # длительность в секундах
        self.filepath = filepath              # если локальный файл
        self.stream_url: Optional[str] = None # прямой стрим-URL (yt-dlp заполнит)


# ---------- Музыкальные классы ----------
class GuildMusic:
    def __init__(self):
        self.queue: asyncio.Queue[Track] = asyncio.Queue()
        self.current: Optional[Track] = None
        self.loop_task: Optional[asyncio.Task] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self.play_next = asyncio.Event()
        self.loop_enabled = False
        self.last_channel: Optional[discord.TextChannel] = None
        self.connected: asyncio.Event = asyncio.Event()

    async def player_loop(self, guild_id: int):
        if not self.connected.is_set():
            try:
                await asyncio.wait_for(self.connected.wait(), timeout=20)
            except asyncio.TimeoutError:
                print(f"[player_loop] timeout waiting for voice connection on guild {guild_id}")
                if self.last_channel:
                    try:
                        await self.last_channel.send("❌ Не удалось подключиться к голосовому каналу.")
                    except:
                        pass
                return

        def extract_info_safe(url: str) -> Optional[dict]:
            opts = YTDL_FORMAT_OPTIONS.copy()
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if not info:
                        return None
                    if "entries" in info and info["entries"]:
                        info = info["entries"][0]
                    return info
            except Exception:
                try:
                    fallback = opts.copy()
                    fallback.pop("format", None)
                    with yt_dlp.YoutubeDL(fallback) as ydl_fb:
                        info = ydl_fb.extract_info(url, download=False)
                        if "entries" in info and info["entries"]:
                            info = info["entries"][0]
                        return info
                except Exception:
                    return None

        while True:
            self.play_next.clear()
            track: Track = await self.queue.get()
            self.current = track

            try:
                # ✅ Локальный файл (playfile)
                if track.filepath:
                    audio_source = discord.FFmpegPCMAudio(
                        track.filepath,
                        executable=FFMPEG_PATH,
                        before_options="-nostdin",
                        options="-vn"
                    )
                else:
                    # ✅ YouTube stream
                    stream_url = None
                    cache_entry = track_cache.get(track.source)

                    if cache_entry and cache_entry.get("stream_url"):
                        stream_url = cache_entry["stream_url"]
                        if cache_entry.get("title"):
                            track.title = cache_entry["title"]
                        if cache_entry.get("duration") and not track.duration:
                            track.duration = cache_entry["duration"]
                    else:
                        info = await asyncio.to_thread(extract_info_safe, track.source)
                        if not info:
                            raise RuntimeError("yt-dlp не вернул info")

                        if "url" in info and info["url"]:
                            stream_url = info["url"]
                        elif "formats" in info:
                            for f in reversed(info["formats"]):
                                if f.get("acodec") and f.get("acodec") != "none":
                                    stream_url = f.get("url")
                                    break

                        if not stream_url:
                            raise RuntimeError("Не найден stream_url")

                        track_cache[track.source] = {
                            "title": info.get("title"),
                            "duration": info.get("duration"),
                            "stream_url": stream_url,
                            "fetched_at": time.time()
                        }

                        track.title = info.get("title") or track.title
                        track.duration = info.get("duration") or track.duration
                        track.stream_url = stream_url

                    final_url = track.stream_url or stream_url

                    audio_source = discord.FFmpegPCMAudio(
                        final_url,
                        executable=FFMPEG_PATH,
                        before_options="-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                        options="-vn"
                    )

                source = discord.PCMVolumeTransformer(audio_source, volume=0.6)

                finished = asyncio.Event()
                loop = asyncio.get_running_loop()

                def _after(err):
                    if err:
                        print("🔥 FFmpeg error:", err)
                    else:
                        print("✅ FFmpeg finished normally")
                    loop.call_soon_threadsafe(finished.set)

                self.voice_client.play(source, after=_after)
                await finished.wait()

                if self.loop_enabled == "track":
                    self.queue._queue.appendleft(track)
                elif self.loop_enabled == "all":
                    await self.queue.put(track)

            except Exception as e:
                print("[Playback error]", e)
                if self.last_channel:
                    try:
                        await self.last_channel.send(f"❌ Ошибка при воспроизведении {track.title}: `{e}`")
                    except:
                        pass

            finally:
                self.current = None
                await asyncio.sleep(0.2)

            if self.queue.empty() and not self.loop_enabled:
                try:
                    await asyncio.wait_for(self.queue.join(), timeout=300)
                except asyncio.TimeoutError:
                    if self.voice_client and self.voice_client.is_connected():
                        await self.voice_client.disconnect()
                    break

# ---------- Хелперы ----------
def is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url

async def search_youtube(query: str) -> Optional[dict]:
    try:
        def _sync(q):
            opts = YTDL_FORMAT_OPTIONS.copy()
            with yt_dlp.YoutubeDL(opts) as y:
                return y.extract_info(f"ytsearch1:{q}", download=False)
        info = await asyncio.to_thread(_sync, query)
        if not info:
            return None
        entries = info.get('entries') or []
        if len(entries) == 0:
            return None
        return entries[0]
    except Exception as e:
        print("yt search error:", e)
        return None

async def resolve_query_to_url(query_or_url: str) -> Optional[dict]:
    try:
        if is_youtube_url(query_or_url):
            cache_entry = track_cache.get(query_or_url)
            if cache_entry:
                return {"title": cache_entry.get("title"), "webpage_url": query_or_url, "duration": cache_entry.get("duration")}
            def _extract(u):
                opts = YTDL_FORMAT_OPTIONS.copy()
                try:
                    with yt_dlp.YoutubeDL(opts) as y:
                        return y.extract_info(u, download=False)
                except Exception:
                    try:
                        fallback = opts.copy()
                        fallback.pop("format", None)
                        with yt_dlp.YoutubeDL(fallback) as y2:
                            return y2.extract_info(u, download=False)
                    except Exception:
                        return None
            info = await asyncio.to_thread(_extract, query_or_url)
            if not info:
                return None
            if "entries" in info and info["entries"]:
                info = info["entries"][0]
            webpage = info.get("webpage_url") or info.get("url") or query_or_url
            title = info.get("title") or query_or_url
            duration = info.get("duration")
            track_cache[webpage] = {"title": title, "duration": duration, "fetched_at": time.time()}
            return {"title": title, "webpage_url": webpage, "duration": duration}
        maybe = await search_youtube(query_or_url)
        if maybe:
            webpage = maybe.get("webpage_url") or maybe.get("url")
            title = maybe.get("title") or query_or_url
            duration = maybe.get("duration")
            if webpage:
                track_cache[webpage] = {"title": title, "duration": duration, "fetched_at": time.time()}
            return {"title": title, "webpage_url": webpage or query_or_url, "duration": duration}
    except Exception as e:
        print("resolve error:", e)
    return None

async def prefetch_stream(track: Track):
    try:
        cache_entry = track_cache.get(track.source)
        if cache_entry and cache_entry.get("stream_url"):
            track.stream_url = cache_entry["stream_url"]
            return
        def _extract_for_stream(u):
            opts = YTDL_FORMAT_OPTIONS.copy()
            try:
                with yt_dlp.YoutubeDL(opts) as y:
                    return y.extract_info(u, download=False)
            except Exception:
                try:
                    fallback = opts.copy()
                    fallback.pop("format", None)
                    with yt_dlp.YoutubeDL(fallback) as y2:
                        return y2.extract_info(u, download=False)
                except Exception:
                    return None
        info = await asyncio.to_thread(_extract_for_stream, track.source)
        if not info:
            return
        if "entries" in info and info["entries"]:
            info = info["entries"][0]
        stream_url = None
        if "url" in info and info["url"]:
            stream_url = info["url"]
        elif "formats" in info and isinstance(info["formats"], list) and len(info["formats"])>0:
            for f in reversed(info["formats"]):
                if f.get("acodec") and f.get("acodec") != "none":
                    stream_url = f.get("url")
                    break
        if stream_url:
            track.stream_url = stream_url
            track_cache[track.source] = {"title": info.get("title"), "duration": info.get("duration"), "stream_url": stream_url, "fetched_at": time.time()}
    except Exception as e:
        print("[prefetch_stream] error", e)

async def ensure_connected(channel: discord.VoiceChannel, player: GuildMusic, interaction: Optional[discord.Interaction] = None):
    try:
        if player.voice_client and player.voice_client.is_connected():
            player.connected.set()
            if interaction:
                try:
                    await interaction.followup.send("⚠️ Я уже подключён.")
                except:
                    pass
            return
        print(f"[ensure_connected] Подключаюсь к {channel} ...")
        vc = await channel.connect()
        player.voice_client = vc
        player.connected.set()
        print(f"[ensure_connected] Успешно подключился к {channel}")
        if interaction:
            try:
                await interaction.followup.send(f"✅ Подключился к {channel.mention}")
            except:
                pass
    except discord.Forbidden:
        print("[ensure_connected] Forbidden")
        if interaction:
            try:
                await interaction.followup.send("❌ У меня нет прав на подключение/говорение.")
            except:
                pass
    except Exception as e:
        print("[ensure_connected] Exception:", e)
        if interaction:
            try:
                await interaction.followup.send(f"❌ Ошибка подключения: {e}")
            except:
                pass

# ---------- ❌ ПРОБЛЕМА в исходном коде ----------
# Раньше здесь был простой вызов ytdl.extract_info(...) и сразу использовались info.get(...)
# Если yt-dlp вернул None (или структура нестандартная) — падали с 'NoneType'.get.
# Ниже — безопасная, с fallback-логикой, реализация.
# ---------- resolve_query_to_url (robust + cache) ----------
async def resolve_query_to_url(query_or_url: str) -> Optional[dict]:
    # Сначала проверим кеш (by query_or_url or by normalized webpage_url)
    # (в кеше мы ключим по source/webpage_url — но при поиске он ещё не извест)
    try:
        # Если это ссылка — быстро проверить кеш
        if is_youtube_url(query_or_url):
            cache_entry = track_cache.get(query_or_url)
            if cache_entry:
                return {
                    "title": cache_entry.get("title"),
                    "webpage_url": query_or_url,
                    "duration": cache_entry.get("duration")
                }

            # sync extract в потоке с fallback'ами
            def _extract(u):
                opts = YTDL_FORMAT_OPTIONS.copy()
                try:
                    with yt_dlp.YoutubeDL(opts) as y:
                        return y.extract_info(u, download=False)
                except Exception:
                    # fallback без формата
                    try:
                        fallback = opts.copy()
                        fallback.pop("format", None)
                        with yt_dlp.YoutubeDL(fallback) as y2:
                            return y2.extract_info(u, download=False)
                    except Exception:
                        return None

            info = await asyncio.to_thread(_extract, query_or_url)
            if not info:
                return None
            if "entries" in info and info["entries"]:
                info = info["entries"][0]

            webpage = info.get("webpage_url") or info.get("url") or query_or_url
            title = info.get("title") or query_or_url
            duration = info.get("duration")
            # сохраняем базовые данные в кеш
            track_cache[webpage] = {
                "title": title,
                "duration": duration,
                # stream_url добавим позже при prefetch/player_loop
                "fetched_at": time.time()
            }
            return {"title": title, "webpage_url": webpage, "duration": duration}

        # если это не ссылка — делаем быстрый поиск (ytsearch1:)
        maybe = await search_youtube(query_or_url)
        if maybe:
            webpage = maybe.get("webpage_url") or maybe.get("url")
            title = maybe.get("title") or query_or_url
            duration = maybe.get("duration")
            if webpage:
                track_cache[webpage] = {
                    "title": title,
                    "duration": duration,
                    "fetched_at": time.time()
                }
            return {"title": title, "webpage_url": webpage or query_or_url, "duration": duration}

    except Exception as e:
        print("resolve error:", e)

    return None

# Prefetch stream url для ускорения старта (в фоне)
async def prefetch_stream(track: Track):
    try:
        # если уже есть в кеше — наполняем track.stream_url
        cache_entry = track_cache.get(track.source)
        if cache_entry and cache_entry.get("stream_url"):
            track.stream_url = cache_entry["stream_url"]
            return

        def _extract_for_stream(u):
            opts = YTDL_FORMAT_OPTIONS.copy()
            try:
                with yt_dlp.YoutubeDL(opts) as y:
                    return y.extract_info(u, download=False)
            except Exception:
                try:
                    fallback = opts.copy()
                    fallback.pop("format", None)
                    with yt_dlp.YoutubeDL(fallback) as y2:
                        return y2.extract_info(u, download=False)
                except Exception:
                    return None

        info = await asyncio.to_thread(_extract_for_stream, track.source)
        if not info:
            return

        if "entries" in info and info["entries"]:
            info = info["entries"][0]

        stream_url = None
        if "url" in info and info["url"]:
            stream_url = info["url"]
        elif "formats" in info and isinstance(info["formats"], list) and len(info["formats"])>0:
            # fallback: возьмём первый доступный аудиоформат
            for f in reversed(info["formats"]):
                if f.get("acodec") and f.get("acodec") != "none":
                    stream_url = f.get("url")
                    break

        if stream_url:
            track.stream_url = stream_url
            track_cache[track.source] = {
                "title": info.get("title"),
                "duration": info.get("duration"),
                "stream_url": stream_url,
                "fetched_at": time.time()
            }
    except Exception as e:
        print("[prefetch_stream] error", e)
        # не критично — player_loop попытается резолвить позже

# ---------- Подключение ----------
async def ensure_connected(channel: discord.VoiceChannel, player: GuildMusic, interaction: Optional[discord.Interaction] = None):
    try:
        if player.voice_client and player.voice_client.is_connected():
            player.connected.set()
            if interaction:
                try:
                    await interaction.followup.send("⚠️ Я уже подключён.")
                except:
                    pass
            return

        print(f"[ensure_connected] Подключаюсь к {channel} ...")
        vc = await channel.connect()
        player.voice_client = vc
        player.connected.set()
        print(f"[ensure_connected] Успешно подключился к {channel}")
        if interaction:
            try:
                await interaction.followup.send(f"✅ Подключился к {channel.mention}")
            except:
                pass

    except discord.Forbidden:
        print("[ensure_connected] Forbidden: нет прав на Connect/Speak")
        if interaction:
            try:
                await interaction.followup.send("❌ У меня нет прав на подключение/говорение в этом голосовом канале.")
            except:
                pass
    except Exception as e:
        print("[ensure_connected] Exception:", e)
        if interaction:
            try:
                await interaction.followup.send(f"❌ Ошибка подключения: {e}")
            except:
                pass

# ---------- Команды ----------
@tree.command(name="leave", description="Отключить бота от голосового канала (только для администрации)")
@app_commands.checks.has_permissions(administrator=True)
async def leave(interaction: discord.Interaction):
    player = get_player(interaction.guild.id)
    if player.voice_client and player.voice_client.is_connected():
        await player.voice_client.disconnect()
        await interaction.response.send_message("👋 Я отключился от голосового канала.")
    else:
        await interaction.response.send_message("❌ Я не в голосовом канале.")

@tree.command(name="playfile", description="🎵 Проиграть трек с вашего устройства (attachment)")
@app_commands.describe(file="Аудиофайл (.mp3, .wav, .m4a, .ogg, .flac)")
async def playfile(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(thinking=True)

    try:
        # 1️⃣ Проверяем голосовой канал
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send("❌ Сначала подключись к голосовому каналу.")
            return

        voice_channel = interaction.user.voice.channel

        # 2️⃣ Проверка расширения
        valid_ext = (".mp3", ".wav", ".m4a", ".ogg", ".flac")
        if not file.filename.lower().endswith(valid_ext):
            await interaction.followup.send("❌ Поддерживаемые форматы: mp3, wav, m4a, ogg, flac.")
            return

        # 3️⃣ Проверка, что команда на сервере
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ Эта команда работает только на сервере.")
            return

        # 4️⃣ Подключение к voice
        player = get_player(guild.id)
        player.last_channel = interaction.channel

        try:
            await ensure_connected(voice_channel, player, interaction)
        except Exception as e:
            await interaction.followup.send(f"⚠️ Ошибка при подключении к voice: `{type(e).__name__}: {e}`")
            print("[playfile] ensure_connected error:", e)
            return

        # 5️⃣ Сохраняем файл
        uploads_dir = "music_uploads"
        os.makedirs(uploads_dir, exist_ok=True)
        safe_name = f"{int(time.time())}_{file.filename}"
        filepath = os.path.abspath(os.path.join(uploads_dir, safe_name))

        try:
            await file.save(fp=filepath)
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка при сохранении файла: `{type(e).__name__}: {e}`")
            print("[playfile] file save error:", e)
            return

        if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
            await interaction.followup.send("❌ Ошибка: файл не сохранился или пуст.")
            print("[playfile] file invalid:", filepath)
            return

        print(f"[playfile] saved file: {filepath} (size: {os.path.getsize(filepath)} bytes)")

        # 6️⃣ Проверим доступность ffmpeg
        if not os.path.exists(FFMPEG_PATH):
            await interaction.followup.send(f"⚠️ ffmpeg не найден по пути: `{FFMPEG_PATH}`")
            return
        else:
            print(f"[playfile] ffmpeg найден по пути: {FFMPEG_PATH}")

        # 7️⃣ Создаём объект трека
        title = os.path.splitext(file.filename)[0]
        track = Track(
            title=title,
            source=filepath,
            filepath=filepath,
            requested_by=interaction.user,
            duration=None
        )

        # 8️⃣ Добавляем в очередь
        await player.queue.put(track)
        print(f"[playfile] queued: {title}")

        # 🔍 Проверим, можем ли воспроизвести вручную (указан ffmpeg путь!)
        try:
            test_audio = discord.FFmpegPCMAudio(
                filepath,
                executable=FFMPEG_PATH,
                before_options="-nostdin",
                options="-vn"
            )
            test_audio.cleanup()  # просто проверить, создается ли объект
            print("[playfile] FFmpegPCMAudio успешно инициализировался ✅")
        except Exception as e:
            await interaction.followup.send(f"⚠️ FFmpeg не смог открыть файл: `{type(e).__name__}: {e}`")
            print("[playfile] FFmpeg init error:", e)
            return

        # 9️⃣ Если player_loop не запущен — запускаем
        if not player.loop_task or player.loop_task.done():
            player.loop_task = asyncio.create_task(player.player_loop(guild.id))
            print("[playfile] started player loop for guild:", guild.id)

        queue_position = player.queue.qsize()

        # 🔟 Embed-ответ
        embed = discord.Embed(
            title="📂 Трек добавлен в очередь",
            description=f"**{title}**",
            color=discord.Color.green()
        )
        embed.add_field(name="👤 Добавил", value=interaction.user.mention, inline=True)
        embed.add_field(name="🔢 Позиция", value=str(queue_position), inline=True)

        await interaction.followup.send(embed=embed)

        # 🔁 Реплика в чат
        try:
            await interaction.channel.send(make_n_reply_ru(track.title, "add"))
        except Exception as e:
            print("[playfile] make_n_reply_ru error:", e)

    except Exception as e:
        await interaction.followup.send(f"💥 Непредвиденная ошибка: `{type(e).__name__}: {e}`")
        import traceback
        traceback.print_exc()

@tree.command(name="play", description="Воспроизвести трек с YouTube или по ссылке")
@app_commands.describe(query="Название трека или ссылка на YouTube")
async def play(interaction: discord.Interaction, query: str):
    # быстрый ответ пользователю — defer + потом followup (placeholder)
    await interaction.response.defer()
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send("❌ Зайди в голосовой канал сначала.")
        return

    guild = interaction.guild
    if not guild:
        await interaction.followup.send("❌ Команда работает только на сервере.")
        return

    player = get_player(guild.id)
    player.last_channel = interaction.channel

    # Подключаемся (передаём interaction, чтобы ensure_connected мог отправить followup)
    await ensure_connected(interaction.user.voice.channel, player, interaction)

    # мгновенно отправим placeholder — чтобы пользователь видел ответ сразу
    placeholder = await interaction.followup.send(f"🔎 Добавляю в очередь: `{query}`…")

    # ленивый resolve (быстро вернётся на большинство запросов)
    data = await resolve_query_to_url(query)
    if not data:
        try:
            await placeholder.edit(content=f"❌ Не удалось найти трек по запросу: `{query}`")
        except:
            await interaction.followup.send(f"❌ Не удалось найти трек по запросу: `{query}`")
        return

    # Если в кеше есть title/webpage_url, используем их
    title = data.get("title") or query
    webpage = data.get("webpage_url") or query
    duration = data.get("duration")

    track = Track(title=title, source=webpage, requested_by=interaction.user, duration=duration)
    await player.queue.put(track)

    # Запускаем фоновый prefetch stream (ускорит реальный старт)
    asyncio.create_task(prefetch_stream(track))

    if not player.loop_task or player.loop_task.done():
        player.loop_task = asyncio.create_task(player.player_loop(guild.id))

    queue_position = player.queue.qsize()
    duration = track.duration
    if duration:
        hours, rem = divmod(duration, 3600)
        minutes, seconds = divmod(rem, 60)
        duration_str = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
    else:
        duration_str = "Неизвестно"

    embed = discord.Embed(
        title="🎶 Added to queue",
        description=f"[{track.title}]({track.source})",
        color=discord.Color.green()
    )
    embed.add_field(name="👤 Добавил", value=interaction.user.mention, inline=True)
    embed.add_field(name="⏱ Длительность", value=duration_str, inline=True)
    embed.add_field(name="🔢 Позиция", value=str(queue_position), inline=True)

    try:
        await placeholder.edit(content=None, embed=embed)
    except Exception:
        await interaction.followup.send(embed=embed)

    try:
        await interaction.channel.send(make_n_reply_ru(track.title, "add"))
    except Exception:
        pass

@tree.command(name="pause", description="Приостановить текущий трек")
async def pause(interaction: discord.Interaction):
    player = get_player(interaction.guild.id)
    if player.voice_client and player.voice_client.is_playing():
        player.voice_client.pause()
        await interaction.response.send_message(make_n_reply_ru(player.current.title if player.current else "", "pause"))
    else:
        await interaction.response.send_message("❌ Сейчас ничего не играет.")

@tree.command(name="resume", description="Продолжить воспроизведение трека")
async def resume(interaction: discord.Interaction):
    player = get_player(interaction.guild.id)
    if player.voice_client and player.voice_client.is_paused():
        player.voice_client.resume()
        await interaction.response.send_message(make_n_reply_ru(player.current.title if player.current else "", "resume"))
    else:
        await interaction.response.send_message("❌ Трек не на паузе.")

@tree.command(name="stop", description="Остановить воспроизведение и очистить очередь")
async def stop(interaction: discord.Interaction):
    player = get_player(interaction.guild.id)
    if player.voice_client:
        title = player.current.title if player.current else ""
        try:
            player.voice_client.stop()
        except Exception:
            pass
        guild_players[interaction.guild.id] = GuildMusic()
        await interaction.response.send_message(make_n_reply_ru(title, "stop"))
    else:
        await interaction.response.send_message("❌ Сейчас ничего не играет.")

@tree.command(name="skip", description="Пропустить текущий трек")
async def skip(interaction: discord.Interaction):
    player = get_player(interaction.guild.id)
    if player.voice_client and player.voice_client.is_playing():
        player.voice_client.stop()
        await interaction.response.send_message(make_n_reply_ru(player.current.title if player.current else "", "skip"))
    else:
        await interaction.response.send_message("❌ Сейчас ничего не играет.")

from discord.ui import View, button
import discord, math, time, asyncio

@tree.command(name="queue", description="Показать очередь треков")
async def queue_cmd(interaction: discord.Interaction):
    await interaction.response.defer()

    player = get_player(interaction.guild.id)
    queue_list = list(getattr(getattr(player, "queue", None), "_queue", []))

    if not player.current and not queue_list:
        await interaction.followup.send("📃 Очередь пуста.")
        return

    per_page = 5  # количество треков на странице

    # ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====
    def format_duration(seconds: int) -> str:
        if seconds is None or seconds < 0:
            return "?:??"
        minutes, sec = divmod(int(seconds), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{sec:02d}" if hours else f"{minutes}:{sec:02d}"

    def progress_bar(current_time: int, duration: int, length: int = 18) -> str:
        if not duration or duration <= 0:
            return "▱" * length
        ratio = max(0.0, min(1.0, current_time / duration))
        filled = int(length * ratio)
        return "▰" * filled + "▱" * (length - filled)

    def get_page_embed(page_num: int) -> discord.Embed:
        embed = discord.Embed(
            title="🎶 Очередь воспроизведения",
            color=discord.Color.blurple()
        )

        # ▶️ Текущий трек
        if getattr(player, "current", None):
            duration = getattr(player.current, "duration", 0) or 0

            # создаем start_time если его нет
            start_time = getattr(player, "start_time", None)
            if not start_time:
                player.start_time = time.time()
                start_time = player.start_time

            elapsed = int(time.time() - start_time)
            elapsed = max(0, min(elapsed, duration))
            remaining = duration - elapsed

            bar = progress_bar(elapsed, duration)

            embed.add_field(
                name="▶ Сейчас играет",
                value=(
                    f"**[{player.current.title}]({player.current.source})**\n"
                    f"`{bar}` `{format_duration(elapsed)} / {format_duration(duration)}` "
                    f"(-{format_duration(remaining)} осталось)\n"
                    f"👤 {getattr(player.current, 'requested_by', interaction.user).mention}"
                ),
                inline=False
            )

            # Следующий трек
            if len(queue_list) > 0:
                nxt = queue_list[0]
                embed.add_field(
                    name="⏭ Следующий трек",
                    value=f"[{nxt.title}]({nxt.source}) • 👤 {getattr(nxt, 'requested_by', interaction.user).mention}",
                    inline=False
                )

        # 📜 Остальная очередь (страницы)
        if queue_list:
            start = page_num * per_page
            end = start + per_page
            tracks = queue_list[start:end]

            desc = ""
            for i, track in enumerate(tracks, start=start + 1):
                marker = "🔹" if track == player.current else f"**{i}.**"
                user = getattr(track, "requested_by", None)
                user_text = f" • 👤 {user.mention}" if user else ""
                desc += f"{marker} [{track.title}]({track.source}){user_text}\n"

            total_pages = math.ceil(len(queue_list) / per_page)
            embed.add_field(
                name=f"📃 Очередь (страница {page_num + 1}/{total_pages})",
                value=desc or "—",
                inline=False
            )

        embed.set_footer(text=f"Запросил {interaction.user.display_name}")
        return embed

    # ===== ВИД С КНОПКАМИ И ОБНОВЛЕНИЕМ =====
    class QueueView(View):
        def __init__(self):
            super().__init__(timeout=180)
            self.page = 0
            self.message = None
            self.running = True
            asyncio.create_task(self.update_loop())

        async def update_loop(self):
            """Обновление прогрессбара каждые 5 секунд"""
            await asyncio.sleep(2)
            while self.running:
                try:
                    await asyncio.sleep(5)
                    if self.message:
                        await self.message.edit(embed=get_page_embed(self.page), view=self)
                except Exception:
                    break

        @button(label="⬅", style=discord.ButtonStyle.secondary)
        async def prev_page(self, inter: discord.Interaction, button: discord.ui.Button):
            if self.page > 0:
                self.page -= 1
            await inter.response.edit_message(embed=get_page_embed(self.page), view=self)

        @button(label="➡", style=discord.ButtonStyle.secondary)
        async def next_page(self, inter: discord.Interaction, button: discord.ui.Button):
            if (self.page + 1) * per_page < len(queue_list):
                self.page += 1
            await inter.response.edit_message(embed=get_page_embed(self.page), view=self)

        @button(label="❌ Закрыть", style=discord.ButtonStyle.danger)
        async def close(self, inter: discord.Interaction, button: discord.ui.Button):
            self.running = False
            for item in self.children:
                item.disabled = True
            try:
                await inter.message.delete()
            except:
                await inter.response.edit_message(content="Очередь закрыта.", view=None)

        async def on_timeout(self):
            self.running = False
            for item in self.children:
                item.disabled = True
            try:
                await self.message.edit(view=self)
            except:
                pass

    # ===== ОТПРАВКА EMBED =====
    view = QueueView()
    msg = await interaction.followup.send(embed=get_page_embed(0), view=view)
    view.message = msg

@tree.command(name="repeat", description="Управление повтором трека или очереди")
@app_commands.describe(mode="Варианты: track / all / off (если не указано — переключает текущий трек)")
async def repeat(interaction: discord.Interaction, mode: str = None):
    player = get_player(interaction.guild.id)

    if mode:
        mode = mode.lower()
        if mode == "track":
            if not player.current:
                await interaction.response.send_message("❌ Сейчас ничего не играет.")
                return
            player.loop_enabled = "track"
            await interaction.response.send_message(f"🔂 Повтор включен для трека **{player.current.title}**")

        elif mode == "all":
            if not player.current and player.queue.empty():
                await interaction.response.send_message("❌ Очередь пуста.")
                return
            player.loop_enabled = "all"
            await interaction.response.send_message("🔁 Повтор включен для всей очереди.")

        elif mode == "off":
            player.loop_enabled = False
            await interaction.response.send_message("⏹ Повтор отключен.")

        else:
            await interaction.response.send_message("⚠ Укажи режим: `track`, `all` или `off`.")

    else:  # если режим не указан — переключаем текущий трек
        if not player.current:
            await interaction.response.send_message("❌ Сейчас ничего не играет.")
            return
        if player.loop_enabled == "track":
            player.loop_enabled = False
            event = "loop_off"
        else:
            player.loop_enabled = "track"
            event = "loop_on"
        await interaction.response.send_message(make_n_reply_ru(player.current.title, event))

# ---------- События ----------
@bot.event
async def on_ready():
    print(f"✅ Бот {bot.user} подключился! ID: {bot.user.id}")
    print(f"🌐 Бот на {len(bot.guilds)} серверах:")
    for guild in bot.guilds:
        print(f"   - {guild.name} (ID: {guild.id}) | Участников: {guild.member_count}")
    try:
        g = bot.get_guild(GUILD_ID)
        if g:
            tree.copy_global_to(guild=g)
            await tree.sync(guild=g)
            print(f"✅ Команды синхронизированы с сервером {g.name}")
        else:
            synced = await tree.sync()
            print(f"✅ Синхронизировано {len(synced)} команд глобально")
    except Exception as e:
        print("Ошибка синхронизации команд:", e)

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    for guild_id, player in guild_players.items():
        if player.voice_client and before.channel == player.voice_client.channel and after.channel is None:
            if len(player.voice_client.channel.members) == 1:
                await player.voice_client.disconnect()

# ---------- Запуск ----------
if __name__ == "__main__":
    TOKEN = "MTQxNzQ4MTU2NjA5OTM0MTM0Mg.Galo3D.voBI6RHVPtAunwayPRL3RVL0Rt8ai0i8s7OuvM"
    print("🚀 Запускаю бота...")
    bot.run(TOKEN)