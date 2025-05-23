import os
import logging
import tempfile
import time
from pathlib import Path
from typing import Dict, List

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
auth_manager = SpotifyClientCredentials(
    client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET
)
sp = spotipy.Spotify(auth_manager=auth_manager, requests_session=session)

# User session data storage
user_data: Dict[int, Dict] = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation and ask user for language."""
    user = update.message.from_user
    logger.info(f"User {user.id} started the bot.")
    
    keyboard = [
        [InlineKeyboardButton("English", callback_data="en")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "Hello! I'm your Singer Playlist Bot. Let's get started! "
        "Which language would you like to use? 😊",
        reply_markup=reply_markup,
    )
    
    return LANGUAGE


async def language_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle language selection."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    user_data[user_id] = {"language": query.data}
    
    await query.edit_message_text(
        text="Awesome! Now, tell me the name of a singer to create a playlist for you! 🌟"
    )
    
    return ARTIST


async def get_artist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Get artist name and search for their top tracks."""
    user = update.message.from_user
    artist_name = update.message.text
    logger.info(f"User {user.id} requested artist: {artist_name}")
    
    # Show searching message
    await update.message.reply_text(
        f"Searching for songs by {artist_name}... Please wait! ⏳"
    )
    
    try:
        # Search Spotify for artist's top tracks with retries
        tracks = []
        for attempt in range(SPOTIFY_RETRIES):
            try:
                # First search for the artist
                result = sp.search(q=f"artist:{artist_name}", type="artist", limit=1)
                if not result["artists"]["items"]:
                    raise ValueError("Artist not found")
                
                artist_id = result["artists"]["items"][0]["id"]
                
                # Get top tracks
                top_tracks = sp.artist_top_tracks(artist_id)
                tracks = [
                    {
                        "name": track["name"],
                        "artist": ", ".join(
                            artist["name"] for artist in track["artists"]
                        ),
                        "full_query": f"{track['name']} {artist_name}",
                    }
                    for track in top_tracks["tracks"][:10]  # Limit to top 10
                ]
                break
            except spotipy.SpotifyException as e:
                if attempt == SPOTIFY_RETRIES - 1:
                    raise
                logger.warning(f"Spotify API attempt {attempt + 1} failed: {e}")
                time.sleep(1)  # Wait before retrying
        
        if not tracks:
            await update.message.reply_text(
                f"Couldn't find any tracks for {artist_name}. Please try another artist! 😊"
            )
            return ARTIST
        
        # Store tracks in user data
        user_data[user.id] = {
            **user_data.get(user.id, {}),
            "artist": artist_name,
            "tracks": tracks,
        }
        
        # Format playlist message
        playlist_msg = f"🎵 Playlist for {artist_name}:\n\n"
        for i, track in enumerate(tracks, start=1):
            playlist_msg += f"{i}. {track['name']}\n"
        
        playlist_msg += (
            "\nWhich song would you like to download? Type the number! "
            "Or, type a new singer's name to create another playlist! 🌟"
        )
        
        await update.message.reply_text(playlist_msg)
        
        return SONG_SELECTION
    
    except Exception as e:
        logger.error(f"Error searching for artist {artist_name}: {e}", exc_info=True)
        await update.message.reply_text(
            "Something went wrong while searching. Please try another artist! 😊"
        )
        return ARTIST


async def download_song(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Download the selected song from YouTube."""
    user = update.message.from_user
    user_input = update.message.text
    logger.info(f"User {user.id} input: {user_input}")
    
    # Check if user entered a new artist name instead of a number
    if not user_input.isdigit():
        return await get_artist(update, context)
    
    # Get selected track
    try:
        track_index = int(user_input) - 1
        tracks = user_data[user.id]["tracks"]
        if track_index < 0 or track_index >= len(tracks):
            raise IndexError("Invalid track index")
        
        selected_track = tracks[track_index]
        search_query = selected_track["full_query"]
        
        await update.message.reply_text(
            f"⬇️ Downloading {selected_track['name']}... Please wait a moment! 😊"
        )
        
        # Prepare yt-dlp options
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(TEMP_DIR / "%(title)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "noplaylist": True,
            "default_search": "ytsearch",
            "socket_timeout": 30,
            "retries": 10,
            "extractaudio": True,
            "audioformat": "mp3",
            "nocheckcertificate": True,
            "ignoreerrors": True,
            "logtostderr": False,
            "restrictfilenames": True,
            "no_color": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
        }
        
        # Temporary file path
        temp_file = None
        
        try:
            # Search and download with yt-dlp with retries
            for attempt in range(YT_RETRIES):
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        # Add delay between YouTube requests
                        if attempt > 0:
                            time.sleep(YT_DELAY)
                        
                        info = ydl.extract_info(f"ytsearch:{search_query}", download=True)
                        
                        if not info or "entries" not in info or not info["entries"]:
                            raise ValueError("No videos found")
                        
                        video = info["entries"][0]
                        temp_file = TEMP_DIR / f"{video['title']}.mp3"
                        
                        # Check file size
                        file_size = temp_file.stat().st_size
                        if file_size > MAX_FILE_SIZE:
                            raise ValueError(
                                f"File too large ({file_size / 1024 / 1024:.2f}MB)"
                            )
                        
                        # Send the audio file
                        with open(temp_file, "rb") as audio_file:
                            await update.message.reply_audio(
                                audio=audio_file,
                                title=video["title"],
                                performer=selected_track["artist"],
                                caption=f"{selected_track['name']} downloaded! Enjoy the music! 😊❤️",
                            )
                        
                        break
                except yt_dlp.utils.DownloadError as e:
                    if "Sign in to confirm you're not a bot" in str(e):
                        logger.warning(f"YouTube bot detection triggered (attempt {attempt + 1})")
                        if attempt == YT_RETRIES - 1:
                            raise RuntimeError("YouTube blocked the request. Please try again later.")
                        continue
                    raise
        except Exception as e:
            logger.error(f"Error downloading song: {e}", exc_info=True)
            await update.message.reply_text(
                f"Sorry, I couldn't download the song. Error: {str(e)}. Please try another song! 😊"
            )
        finally:
            # Clean up temporary file
            if temp_file and temp_file.exists():
                try:
                    temp_file.unlink()
                    logger.info(f"Deleted temporary file: {temp_file}")
                except Exception as e:
                    logger.error(f"Error deleting temporary file: {e}", exc_info=True)
        
        # Ask if user wants to select another song
        await update.message.reply_text(
            "Would you like to download another song from this playlist? "
            "Type the number! Or, type a new singer's name to create another playlist! 🌟"
        )
        
        return SONG_SELECTION
    
    except (IndexError, KeyError, ValueError) as e:
        logger.error(f"Invalid selection: {e}", exc_info=True)
        await update.message.reply_text(
            "Please enter a valid number from the playlist or a new artist name! 😊"
        )
        return SONG_SELECTION
    except Exception as e:
        logger.error(f"Unexpected error in download_song: {e}", exc_info=True)
        await update.message.reply_text(
            "Something went wrong. Let's start over with a new artist! 😊"
        )
        return ARTIST


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the current operation."""
    user = update.message.from_user
    logger.info(f"User {user.id} canceled the conversation.")
    
    await update.message.reply_text(
        "Okay, let's start over when you're ready! Just send /start to begin again. 😊"
    )
    
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors and send a user-friendly message."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    
    if update and hasattr(update, "message"):
        await update.message.reply_text(
            "Oops! Something went wrong. Let's try again! 😊"
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
    )
    
    # Add handlers
    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)
    
    # Run the bot
    application.run_polling()


if __name__ == "__main__":
    main()