import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import os
import requests
import yt_dlp
import time
from pathlib import Path

# Enhanced logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Conversation states
LANGUAGE, SINGER, DOWNLOAD = range(3)

# Global dictionary to store user data
user_data = {}

# Configuration
CONFIG = {
    'SPOTIFY_CLIENT_ID': os.getenv('SPOTIFY_CLIENT_ID'),
    'SPOTIFY_CLIENT_SECRET': os.getenv('SPOTIFY_CLIENT_SECRET'),
    'TELEGRAM_TOKEN': os.getenv('TELEGRAM_TOKEN'),
    'MAX_FILE_SIZE_MB': 50,
    'TEMP_DIR': 'temp_downloads',
}

# Ensure temp directory exists
Path(CONFIG['TEMP_DIR']).mkdir(exist_ok=True)

# Initialize Spotify client
try:
    auth_manager = SpotifyClientCredentials(
        client_id=CONFIG['SPOTIFY_CLIENT_ID'],
        client_secret=CONFIG['SPOTIFY_CLIENT_SECRET']
    )
    sp = spotipy.Spotify(auth_manager=auth_manager)
    logger.info("Spotify client initialized successfully")
except Exception as e:
    logger.error(f"Spotify setup failed: {str(e)}")
    raise

# Function to search YouTube using yt-dlp (try up to 3 results with fallback queries)
async def search_youtube(song_name: str, singer: str) -> str:
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'force_generic_extractor': True,
    }
    search_queries = [
        f"{song_name} {singer} official audio",
        f"{song_name} {singer}",
        f"{song_name} audio"
    ]
    for query in search_queries:
        try:
            logger.info(f"Searching YouTube with query: {query}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                result = ydl.extract_info(f"ytsearch3:{query}", download=False)
                if 'entries' in result and result['entries']:
                    for entry in result['entries'][:3]:
                        url = entry['url']
                        test_opts = {
                            'format': 'bestaudio/best',
                            'quiet': True,
                            'no_warnings': True,
                            'simulate': True,
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
        except Exception as e:
            logger.error(f"Error searching YouTube for {query}: {str(e)}")
            continue
    logger.warning(f"No valid URLs found for {song_name}")
    return None

# Function to download YouTube audio using yt-dlp (without FFmpeg)
async def download_youtube_audio(url: str, output_name: str) -> str:
    output_path = os.path.join(CONFIG['TEMP_DIR'], output_name)
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_path + '.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'retries': 3,
        'socket_timeout': 30,
        'extractaudio': True,
        'audioformat': 'm4a',  # Use YouTube's native format
        'prefer_ffmpeg': False,
        'keepvideo': False,
    }

    for attempt in range(3):
        try:
            logger.info(f"Download attempt {attempt + 1} for {url}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            
            # Check for the file (can be .m4a or other audio format)
            for ext in ['.m4a', '.webm', '.mp3']:
                audio_path = f"{output_path}{ext}"
                if os.path.exists(audio_path):
                    logger.info(f"Successfully downloaded to {audio_path}")
                    return audio_path
            
            raise Exception("Download completed but audio file not found")
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} failed: {str(e)}")
            if attempt == 2:
                raise Exception(f"Failed after 3 attempts: {str(e)}")
            time.sleep(2)
    
    return None

# Start command handler
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.message.from_user
    logger.info(f"Start command from {user.id} ({user.username})")
    
    keyboard = [[InlineKeyboardButton("English", callback_data='lang_en')]]
    await update.message.reply_text(
        "Hello! I'm your Singer Playlist Bot. Let's get started! Which language would you like to use? 😊",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return LANGUAGE

# Language selection handler
async def language_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    chat_id = query.message.chat_id
    lang = query.data.split('_')[1]
    user_data[chat_id] = {'lang': lang}
    
    await query.edit_message_text("Awesome! Now, tell me the name of a singer to create a playlist for you! 🌟")
    return SINGER

# Singer name handler
async def singer_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.message.chat_id
    singer = update.message.text.strip()
    user_data[chat_id]['singer'] = singer
    
    await update.message.reply_text(f"Searching for songs by {singer}... Please wait! ⏳")

    try:
        # Search for artist
        results = sp.search(q=f'artist:{singer}', type='artist', limit=1)
        if not results['artists']['items']:
            await update.message.reply_text(f"Sorry, I couldn't find {singer}. Try another name! 😊")
            return SINGER

        # Get top tracks
        artist_id = results['artists']['items'][0]['id']
        tracks = sp.artist_top_tracks(artist_id)
        song_list = [(track['name'], track['external_urls']['spotify']) for track in tracks['tracks'][:10]]
        
        # Find YouTube links
        downloadable_songs = []
        for song_name, _ in song_list:
            try:
                yt_url = await search_youtube(song_name, singer)
                if yt_url:
                    downloadable_songs.append((song_name, yt_url))
                else:
                    await update.message.reply_text(f"Couldn't find {song_name} on YouTube. Skipping...")
            except Exception as e:
                logger.error(f"Error searching for {song_name}: {str(e)}")
                continue

        if not downloadable_songs:
            await update.message.reply_text("No downloadable songs found. Try another artist! 😊")
            return SINGER

        user_data[chat_id]['songs'] = downloadable_songs
        
        # Show playlist
        playlist_text = f"🎵 Playlist for {singer}:\n\n"
        for idx, (song_name, _) in enumerate(downloadable_songs, 1):
            playlist_text += f"{idx}. {song_name}\n"
        
        playlist_text += "\nWhich song would you like to download? Type the number! Or, type a new singer's name to create another playlist! 🌟"
        await update.message.reply_text(playlist_text)
        return DOWNLOAD

    except requests.exceptions.RequestException as e:
        logger.error(f"Network error with Spotify: {str(e)}")
        await update.message.reply_text(f"Network issue while connecting to Spotify. Error: {str(e)}. Please check your internet and try again! 😊")
        return SINGER
    except Exception as e:
        logger.error(f"Error in singer_name: {str(e)}")
        await update.message.reply_text(f"Oops! Something went wrong. Error: {str(e)}. Let's try again! 😊")
        return SINGER

# Download song handler
async def download_song(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.message.chat_id
    user_input = update.message.text.strip()

    # Check if the user entered a new singer's name instead of a number
    try:
        song_idx = int(user_input) - 1
    except ValueError:
        # User entered a new singer's name, treat it as a new search
        logger.info(f"User entered a new singer name: {user_input} from chat_id: {chat_id}")
        user_data[chat_id]['singer'] = user_input
        await update.message.reply_text(f"Got it! I'm searching for songs by {user_input}... Please wait a moment! 😊")
        
        try:
            results = sp.search(q=f'artist:{user_input}', type='artist', limit=1)
            if not results['artists']['items']:
                await update.message.reply_text(f"Sorry, I couldn't find any artist named {user_input}. Try another name! 😊")
                return SINGER

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
                await update.message.reply_text(f"Sorry, I couldn't find any downloadable links for this singer. Try another name! 😊")
                return SINGER

            user_data[chat_id]['songs'] = downloadable_songs

            playlist_text = f"Here's the playlist for {user_input}:\n\n"
            for idx, (song_name, _) in enumerate(downloadable_songs, 1):
                playlist_text += f"{idx}. {song_name}\n"
            playlist_text += "\nWhich song would you like to download? Just type the number (e.g., 1)! Or, type a new singer's name to create another playlist! 🌟"
            await update.message.reply_text(playlist_text)
            return DOWNLOAD
        except Exception as e:
            logger.error(f"Error in singer search for {user_input}: {str(e)}")
            await update.message.reply_text(f"Oops! Something went wrong while searching for {user_input}. Error: {str(e)}. Let's try again! 😊")
            return SINGER

    # Proceed with downloading the selected song
    try:
        songs = user_data[chat_id]['songs']
        singer = user_data[chat_id]['singer']
        
        if song_idx < 0 or song_idx >= len(songs):
            await update.message.reply_text("Please choose a valid number from the playlist! 😊")
            return DOWNLOAD

        song_name, yt_url = songs[song_idx]
        await update.message.reply_text(f"⬇️ Downloading {song_name}... Please wait a moment! 😊")

        try:
            # Sanitize filename
            safe_name = "".join(c for c in song_name if c.isalnum() or c in " _-")
            audio_path = await download_youtube_audio(yt_url, f"{singer}_{safe_name}")
            
            if not audio_path:
                await update.message.reply_text("Sorry, I couldn't download this song. Let's try another one! 😊")
                return DOWNLOAD

            # Check file size (Telegram has a 50MB limit)
            file_size = os.path.getsize(audio_path) / (1024 * 1024)
            if file_size > CONFIG['MAX_FILE_SIZE_MB']:
                await update.message.reply_text(f"Sorry, the file {song_name} is {file_size:.2f}MB, which is larger than Telegram's 50MB limit. Please try another song! 😊")
                os.remove(audio_path)
            else:
                with open(audio_path, 'rb') as audio_file:
                    await update.message.reply_audio(audio=audio_file, title=song_name, performer=singer, timeout=60)  # Increased timeout
                await update.message.reply_text(f"{song_name} downloaded! Enjoy the music! 😊❤️")
            
            # Clean up
            try:
                os.remove(audio_path)
            except Exception as e:
                logger.warning(f"Failed to delete temporary file {audio_path}: {str(e)}")

        except Exception as e:
            logger.error(f"Download error for {song_name}: {str(e)}")
            await update.message.reply_text(f"Error while downloading {song_name}. Error: {str(e)}. Let's try another song! 😊")
            return DOWNLOAD

        # Show the playlist again
        playlist_text = f"🎵 Playlist for {singer}:\n\n"
        for idx, (song_name, _) in enumerate(songs, 1):
            playlist_text += f"{idx}. {song_name}\n"
        playlist_text += "\nWhich song would you like to download? Type the number! Or, type a new singer's name to create another playlist! 🌟"
        await update.message.reply_text(playlist_text)
        return DOWNLOAD

    except Exception as e:
        logger.error(f"Error in download_song: {str(e)}")
        await update.message.reply_text(f"Oops! Something went wrong while downloading. Error: {str(e)}. Let's try again! 😊")
        return DOWNLOAD

# Cancel command
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Operation cancelled. Type /start to begin again! 😊")
    return ConversationHandler.END

# Main function to run the bot
def main():
    try:
        logger.info("Starting bot...")
        
        application = Application.builder().token(CONFIG['TELEGRAM_TOKEN']).build()

        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', start)],
            states={
                LANGUAGE: [CallbackQueryHandler(language_selection, pattern='^lang_')],
                SINGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, singer_name)],
                DOWNLOAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, download_song)]
            },
            fallbacks=[CommandHandler('cancel', cancel)],
            per_message=False
        )

        application.add_handler(conv_handler)
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.error(f"Bot crashed: {str(e)}")
        raise

if __name__ == '__main__':
    main()