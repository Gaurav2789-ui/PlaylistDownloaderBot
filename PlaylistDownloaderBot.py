import os
import logging
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
)
import yt_dlp
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Constants
LANGUAGE, ARTIST, SONG_SELECTION = range(3)
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB Telegram limit
DOWNLOAD_TIMEOUT = 300  # 5 minutes
SPOTIFY_RETRIES = 3
YT_RETRIES = 3
YT_DELAY = 2  # seconds between YouTube requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# Environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Validate environment variables
for key, value in [
    ("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
    ("SPOTIFY_CLIENT_ID", SPOTIFY_CLIENT_ID),
    ("SPOTIFY_CLIENT_SECRET", SPOTIFY_CLIENT_SECRET),
]:
    if not value:
        logger.error(f"Missing environment variable: {key}")
        raise ValueError(f"Environment variable {key} is not set")

# Ensure temp directory exists
TEMP_DIR = Path("temp_downloads")
TEMP_DIR.mkdir(exist_ok=True)

# Configure requests session with retries
session = requests.Session()
retry = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
)
adapter = HTTPAdapter(max_retries=retry)
session.mount("http://", adapter)
session.mount("https://", adapter)

# Initialize Spotify client
try:
    auth_manager = SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET
    )
    sp = spotipy.Spotify(auth_manager=auth_manager, requests_session=session)
    logger.info("Spotify client initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Spotify client: {str(e)}")
    raise

# User session data storage
user_data: Dict[int, Dict] = {}

# Function to search YouTube with retries and bot detection avoidance
async def search_youtube(song_name: str, artist: str) -> Optional[str]:
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'force_generic_extractor': True,
        'geo_bypass': True,
        'no_check_certificate': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    }
    search_queries = [
        f"{song_name} {artist} official video",
        f"{song_name} {artist} official audio",
        f"{song_name} {artist} official",
        f"{song_name} {artist}",
        f"{song_name} audio {artist}",
        f"{song_name} by {artist}",
    ]
    
    for query in search_queries:
        for attempt in range(YT_RETRIES):
            try:
                logger.info(f"Attempt {attempt + 1}/{YT_RETRIES} - Searching YouTube with query: {query}")
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    result = ydl.extract_info(f"ytsearch5:{query}", download=False)
                    if 'entries' in result and result['entries']:
                        for entry in result['entries'][:5]:
                            url = entry['url']
                            test_opts = {
                                'format': 'bestaudio/best',
                                'quiet': True,
                                'no_warnings': True,
                                'simulate': True,
                                'geo_bypass': True,
                                'no_check_certificate': True,
                                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
                            }
                            with yt_dlp.YoutubeDL(test_opts) as test_ydl:
                                try:
                                    test_ydl.extract_info(url, download=False)
                                    logger.info(f"Found valid URL: {url}")
                                    return url
                                except Exception as e:
                                    logger.warning(f"URL {url} failed test: {str(e)}")
                                    continue
                logger.warning(f"No downloadable URLs found for query: {query}")
                break
            except Exception as e:
                logger.error(f"Error searching YouTube for {query}: {str(e)}")
                if "Sign in to confirm you’re not a bot" in str(e):
                    logger.warning("YouTube bot detection triggered. Retrying with delay...")
                if attempt == YT_RETRIES - 1:
                    logger.error(f"Failed to search YouTube for {query} after {YT_RETRIES} attempts")
                    break
                await asyncio.sleep(YT_DELAY)
        await asyncio.sleep(1)
    logger.warning(f"No valid URLs found for {song_name}")
    return None

# Function to download YouTube audio
async def download_youtube_audio(url: str, output_name: str) -> Optional[str]:
    output_path = os.path.join(TEMP_DIR, output_name)
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_path + '.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'retries': YT_RETRIES,
        'socket_timeout': 30,
        'extractaudio': True,
        'audioformat': 'm4a',
        'prefer_ffmpeg': False,
        'keepvideo': False,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    }

    for attempt in range(YT_RETRIES):
        try:
            logger.info(f"Download attempt {attempt + 1}/{YT_RETRIES} for {url}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            
            for ext in ['.m4a', '.webm', '.mp3']:
                audio_path = f"{output_path}{ext}"
                if os.path.exists(audio_path):
                    logger.info(f"Successfully downloaded to {audio_path}")
                    return audio_path
            
            raise Exception("Download completed but audio file not found")
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} failed: {str(e)}")
            if attempt == YT_RETRIES - 1:
                logger.error(f"Failed to download {url} after {YT_RETRIES} attempts: {str(e)}")
                return None
            await asyncio.sleep(YT_DELAY)
    return None

# Start command handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.message.from_user
    logger.info(f"Start command from {user.id} ({user.username})")
    
    keyboard = [[InlineKeyboardButton("English", callback_data='lang_en')]]
    try:
        await update.message.reply_text(
            "Hello! I'm your Singer Playlist Bot. Let's get started! Which language would you like to use? 😊",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return LANGUAGE
    except Exception as e:
        logger.error(f"Error in start command: {str(e)}")
        await update.message.reply_text("Oops! Something went wrong. Please try again with /start. 😊")
        return ConversationHandler.END

# Language selection handler
async def language_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    try:
        await query.answer()
        chat_id = query.message.chat_id
        lang = query.data.split('_')[1]
        user_data[chat_id] = {'lang': lang}
        
        await query.edit_message_text("Awesome! Now, tell me the name of a singer to create a playlist for you! 🌟")
        return ARTIST
    except Exception as e:
        logger.error(f"Error in language_selection: {str(e)}")
        await query.message.reply_text("Oops! Something went wrong. Please start again with /start. 😊")
        return ConversationHandler.END

# Artist search handler
async def get_artist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.message.chat_id
    artist = update.message.text.strip()
    user_data[chat_id]['artist'] = artist
    
    try:
        await update.message.reply_text(f"Searching for songs by {artist}... Please wait! ⏳")
        
        for attempt in range(SPOTIFY_RETRIES):
            try:
                results = sp.search(q=f'artist:{artist}', type='artist', limit=1)
                if not results['artists']['items']:
                    await update.message.reply_text(f"Sorry, I couldn't find {artist}. Try another name! 😊")
                    return ARTIST
                break
            except requests.exceptions.RequestException as e:
                logger.warning(f"Network error with Spotify (attempt {attempt + 1}/{SPOTIFY_RETRIES}): {str(e)}")
                if attempt == SPOTIFY_RETRIES - 1:
                    logger.error(f"Failed to search Spotify for {artist} after {SPOTIFY_RETRIES} attempts")
                    await update.message.reply_text(f"Network issue while connecting to Spotify. Error: {str(e)}. Please try again later! 😊")
                    return ARTIST
                await asyncio.sleep(1)
        
        artist_id = results['artists']['items'][0]['id']
        tracks = sp.artist_top_tracks(artist_id)
        song_list: List[Tuple[str, str]] = [(track['name'], track['external_urls']['spotify']) for track in tracks['tracks'][:10]]
        
        downloadable_songs = []
        for song_name, _ in song_list:
            try:
                yt_url = await search_youtube(song_name, artist)
                if yt_url:
                    downloadable_songs.append((song_name, yt_url))
                else:
                    await update.message.reply_text(f"Couldn't find {song_name} on YouTube. Skipping...")
            except Exception as e:
                logger.error(f"Error searching for {song_name} on YouTube: {str(e)}")
                await update.message.reply_text(f"Error searching for {song_name} on YouTube. Skipping...")
                continue

        if not downloadable_songs:
            await update.message.reply_text("No downloadable songs found. Try another artist! 😊")
            return ARTIST

        user_data[chat_id]['songs'] = downloadable_songs
        
        playlist_text = f"🎵 Playlist for {artist}:\n\n"
        for idx, (song_name, _) in enumerate(downloadable_songs, 1):
            playlist_text += f"{idx}. {song_name}\n"
        
        playlist_text += "\nWhich song would you like to download? Type the number! Or, type a new artist's name to create another playlist! 🌟"
        await update.message.reply_text(playlist_text)
        return SONG_SELECTION

    except Exception as e:
        logger.error(f"Error in get_artist: {str(e)}")
        await update.message.reply_text(f"Oops! Something went wrong while searching for {artist}. Error: {str(e)}. Let's try again! 😊")
        return ARTIST

# Download song handler
async def download_song(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.message.chat_id
    user_input = update.message.text.strip()

    try:
        try:
            song_idx = int(user_input) - 1
        except ValueError:
            logger.info(f"User entered a new artist name: {user_input} from chat_id: {chat_id}")
            user_data[chat_id]['artist'] = user_input
            await update.message.reply_text(f"Got it! I'm searching for songs by {user_input}... Please wait a moment! 😊")
            
            try:
                results = sp.search(q=f'artist:{user_input}', type='artist', limit=1)
                if not results['artists']['items']:
                    await update.message.reply_text(f"Sorry, I couldn't find any artist named {user_input}. Try another name! 😊")
                    return ARTIST

                artist_id = results['artists']['items'][0]['id']
                tracks = sp.artist_top_tracks(artist_id)
                song_list = [(track['name'], track['external_urls']['spotify']) for track in tracks['tracks'][:10]]

                downloadable_songs = []
                for song_name, _ in song_list:
                    try:
                        yt_url = await search_youtube(song_name, user_input)
                        if yt_url:
                            downloadable_songs.append((song_name, yt_url))
                        else:
                            await update.message.reply_text(f"Couldn't find a downloadable version of {song_name} on YouTube. Skipping this song... 😊")
                    except Exception as e:
                        await update.message.reply_text(f"Error while searching for {song_name} on YouTube. Error: {str(e)}. Skipping this song... 😊")
                        continue

                if not downloadable_songs:
                    await update.message.reply_text(f"Sorry, I couldn't find any downloadable links for this artist. Try another name! 😊")
                    return ARTIST

                user_data[chat_id]['songs'] = downloadable_songs

                playlist_text = f"Here's the playlist for {user_input}:\n\n"
                for idx, (song_name, _) in enumerate(downloadable_songs, 1):
                    playlist_text += f"{idx}. {song_name}\n"
                playlist_text += "\nWhich song would you like to download? Just type the number (e.g., 1)! Or, type a new artist's name to create another playlist! 🌟"
                await update.message.reply_text(playlist_text)
                return SONG_SELECTION
            except Exception as e:
                logger.error(f"Error in artist search for {user_input}: {str(e)}")
                await update.message.reply_text(f"Oops! Something went wrong while searching for {user_input}. Error: {str(e)}. Let's try again! 😊")
                return ARTIST

        songs = user_data[chat_id]['songs']
        artist = user_data[chat_id]['artist']
        
        if song_idx < 0 or song_idx >= len(songs):
            await update.message.reply_text("Please choose a valid number from the playlist! 😊")
            return SONG_SELECTION

        song_name, yt_url = songs[song_idx]
        await update.message.reply_text(f"⬇️ Downloading {song_name}... Please wait a moment! 😊")

        try:
            safe_name = "".join(c for c in song_name if c.isalnum() or c in " _-")
            audio_path = await download_youtube_audio(yt_url, f"{artist}_{safe_name}")
            
            if not audio_path:
                await update.message.reply_text("Sorry, I couldn't download this song. Let's try another one! 😊")
                return SONG_SELECTION

            file_size = os.path.getsize(audio_path)
            if file_size > MAX_FILE_SIZE:
                await update.message.reply_text(
                    f"Sorry, the file {song_name} is {file_size / (1024 * 1024):.2f}MB, "
                    f"which is larger than Telegram's 50MB limit. Please try another song! 😊"
                )
                os.remove(audio_path)
            else:
                with open(audio_path, 'rb') as audio_file:
                    await update.message.reply_audio(
                        audio=audio_file,
                        title=song_name,
                        performer=artist,
                        timeout=DOWNLOAD_TIMEOUT
                    )
                await update.message.reply_text(f"{song_name} downloaded! Enjoy the music! 😊❤️")
            
            try:
                os.remove(audio_path)
            except Exception as e:
                logger.warning(f"Failed to delete temporary file {audio_path}: {str(e)}")

        except Exception as e:
            logger.error(f"Download error for {song_name}: {str(e)}")
            await update.message.reply_text(f"Error while downloading {song_name}. Error: {str(e)}. Let's try another song! 😊")
            return SONG_SELECTION

        playlist_text = f"🎵 Playlist for {artist}:\n\n"
        for idx, (song_name, _) in enumerate(songs, 1):
            playlist_text += f"{idx}. {song_name}\n"
        playlist_text += "\nWhich song would you like to download? Type the number! Or, type a new artist's name to create another playlist! 🌟"
        await update.message.reply_text(playlist_text)
        return SONG_SELECTION

    except Exception as e:
        logger.error(f"Error in download_song: {str(e)}")
        await update.message.reply_text(f"Oops! Something went wrong while downloading. Error: {str(e)}. Let's try again! 😊")
        return SONG_SELECTION

# Cancel command handler
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        await update.message.reply_text("Operation cancelled. Type /start to begin again! 😊")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in cancel command: {str(e)}")
        return ConversationHandler.END

# Error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.message:
        await update.message.reply_text(
            "An unexpected error occurred. Please try again with /start. 😊"
        )

def main() -> None:
    """Run the bot."""
    # Create the Application
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Set up conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            LANGUAGE: [CallbackQueryHandler(language_selection)],
            ARTIST: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_artist)],
            SONG_SELECTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, download_song)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=True  # Set to True to avoid PTBUserWarning
    )
    
    # Add handlers
    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)
    
    # Run the bot with webhook if WEBHOOK_URL is set
    if WEBHOOK_URL:
        PORT = int(os.environ.get("PORT", 10000))
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TELEGRAM_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}"
        )
    else:
        # For local development or Render with polling
        max_retries = 3
        for attempt in range(max_retries):
            try:
                logger.info(f"Attempt {attempt + 1}/{max_retries} to start polling...")
                application.run_polling(allowed_updates=Update.ALL_TYPES)
                break
            except telegram.error.Conflict as e:
                logger.warning(f"Conflict error on attempt {attempt + 1}: {str(e)}")
                if attempt == max_retries - 1:
                    logger.error("Failed to start bot after maximum retries due to Conflict error")
                    raise
                logger.info("Waiting 5 seconds before retrying...")
                time.sleep(5)
            except Exception as e:
                logger.error(f"Unexpected error while starting bot: {str(e)}")
                raise

if __name__ == "__main__":
    main()