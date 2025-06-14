import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import asyncio
from datetime import datetime, timedelta, timezone
import os
import re
import signal
import sys
import psutil
from dotenv import load_dotenv
import tempfile
import atexit
import base64
from pathlib import Path

# --- Cookie Management ---
def get_cookies_content():
    """Get cookies content from multiple environment variables if needed."""
    print("\n=== Checking YouTube Cookies ===")
    
    # Try to get all cookie parts
    cookie_parts = []
    part_num = 1
    while True:
        env_var = f'YOUTUBE_COOKIES_B64_{part_num}'
        cookie_part = os.getenv(env_var)
        if not cookie_part:
            if part_num == 1:
                # Try the old single variable name for backward compatibility
                cookie_part = os.getenv('YOUTUBE_COOKIES_B64')
            if not cookie_part:
                break
        cookie_parts.append(cookie_part)
        part_num += 1
    
    if not cookie_parts:
        print("No cookie environment variables found")
        return None
    
    try:
        # Combine and decode all parts
        combined_b64 = ''.join(cookie_parts)
        cookies_content = base64.b64decode(combined_b64).decode('utf-8')
        
        # Validate cookie content
        if not cookies_content.strip():
            print("Cookie content is empty")
            return None
            
        # Check for required cookie fields - only check for essential ones
        required_fields = ['youtube.com', 'VISITOR_INFO1_LIVE']
        found_fields = []
        missing_fields = []
        
        # Check both youtube.com and www.youtube.com domains
        for field in required_fields:
            if field in cookies_content or f'www.{field}' in cookies_content:
                found_fields.append(field)
            else:
                missing_fields.append(field)
        
        print(f"Found cookie fields: {found_fields}")
        if missing_fields:
            print(f"Missing cookie fields: {missing_fields}")
            # Don't return None here, as some fields might be optional
        
        # Check if we have at least the basic required fields
        if 'youtube.com' not in cookies_content and 'www.youtube.com' not in cookies_content:
            print("No YouTube domain cookies found")
            return None
            
        print("Cookie validation successful")
        return cookies_content
        
    except Exception as e:
        print(f"Error decoding/validating cookies: {e}")
        print(f"Error type: {type(e)}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        return None

def create_temp_cookies_file():
    """Create a temporary cookies file from environment variables."""
    cookies_content = get_cookies_content()
    if not cookies_content:
        print("No valid cookies content to write to file")
        return None
        
    # Create a temporary file
    temp_file = tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.txt')
    try:
        # Write cookies content to the temporary file
        temp_file.write(cookies_content)
        temp_file.close()
        print(f"Successfully created temporary cookies file: {temp_file.name}")
        return temp_file.name
    except Exception as e:
        print(f"Error writing to temporary cookies file: {e}")
        print(f"Error type: {type(e)}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        if temp_file:
            temp_file.close()
        return None

def cleanup_temp_cookies_file(file_path):
    """Clean up the temporary cookies file."""
    if file_path and os.path.exists(file_path):
        try:
            os.unlink(file_path)
        except Exception as e:
            print(f"Error cleaning up temporary cookies file: {e}")

# Register cleanup function to run at exit
atexit.register(lambda: cleanup_temp_cookies_file(getattr(create_temp_cookies_file, 'last_file', None)))

# --- Bot Setup ---
# Initialize bot with required intents
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)

# Define yt-dlp options globally
ydl_opts = {
    'format': 'bestaudio/best',  # Prefer best audio quality
    'quiet': False,  # Enable logging
    'extract_flat': 'in_playlist',
    'default_search': 'ytsearch',
    'noplaylist': True,  # Don't extract playlists when searching
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }]
}

# Define FFmpeg options globally
ffmpeg_options = {
    'before_options': (
        '-reconnect 1 '  # Enable reconnection
        '-reconnect_streamed 1 '  # Enable stream reconnection
        '-reconnect_delay_max 5 '  # Max delay between reconnection attempts
        '-thread_queue_size 1024 '  # Reduced thread queue size
        '-analyzeduration 0 '  # Disable analysis duration limit
        '-probesize 32M '  # Increased probe size
        '-loglevel warning'  # Only show warnings and errors
    ),
    'options': (
        '-vn '  # Disable video
        '-acodec libopus '  # Use opus codec directly
        '-b:a 128k '  # Reduced bitrate for stability
        '-ar 48000 '  # Sample rate
        '-ac 2 '  # Stereo
        '-application voip '  # Optimize for voice
        '-packet_loss 10 '  # Handle packet loss
        '-frame_duration 20 '  # Frame duration
        '-compression_level 10 '  # Maximum compression
        '-vbr on '  # Variable bitrate
        '-cutoff 20000 '  # Frequency cutoff
        '-af "volume=1.0" '  # Volume normalization
        '-bufsize 96k'  # Reduced buffer size
    ),
    'executable': str(Path('ffmpeg/bin/ffmpeg.exe' if os.name == 'nt' else 'ffmpeg/bin/ffmpeg'))
}

# Add shutdown handler
@bot.event
async def on_shutdown():
    """Called when the bot is shutting down."""
    print("\n🛑 Shutting down bot...")
    # Disconnect from all voice channels
    for guild in bot.guilds:
        if guild.voice_client:
            try:
                await guild.voice_client.disconnect()
            except:
                pass
    # Clear all players
    players.clear()
    print("✅ Bot shutdown complete.")

def get_current_time():
    """Get current time in UTC."""
    return datetime.now(timezone.utc)

# --- State Management Class ---
class GuildPlayer:
    """A class to manage all music player state for a single guild."""
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue = []
        self.playback_history = []
        self.loop_mode = 0  # 0: off, 1: track, 2: queue
        self.current_track_url = None
        self.player_message = None
        self.current_track_info = None
        self.start_time = None
        self.last_update = None
        self.pause_time = None  # Track when the player was paused
        self.total_paused_time = 0  # Track total time spent paused
        self.is_paused = False
        self.last_position = 0  # Track the last known position
        self.position_update_time = None  # Track when we last updated the position

    def get_elapsed_time(self) -> float:
        """Calculate the actual elapsed time, accounting for pauses and voice client position."""
        if not self.start_time:
            return 0.0
        
        # If we have a voice client, use its position as the primary source
        voice_client = self.guild.voice_client
        if voice_client and voice_client.is_playing():
            # Get position from voice client
            position = voice_client.source.position if hasattr(voice_client.source, 'position') else 0
            if position > 0:
                self.last_position = position
                self.position_update_time = get_current_time()
                return position
        
        # Fallback to time-based calculation if no voice client position
        current_time = get_current_time()
        if self.is_paused and self.pause_time:
            # If paused, use the time when we paused
            elapsed = (self.pause_time - self.start_time).total_seconds() - self.total_paused_time
        else:
            # If playing, use current time
            elapsed = (current_time - self.start_time).total_seconds() - self.total_paused_time
        
        # If we have a last known position and it's recent, use that as a base
        if self.position_update_time and (current_time - self.position_update_time).total_seconds() < 5:
            elapsed = max(elapsed, self.last_position)
        
        return max(0.0, elapsed)

    def pause(self):
        """Handle pausing the player."""
        if not self.is_paused:
            self.pause_time = get_current_time()
            self.is_paused = True

    def resume(self):
        """Handle resuming the player."""
        if self.is_paused and self.pause_time:
            current_time = get_current_time()
            self.total_paused_time += (current_time - self.pause_time).total_seconds()
            self.pause_time = None
            self.is_paused = False

# This dictionary will hold all our GuildPlayer instances, one for each server.
players = {}

def get_player(guild: discord.Guild) -> GuildPlayer:
    """Gets the GuildPlayer instance for a guild, creating it if it doesn't exist."""
    if guild.id not in players:
        players[guild.id] = GuildPlayer(guild)
    return players[guild.id]

# --- UI Controls View ---
class MusicControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Persistent view

    async def handle_interaction(self, interaction: discord.Interaction, response: str, ephemeral: bool = True):
        """Helper method to safely handle interaction responses."""
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(response, ephemeral=ephemeral)
            else:
                # If we've already responded, use followup
                try:
                    await interaction.followup.send(response, ephemeral=ephemeral)
                except discord.errors.HTTPException as e:
                    if e.code == 40060:  # Interaction already acknowledged
                        # If followup also fails, try to send a new message
                        await interaction.channel.send(response)
                    else:
                        raise
        except discord.errors.HTTPException as e:
            if e.code == 40060:  # Interaction already acknowledged
                try:
                    await interaction.channel.send(response)
                except:
                    pass  # Ignore if all message attempts fail
            else:
                raise

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.blurple, custom_id="music_prev")
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(interaction.guild)
        if len(player.playback_history) > 1:
            # Add the current track to the front of the queue
            if player.current_track_url:
                player.queue.insert(0, player.current_track_url)
            # Pop current and previous track URLs from history
            player.playback_history.pop()
            prev_track = player.playback_history.pop()
            # Add the previous track to the front of the queue to be played next
            player.queue.insert(0, prev_track)
            
            # Skip to the previous track
            if interaction.guild.voice_client:
                interaction.guild.voice_client.stop()
                await self.handle_interaction(interaction, "⏮️ Playing previous track.")
            else:
                await self.handle_interaction(interaction, "❌ Not connected to voice channel.")
        else:
            await self.handle_interaction(interaction, "❌ No previous track in history.")

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.blurple, custom_id="music_playpause")
    async def play_pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(interaction.guild)
        voice_client = interaction.guild.voice_client
        if voice_client:
            if voice_client.is_paused():
                voice_client.resume()
                player.resume()  # Update player state
                await self.handle_interaction(interaction, "▶️ Resumed.")
            elif voice_client.is_playing():
                voice_client.pause()
                player.pause()  # Update player state
                await self.handle_interaction(interaction, "⏸️ Paused.")
            else:
                # If not playing but we have a queue, start playing
                if player.queue:
                    next_url = player.queue.pop(0)
                    await play_track(await commands.Context.from_interaction(interaction), next_url)
                    await self.handle_interaction(interaction, "▶️ Starting playback.")
                else:
                    await self.handle_interaction(interaction, "❌ Nothing in queue to play.")

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.blurple, custom_id="music_skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice_client = interaction.guild.voice_client
        if voice_client and voice_client.is_playing():
            voice_client.stop()  # This will trigger play_next
            await self.handle_interaction(interaction, "⏭️ Skipped.")
        elif voice_client and not voice_client.is_playing() and player.queue:
            # If not playing but we have a queue, start playing
            next_url = player.queue.pop(0)
            await play_track(await commands.Context.from_interaction(interaction), next_url)
            await self.handle_interaction(interaction, "▶️ Starting next track.")
        else:
            await self.handle_interaction(interaction, "❌ Nothing to skip.")

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.blurple, custom_id="music_loop")
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(interaction.guild)
        player.loop_mode = (player.loop_mode + 1) % 3
        
        loop_status_map = {0: ("Off", discord.ButtonStyle.blurple), 1: ("Track", discord.ButtonStyle.green), 2: ("Queue", discord.ButtonStyle.green)}
        status_text, style = loop_status_map[player.loop_mode]
        
        button.style = style
        try:
            await interaction.message.edit(view=self)  # Update the button color
        except discord.NotFound:
            pass  # Message might have been deleted
        except Exception as e:
            print(f"Error updating loop button: {e}")
        
        await self.handle_interaction(interaction, f"🔁 Loop mode set to **{status_text}**.")

    @discord.ui.button(emoji="🛑", style=discord.ButtonStyle.danger, custom_id="music_stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = get_player(interaction.guild)
        voice_client = interaction.guild.voice_client
        if voice_client:
            player.queue.clear()
            voice_client.stop()
            await voice_client.disconnect()
            if player.player_message:
                try:
                    await player.player_message.delete()
                except discord.NotFound:
                    pass
            players.pop(interaction.guild.id, None)  # Clean up the player instance
            await self.handle_interaction(interaction, "🛑 Playback stopped and queue cleared.")
        else:
            await self.handle_interaction(interaction, "❌ Not connected to a voice channel.")

# --- Helper Functions ---
def create_progress_bar(progress: float, duration: int) -> str:
    """Creates a visual progress bar for the track with improved visualization."""
    bar_length = 15  # Slightly shorter for cleaner look
    filled_length = int(bar_length * progress)
    
    # Use different characters for a more modern look
    bar = '━' * filled_length + '─' * (bar_length - filled_length)
    
    # Add a small dot to show current position
    if filled_length < bar_length:
        bar = bar[:filled_length] + '●' + bar[filled_length + 1:]
    else:
        bar = bar[:-1] + '●'
    
    return f"`{bar}`"

def format_time(seconds: float) -> str:
    """Formats seconds into MM:SS or HH:MM:SS format with leading zeros."""
    # Convert float to int for formatting
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"

async def create_player_embed(info: dict, requester: discord.Member, player: GuildPlayer) -> discord.Embed:
    """Creates an improved 'Now Playing' embed with a cleaner, more responsive design."""
    try:
        # Calculate progress
        duration = info.get('duration', 0)
        elapsed = player.get_elapsed_time()
        
        print(f"\n=== Creating Player Embed ===")
        print(f"Start time: {player.start_time}")
        print(f"Current time: {get_current_time()}")
        print(f"Pause time: {player.pause_time}")
        print(f"Total paused time: {player.total_paused_time}")
        print(f"Elapsed time: {elapsed}")
        print(f"Duration: {duration}")
        
        progress = min(1.0, elapsed / duration) if duration > 0 else 0.0
        
        # Create embed with a more modern color
        embed = discord.Embed(
            title="🎵 Now Playing",
            color=discord.Color.from_rgb(88, 101, 242)
        )
        
        # Add thumbnail with a slight border effect
        if info.get('thumbnail'):
            embed.set_thumbnail(url=info['thumbnail'])
        
        # Format the title and URL more cleanly
        title = info.get('title', 'Unknown Title')
        url = info.get('webpage_url', '#')
        uploader = info.get('uploader', 'Unknown Artist')
        
        # Create a cleaner description with uploader info
        embed.description = f"**[{title}]({url})**\n👤 {uploader}"
        
        # Add progress bar with time
        progress_bar = create_progress_bar(progress, duration)
        elapsed_str = format_time(elapsed)
        duration_str = format_time(duration)
        
        # Create a more compact progress display
        progress_text = f"{elapsed_str} {progress_bar} {duration_str}"
        embed.add_field(
            name="\u200b",
            value=progress_text,
            inline=False
        )
        
        # Add metadata in a more compact way
        metadata = []
        if info.get('view_count'):
            views = f"{int(info['view_count']):,}"
            metadata.append(f"👁️ {views} views")
        if info.get('like_count'):
            likes = f"{int(info['like_count']):,}"
            metadata.append(f"❤️ {likes} likes")
        
        if metadata:
            embed.add_field(
                name="\u200b",
                value=" • ".join(metadata),
                inline=False
            )
        
        # Add requester info in a cleaner way
        embed.add_field(
            name="\u200b",
            value=f"🎵 Requested by {requester.mention}",
            inline=False
        )
        
        # Add status footer with improved formatting
        loop_status = {0: "Off", 1: "🔂 Track", 2: "🔁 Queue"}.get(player.loop_mode, "Off")
        queue_size = len(player.queue)
        queue_text = f"{queue_size} {'track' if queue_size == 1 else 'tracks'}"
        
        # Create a more informative footer
        footer_text = f"{loop_status} • Queue: {queue_text}"
        if player.loop_mode != 0:
            footer_text = f"**{footer_text}**"
        
        embed.set_footer(text=footer_text)
        
        return embed
    except Exception as e:
        print(f"Error creating player embed: {str(e)}")
        print(f"Error type: {type(e)}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        # Return a basic embed if there's an error
        embed = discord.Embed(
            title="🎵 Now Playing",
            description=f"**{info.get('title', 'Unknown Title')}**",
            color=discord.Color.from_rgb(88, 101, 242)
        )
        return embed

# --- Core Playback Logic ---
async def play_next(ctx: commands.Context):
    """The main playback loop that plays the next song in the queue."""
    player = get_player(ctx.guild)
    
    try:
        print("\n=== Starting play_next ===")
        print(f"Current track URL: {player.current_track_url}")
        print(f"Queue size: {len(player.queue)}")
        print(f"Loop mode: {player.loop_mode}")
        
        # Handle looping for the track that just finished
        if player.current_track_url:
            if player.loop_mode == 1:  # Loop track
                print("Looping current track")
                player.queue.insert(0, player.current_track_url)
            elif player.loop_mode == 2:  # Loop queue
                print("Looping queue - adding current track to end")
                player.queue.append(player.current_track_url)

        # Clean up the current track info
        player.current_track_url = None
        player.current_track_info = None
        
        # If the queue is not empty, play the next track
        if player.queue:
            next_url = player.queue.pop(0)
            print(f"Playing next track: {next_url}")
            await play_track(ctx, next_url)  # Don't pass msg_handler here
        else:
            print("Queue is empty, cleaning up")
            # Queue is empty, clean up
            if player.player_message:
                try:
                    await player.player_message.edit(content="✅ Queue finished. Add more songs!", embed=None, view=None)
                except Exception as e:
                    print(f"Error updating player message: {e}")
            
            # Optional: Disconnect after a period of inactivity
            await asyncio.sleep(180)  # Wait 3 minutes
            if ctx.guild.voice_client and not ctx.guild.voice_client.is_playing() and not player.queue:
                print("Disconnecting due to inactivity")
                await ctx.guild.voice_client.disconnect()
                players.pop(ctx.guild.id, None)

    except Exception as e:
        print(f"\n=== CRITICAL ERROR in play_next ===")
        print(f"Error: {str(e)}")
        print(f"Error type: {type(e)}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        
        # Try to send error message
        try:
            if isinstance(ctx, commands.Context):
                await ctx.channel.send(f"❌ A critical playback error occurred: {str(e)}")
            else:
                await ctx.followup.send(f"❌ A critical playback error occurred: {str(e)}", ephemeral=True)
        except Exception as send_error:
            print(f"Failed to send error message: {send_error}")
            print(f"Send error type: {type(send_error)}")
            print(f"Send error traceback: {traceback.format_exc()}")

async def play_track(ctx: commands.Context, url: str, msg_handler=None):
    """Plays a single track from a URL."""
    player = get_player(ctx.guild)
    voice_client = ctx.guild.voice_client
    temp_cookies_file = None
    progress_task = None
    cookie_warning_shown = False

    try:
        print(f"\n=== Starting play_track ===")
        print(f"URL: {url}")
        print(f"Voice client exists: {voice_client is not None}")
        print(f"Voice client playing: {voice_client.is_playing() if voice_client else False}")
        
        # Clean up any existing player message
        if player.player_message:
            try:
                print("Cleaning up existing player message")
                await player.player_message.delete()
            except discord.NotFound:
                print("Old player message was already deleted")
            except Exception as e:
                print(f"Error deleting old player message: {e}")
            player.player_message = None
        
        # Stop any existing playback
        if voice_client and voice_client.is_playing():
            print("Stopping existing playback")
            voice_client.stop()
            # Wait a moment for the stop to take effect
            await asyncio.sleep(0.5)
        
        # Set current track info before anything else
        player.current_track_url = url
        player.current_track_info = None  # Will be set after extraction
        player.start_time = get_current_time()
        player.last_update = None
        player.pause_time = None
        player.total_paused_time = 0
        player.is_paused = False
        player.last_position = 0
        player.position_update_time = None
        
        print(f"Player state reset - start time: {player.start_time}")
        print(f"Current track URL set to: {player.current_track_url}")
        
        # Connect to voice channel if not already connected
        if not voice_client:
            if not ctx.author.voice:
                error_msg = "❗ You must be in a voice channel to play music."
                print(f"Sending error: {error_msg}")
                if isinstance(ctx, commands.Context):
                    await ctx.channel.send(error_msg)
                else:
                    await ctx.followup.send(error_msg, ephemeral=True)
                return
            channel = ctx.author.voice.channel
            print(f"Connecting to voice channel: {channel.name}")
            
            try:
                # Connect with optimized settings
                voice_client = await channel.connect(
                    timeout=60,  # Increased timeout
                    reconnect=True  # Enable reconnection
                )
                
                # Configure voice client for better stability
                voice_client.recv_audio = False  # Disable audio receiving
                voice_client.send_audio = True   # Enable audio sending
                
                # Set voice state
                await ctx.guild.change_voice_state(
                    channel=voice_client.channel,
                    self_deaf=True,  # Deafen the bot
                    self_mute=False  # Don't mute the bot
                )
                print("Connected to voice channel with optimized settings")
            except Exception as e:
                print(f"Error connecting to voice channel: {e}")
                raise ValueError(f"Failed to connect to voice channel: {e}")

        # Create a copy of ydl_opts for this specific request
        current_ydl_opts = ydl_opts.copy()
        
        # Add cookies if available
        temp_cookies_file = create_temp_cookies_file()
        if temp_cookies_file:
            current_ydl_opts['cookiefile'] = temp_cookies_file
            print("Added cookies to yt-dlp options")

        # Create a copy of ffmpeg options for this specific request
        current_ffmpeg_options = ffmpeg_options.copy()

        async def try_extract_without_cookies():
            """Try to extract video info without cookies first."""
            print("\n=== Attempting extraction without cookies ===")
            try:
                # Use a copy without cookies
                opts = current_ydl_opts.copy()
                if 'cookiefile' in opts:
                    del opts['cookiefile']
                
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = await bot.loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
                
                # Ensure we get the best audio format
                if isinstance(info, dict) and 'formats' in info:
                    audio_formats = [f for f in info['formats'] 
                                   if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
                    if audio_formats:
                        # Sort by audio quality
                        audio_formats.sort(key=lambda x: (
                            x.get('abr', 0) or 0,  # Audio bitrate
                            x.get('asr', 0) or 0,  # Audio sample rate
                            x.get('filesize', 0) or 0  # File size as fallback
                        ), reverse=True)
                        best_format = audio_formats[0]
                        info['url'] = best_format['url']
                        print(f"Selected audio format: {best_format.get('format_id')} "
                              f"({best_format.get('abr', 'N/A')}kbps, "
                              f"{best_format.get('asr', 'N/A')}Hz)")
                
                return info
            except Exception as e:
                print(f"Extraction without cookies failed: {str(e)}")
                # Check if the error suggests cookies might help
                error_str = str(e).lower()
                if any(keyword in error_str for keyword in [
                    'sign in to confirm your age',
                    'age restricted',
                    'private video',
                    'video unavailable'
                ]):
                    print("Error suggests cookies might be needed")
                    return None
                raise  # Re-raise if it's not a cookie-related error

        async def try_extract_with_cookies():
            """Try to extract video info with cookies if available."""
            print("\n=== Attempting extraction with cookies ===")
            if not temp_cookies_file:
                print("No cookies available")
                return None

            try:
                with yt_dlp.YoutubeDL(current_ydl_opts) as ydl:
                    return await bot.loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
            except Exception as e:
                print(f"Extraction with cookies failed: {str(e)}")
                return None

        # Try extraction without cookies first
        try:
            info = await try_extract_without_cookies()
        except Exception as e:
            print(f"Initial extraction failed with error: {str(e)}")
            info = None

        # If that fails, check if it's a cookie-required error
        if not info:
            print("Initial extraction failed, checking if cookies are needed")
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    # Just try to get basic info to check if cookies are needed
                    basic_info = await bot.loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False, process=False))
                    if isinstance(basic_info, dict):
                        needs_cookies = False
                        reason = []

                        # Only check for essential cases for a music bot
                        if basic_info.get('age_limit', 0) > 0:
                            needs_cookies = True
                            reason.append("age-restricted")
                        if 'private' in str(basic_info).lower():
                            needs_cookies = True
                            reason.append("private")

                        if needs_cookies:
                            print(f"Video requires cookies: {', '.join(reason)}")
                            info = await try_extract_with_cookies()
                            if not info:
                                raise ValueError(f"Could not access {', '.join(reason)} content. Cookies may be invalid or expired.")
                        else:
                            print("Video is not restricted, but extraction failed")
                            raise ValueError("Could not extract video information")
            except Exception as e:
                print(f"Error checking video status: {str(e)}")
                raise ValueError(f"Error processing video: {str(e)}")

        if not info:
            raise ValueError("Could not extract video information")

        # Process the video info
        print("\n=== Video Info Available ===")
        print(f"Title: {info.get('title', 'Not found')}")
        print(f"Duration: {info.get('duration', 'Not found')}")
        print(f"Uploader: {info.get('uploader', 'Not found')}")
        print(f"View count: {info.get('view_count', 'Not found')}")
        print(f"Like count: {info.get('like_count', 'Not found')}")

        # Ensure we have the webpage_url
        if 'webpage_url' not in info and 'url' in info:
            if 'youtube.com' in url or 'youtu.be' in url:
                info['webpage_url'] = url
            else:
                try:
                    if 'original_url' in info:
                        info['webpage_url'] = info['original_url']
                    elif 'id' in info:
                        info['webpage_url'] = f"https://www.youtube.com/watch?v={info['id']}"
                except Exception as e:
                    print(f"Error constructing webpage_url: {e}")
                    info['webpage_url'] = url

        # Get the audio stream URL
        if 'url' not in info:
            print("No direct URL found, extracting format...")
            formats = info.get('formats', [])
            audio_formats = [f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
            if audio_formats:
                best_audio = audio_formats[-1]
                info['url'] = best_audio['url']
            else:
                raise ValueError("No suitable audio format found")

        print(f"Using webpage URL: {info.get('webpage_url', 'Not found')}")
        print(f"Using audio URL: {info.get('url', 'Not found')[:100]}...")

        # After successful extraction, update the track info
        player.current_track_info = info
        if url not in player.playback_history:
            player.playback_history.append(url)
        
        print(f"Track info set - Title: {info.get('title')}, Duration: {info.get('duration')}")

        try:
            print("Creating audio source...")
            # Create audio source with optimized settings
            source = discord.FFmpegOpusAudio(
                info['url'],
                **current_ffmpeg_options
            )
            
            # Configure source for better stability
            source.read_size = 1920  # Reduced read size
            source.packet_size = 960  # Standard packet size
            
            print("Audio source created successfully")
            
            # Start playback
            print("Starting playback...")
            voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(
                handle_playback_complete(ctx, e), bot.loop
            ))
            print("Playback started successfully")
            
            # Start progress updates after confirming playback has started
            if voice_client.is_playing():
                print("Starting progress update task")
                # Cancel any existing progress task
                if hasattr(player, 'progress_task') and player.progress_task:
                    try:
                        player.progress_task.cancel()
                    except:
                        pass
                player.progress_task = bot.loop.create_task(update_progress(ctx, player))
                print("Progress update task started")
            else:
                print("Warning: Voice client not playing after source creation")
            
        except Exception as e:
            print(f"Error creating/starting audio source: {str(e)}")
            print(f"Error type: {type(e)}")
            import traceback
            print(f"Traceback: {traceback.format_exc()}")
            # Clear current track info on error
            player.current_track_url = None
            player.current_track_info = None
            raise ValueError(f"Failed to start playback: {str(e)}")

        # Create player message
        try:
            print("Creating player message")
            embed = await create_player_embed(info, ctx.author, player)
            view = MusicControls()
            player.player_message = await ctx.channel.send(embed=embed, view=view)
            print("Player message sent successfully")
        except Exception as e:
            print(f"Error creating player message: {e}")
            # Don't raise here, just log the error
            # The playback will continue even if the message fails

    except Exception as e:
        print(f"\n=== Play Command Error ===")
        print(f"Error: {str(e)}")
        print(f"Error type: {type(e)}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        
        # Add debug info to error message if msg_handler is available
        if msg_handler:
            debug_info = msg_handler.get_debug_info()
            print("\n=== Message Handler Debug Info ===")
            print(debug_info)
        
        # Ensure we have a non-empty error message
        error_msg = str(e) if str(e).strip() else f"Unknown error occurred (type: {type(e).__name__})"
        try:
            if msg_handler:
                await msg_handler.send(f"❌ Error processing your request: {error_msg}")
            else:
                # Fallback to direct message send if no msg_handler
                if isinstance(ctx, commands.Context):
                    await ctx.channel.send(f"❌ Error processing your request: {error_msg}")
                else:
                    await ctx.followup.send(f"❌ Error processing your request: {error_msg}", ephemeral=True)
        except Exception as send_error:
            print(f"Failed to send error message: {send_error}")
            print(f"Send error type: {type(send_error)}")
            print(f"Send error traceback: {traceback.format_exc()}")
            if msg_handler:
                print("\n=== Final Message Handler State ===")
                print(msg_handler.get_debug_info())

    finally:
        # Clean up temporary cookies file
        if temp_cookies_file:
            cleanup_temp_cookies_file(temp_cookies_file)

async def update_progress(ctx: commands.Context, player: GuildPlayer):
    """Updates the progress bar every 5 seconds."""
    update_count = 0
    last_error_time = None
    error_count = 0
    last_position = 0
    stuck_count = 0
    
    print("\n=== Starting Progress Update Task ===")
    print(f"Current track URL: {player.current_track_url}")
    print(f"Current track info: {player.current_track_info.get('title') if player.current_track_info else 'None'}")
    print(f"Voice client exists: {ctx.guild.voice_client is not None}")
    print(f"Voice client playing: {ctx.guild.voice_client.is_playing() if ctx.guild.voice_client else False}")
    
    while True:
        try:
            # Check if we should continue updating
            if not player.current_track_url:
                print("Stopping progress updates - no current track URL")
                break
                
            if not ctx.guild.voice_client:
                print("Stopping progress updates - no voice client")
                break
                
            if not ctx.guild.voice_client.is_playing():
                print("Stopping progress updates - not playing")
                break
                
            current_time = get_current_time()
            current_position = player.get_elapsed_time()
            
            # Log position updates periodically
            if update_count % 10 == 0:
                print(f"\nProgress Update #{update_count}")
                print(f"Current position: {format_time(current_position)}")
                print(f"Track duration: {format_time(player.current_track_info.get('duration', 0))}")
                print(f"Voice client playing: {ctx.guild.voice_client.is_playing()}")
                print(f"Voice client paused: {ctx.guild.voice_client.is_paused()}")
                if hasattr(ctx.guild.voice_client.source, 'position'):
                    print(f"Voice client position: {ctx.guild.voice_client.source.position:.1f}s")
            
            # Check if progress is stuck
            if abs(current_position - last_position) < 0.1:
                stuck_count += 1
                if stuck_count >= 3:  # If stuck for 3 updates (15 seconds)
                    print(f"Progress appears stuck at {current_position:.1f}s")
                    # Try to force a position update
                    if hasattr(ctx.guild.voice_client.source, 'position'):
                        current_position = ctx.guild.voice_client.source.position
                        player.last_position = current_position
                        player.position_update_time = current_time
                        print(f"Updated position from voice client: {current_position:.1f}s")
            else:
                stuck_count = 0
                last_position = current_position
            
            # Update the player message
            if player.player_message:
                try:
                    # Check if the message still exists
                    try:
                        await player.player_message.fetch()
                    except discord.NotFound:
                        print("Player message was deleted, creating new one")
                        embed = await create_player_embed(
                            player.current_track_info,
                            ctx.author,
                            player
                        )
                        # Use channel.send instead of interaction response
                        player.player_message = await ctx.channel.send(embed=embed, view=MusicControls())
                        continue
                    
                    # Update the existing message
                    embed = await create_player_embed(
                        player.current_track_info, 
                        ctx.author, 
                        player
                    )
                    try:
                        await player.player_message.edit(embed=embed)
                        update_count += 1
                    except discord.NotFound:
                        print("Message was deleted during update, creating new one")
                        player.player_message = await ctx.channel.send(embed=embed, view=MusicControls())
                    except discord.Forbidden:
                        print("No permission to edit message, skipping update")
                    except Exception as e:
                        print(f"Error updating message: {str(e)}")
                        error_count += 1
                        if error_count >= 3:
                            print("Too many errors, stopping progress updates")
                            break
                    
                except Exception as e:
                    print(f"Error in message update loop: {str(e)}")
                    error_count += 1
                    if error_count >= 3:
                        print("Too many errors, stopping progress updates")
                        break
            
            # Wait for next update
            await asyncio.sleep(5)
            
        except asyncio.CancelledError:
            print("Progress update task was cancelled")
            break
        except Exception as e:
            print(f"Unexpected error in progress update: {str(e)}")
            print(f"Error type: {type(e)}")
            import traceback
            print(f"Traceback: {traceback.format_exc()}")
            await asyncio.sleep(5)

    print("\n=== Progress Update Task Ended ===")
    print(f"Final update count: {update_count}")
    print(f"Final position: {format_time(last_position)}")
    if not player.current_track_url:
        print("Reason: No current track URL")
    elif not ctx.guild.voice_client:
        print("Reason: No voice client")
    elif not ctx.guild.voice_client.is_playing():
        print("Reason: Not playing")
    else:
        print("Reason: Unknown")

# --- Bot Commands ---
class MessageHandler:
    """Helper class to handle message state and sending."""
    def __init__(self, interaction: discord.Interaction):
        self.interaction = interaction
        self.message = None
        self.initialized = False
        self.last_error = None
        self.message_history = []
        self.last_send_attempt = None
        self.last_send_error = None
        self.thinking_message = None  # Track the thinking message

    def _log_message(self, action: str, status: str, details: str = ""):
        """Log message handling actions for debugging."""
        log_entry = f"[MessageHandler] {action}: {status} {details}".strip()
        self.message_history.append(log_entry)
        print(log_entry)

    async def initialize(self):
        """Initialize the message handler, either with defer or new message."""
        try:
            self._log_message("Initialize", "Attempting to defer response")
            await self.interaction.response.defer(ephemeral=False)
            self.initialized = True
            self.thinking_message = self.interaction.original_response()  # Store the thinking message
            self._log_message("Initialize", "Success", "Response deferred")
        except discord.NotFound as e:
            self._log_message("Initialize", "Failed", f"Interaction expired: {str(e)}")
            self.message = await self.interaction.channel.send("🔍 Searching for your song...")
            self.initialized = True
            self._log_message("Initialize", "Fallback", "Sent new message")
        except Exception as e:
            self.last_error = e
            self._log_message("Initialize", "Error", f"Unexpected error: {str(e)}")
            self.message = await self.interaction.channel.send("🔍 Searching for your song...")
            self.initialized = True
            self._log_message("Initialize", "Recovery", "Sent new message after error")

    async def send(self, content: str, ephemeral: bool = False):
        """Send or update a message with detailed error tracking."""
        try:
            self.last_send_attempt = content
            if not content or not content.strip():
                self._log_message("Send", "Error", "Empty content provided")
                content = "An error occurred, but no details were provided."
            
            # First try to update the thinking message if it exists
            if self.thinking_message:
                try:
                    self._log_message("Send", "Updating", f"Thinking message with content: {content[:50]}...")
                    await self.thinking_message.edit(content=content)
                    self.message = self.thinking_message  # Update our message reference
                    self._log_message("Send", "Success", "Thinking message updated")
                    return
                except discord.NotFound:
                    self._log_message("Send", "Failed", "Thinking message not found")
                    self.thinking_message = None
                except Exception as e:
                    self._log_message("Send", "Error", f"Failed to update thinking message: {str(e)}")
                    self.thinking_message = None
            
            # If we have an existing message, try to update it
            if self.message:
                try:
                    self._log_message("Send", "Updating", f"Existing message with content: {content[:50]}...")
                    await self.message.edit(content=content)
                    self._log_message("Send", "Success", "Message updated")
                except discord.NotFound:
                    self._log_message("Send", "Failed", "Message not found, creating new one")
                    self.message = await self.interaction.channel.send(content)
                    self._log_message("Send", "Success", "New message created")
            # If we're initialized but have no message, try followup
            elif self.initialized:
                try:
                    self._log_message("Send", "Attempting", f"Followup send with content: {content[:50]}...")
                    await self.interaction.followup.send(content, ephemeral=ephemeral)
                    self._log_message("Send", "Success", "Followup sent")
                except discord.NotFound as e:
                    self._log_message("Send", "Failed", f"Followup expired: {str(e)}")
                    self.message = await self.interaction.channel.send(content)
                    self._log_message("Send", "Fallback", "Sent new message")
                except Exception as e:
                    self.last_send_error = e
                    self._log_message("Send", "Error", f"Followup error: {str(e)}")
                    self.message = await self.interaction.channel.send(content)
                    self._log_message("Send", "Recovery", "Sent new message after error")
            # If we're not initialized, send a new message
            else:
                self._log_message("Send", "Initial", f"First message with content: {content[:50]}...")
                self.message = await self.interaction.channel.send(content)
                self.initialized = True
                self._log_message("Send", "Success", "First message sent")
        except Exception as e:
            self.last_send_error = e
            self._log_message("Send", "Error", f"Unexpected error: {str(e)}")
            if not self.message:
                try:
                    self.message = await self.interaction.channel.send(content)
                    self.initialized = True
                    self._log_message("Send", "Recovery", "Sent new message after error")
                except Exception as send_error:
                    self._log_message("Send", "Critical", f"Failed to send message: {str(send_error)}")
                    print(f"CRITICAL: Failed to send message after all attempts: {str(send_error)}")
                    print(f"Original error: {str(e)}")
                    print(f"Message history: {self.message_history}")

    def get_debug_info(self) -> str:
        """Get debug information about the message handler's state."""
        return (
            f"MessageHandler State:\n"
            f"Initialized: {self.initialized}\n"
            f"Has Message: {self.message is not None}\n"
            f"Has Thinking Message: {self.thinking_message is not None}\n"
            f"Last Error: {str(self.last_error) if self.last_error else 'None'}\n"
            f"Last Send Attempt: {self.last_send_attempt}\n"
            f"Last Send Error: {str(self.last_send_error) if self.last_send_error else 'None'}\n"
            f"Message History:\n" + "\n".join(self.message_history)
        )

@bot.tree.command(name="play", description="Play a song or playlist from YouTube")
@app_commands.describe(query="A song name or URL (YouTube, Spotify, SoundCloud, etc.)")
async def play_command(interaction: discord.Interaction, query: str):
    # Create message handler
    msg_handler = MessageHandler(interaction)
    
    try:
        print("\n=== Starting Play Command ===")
        print(f"Query: {query}")
        print(f"User: {interaction.user}")
        print(f"Channel: {interaction.channel}")
        
        # Initialize message handler
        await msg_handler.initialize()
        
        if not interaction.user.voice:
            await msg_handler.send("❗ You must be in a voice channel first!")
            return

        player = get_player(interaction.guild)
        ctx = await commands.Context.from_interaction(interaction)
        print(f"Got player for guild {interaction.guild.id}")

        # Create temporary cookies file
        temp_cookies_file = create_temp_cookies_file()
        create_temp_cookies_file.last_file = temp_cookies_file  # Store for cleanup

        # Enhanced yt-dlp options for better search results
        ydl_opts = {
            'format': 'bestaudio/best',  # Prefer best audio quality
            'quiet': False,  # Enable logging
            'extract_flat': 'in_playlist',
            'default_search': 'ytsearch',
            'noplaylist': True,  # Don't extract playlists when searching
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        }
        
        # Add cookies file if available
        if temp_cookies_file:
            ydl_opts['cookiefile'] = temp_cookies_file

        async def try_extract_info(query: str, is_search: bool = False) -> dict:
            """Helper function to try extracting video info with better error handling."""
            try:
                print(f"\n=== Starting search for: {query} ===")
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    print("Created yt-dlp instance")
                    try:
                        info = await bot.loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False))
                        print(f"Raw search results type: {type(info)}")
                        print(f"Raw search results keys: {info.keys() if isinstance(info, dict) else 'Not a dict'}")
                    except Exception as e:
                        print(f"Error during yt-dlp extraction: {str(e)}")
                        print(f"Error type: {type(e)}")
                        import traceback
                        print(f"Traceback: {traceback.format_exc()}")
                        raise
                    
                    # Validate the info dictionary
                    if not info:
                        print("No info returned from yt-dlp")
                        raise ValueError("No video information found")
                    
                    # For search results, validate entries
                    if 'entries' in info:
                        print(f"Found {len(info['entries'])} entries in search results")
                        if not info['entries']:
                            print("Empty entries list")
                            raise ValueError("No search results found")
                        
                        # Filter and validate entries with detailed debug info
                        valid_entries = []
                        for i, entry in enumerate(info['entries']):
                            print(f"\nProcessing entry {i + 1}:")
                            print(f"Entry type: {type(entry)}")
                            print(f"Entry keys: {entry.keys() if isinstance(entry, dict) else 'Not a dict'}")
                            print(f"Title: {entry.get('title', 'NO TITLE')}")
                            print(f"Views: {entry.get('view_count', 'NO VIEWS')}")
                            print(f"Duration: {entry.get('duration', 'NO DURATION')}")
                            print(f"URL: {entry.get('url', 'NO URL')}")
                            
                            if entry and isinstance(entry, dict):
                                # For search results, use 'url' instead of 'webpage_url'
                                if 'url' in entry and 'title' in entry:
                                    # Add webpage_url field for consistency
                                    entry['webpage_url'] = entry['url']
                                    valid_entries.append(entry)
                                    print("✓ Entry is valid")
                                else:
                                    print("✗ Entry filtered out - missing required fields")
                                    print(f"Missing fields: {[k for k in ['url', 'title'] if k not in entry]}")
                            else:
                                print(f"✗ Entry filtered out - invalid type: {type(entry)}")
                        
                        print(f"\nFound {len(valid_entries)} valid entries after filtering")
                        if not valid_entries:
                            print("No valid entries found after filtering")
                            raise ValueError("No valid search results found")
                        info['entries'] = valid_entries
                    # For single videos, validate required fields
                    elif not all(key in info for key in ['url', 'title']):
                        print(f"Single video missing required fields: {info}")
                        raise ValueError("Incomplete video information")
                    else:
                        # Add webpage_url field for consistency
                        info['webpage_url'] = info['url']
                    
                    return info
            except Exception as e:
                print(f"Error in try_extract_info: {str(e)}")
                print(f"Error type: {type(e)}")
                import traceback
                print(f"Traceback: {traceback.format_exc()}")
                raise ValueError(f"Error extracting video info: {str(e)}")

        # Try different search strategies with debug logging
        info = None
        search_strategies = [
            query,  # Try original query first
            f"{query} music",  # Try with music
            f"{query} audio",  # Try with audio
            f"{query} official",  # Try with official
            f"{query} lyrics"  # Try with lyrics as last resort
        ]
        
        # Update the search strategies to use our new message handler
        for search_query in search_strategies:
            try:
                print(f"\n=== Trying search strategy: {search_query} ===")
                info = await try_extract_info(search_query, is_search=True)
                if info and ('entries' in info and info['entries'] or 'webpage_url' in info):
                    print(f"✓ Found valid result with strategy: {search_query}")
                    break
            except ValueError as e:
                print(f"✗ Strategy {search_query} failed: {str(e)}")
                if search_query == search_strategies[-1]:
                    await msg_handler.send(f"❌ {str(e)}")
                    return
                continue

        print("\n=== Processing search results ===")
        if 'entries' in info:
            print("Processing search results as entries")
            entries = info['entries']
            entries.sort(key=lambda x: (
                x.get('view_count', 0),
                x.get('like_count', 0),
                x.get('duration', 0)
            ), reverse=True)
            
            best_entry = entries[0]
            print(f"Best entry: {best_entry['title']}")
            print(f"URL: {best_entry['webpage_url']}")
            print(f"Views: {best_entry.get('view_count', 0)}")
            
            # Only add to queue if not already playing
            if not interaction.guild.voice_client or not interaction.guild.voice_client.is_playing():
                print("No active playback, starting play_track")
                await play_track(ctx, best_entry['webpage_url'], msg_handler)
            else:
                print("Already playing, adding to queue")
                player.queue.append(best_entry['webpage_url'])
                await msg_handler.send(
                    f"🎵 Added **[{best_entry['title']}]({best_entry['webpage_url']})** to the queue.\n"
                    f"👁️ {int(best_entry.get('view_count', 0)):,} views • "
                    f"⏱️ {format_time(best_entry.get('duration', 0))}"
                )
        else:
            print("Processing single video result")
            print(f"Title: {info['title']}")
            print(f"URL: {info['webpage_url']}")
            print(f"Views: {info.get('view_count', 0)}")
            
            # Only add to queue if not already playing
            if not interaction.guild.voice_client or not interaction.guild.voice_client.is_playing():
                print("No active playback, starting play_track")
                await play_track(ctx, info['webpage_url'], msg_handler)
            else:
                print("Already playing, adding to queue")
                player.queue.append(info['webpage_url'])
                await msg_handler.send(
                    f"🎵 Added **[{info['title']}]({info['webpage_url']})** to the queue.\n"
                    f"👁️ {int(info.get('view_count', 0)):,} views • "
                    f"⏱️ {format_time(info.get('duration', 0))}"
                )

    except Exception as e:
        print(f"\n=== Play Command Error ===")
        print(f"Error: {str(e)}")
        print(f"Error type: {type(e)}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        
        # Add debug info to error message if msg_handler is available
        if msg_handler:
            debug_info = msg_handler.get_debug_info()
            print("\n=== Message Handler Debug Info ===")
            print(debug_info)
        
        # Ensure we have a non-empty error message
        error_msg = str(e) if str(e).strip() else f"Unknown error occurred (type: {type(e).__name__})"
        try:
            if msg_handler:
                await msg_handler.send(f"❌ Error processing your request: {error_msg}")
            else:
                # Fallback to direct message send if no msg_handler
                if isinstance(ctx, commands.Context):
                    await ctx.channel.send(f"❌ Error processing your request: {error_msg}")
                else:
                    await ctx.followup.send(f"❌ Error processing your request: {error_msg}", ephemeral=True)
        except Exception as send_error:
            print(f"Failed to send error message: {send_error}")
            print(f"Send error type: {type(send_error)}")
            print(f"Send error traceback: {traceback.format_exc()}")
            if msg_handler:
                print("\n=== Final Message Handler State ===")
                print(msg_handler.get_debug_info())

    finally:
        # Clean up temporary cookies file
        if temp_cookies_file:
            cleanup_temp_cookies_file(temp_cookies_file)

async def handle_playback_complete(ctx, error):
    """Handle playback completion or errors."""
    if error:
        print(f"\n=== Playback Error ===")
        print(f"Error: {error}")
        print(f"Error type: {type(error)}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        
        # Try to reconnect if it's a connection error
        if isinstance(error, discord.errors.ConnectionClosed):
            try:
                if ctx.guild.voice_client:
                    await ctx.guild.voice_client.disconnect(force=True)
                await ctx.guild.voice_client.connect(reconnect=True)
                print("Successfully reconnected to voice channel")
            except Exception as e:
                print(f"Failed to reconnect: {e}")
    
    print("Calling play_next from handle_playback_complete")
    await play_next(ctx)

# --- Bot Events ---
@bot.event
async def on_ready():
    """Called when the bot is ready and connected."""
    print(f"✅ Bot ready as {bot.user}")
    bot.add_view(MusicControls())  # Now valid with custom_ids
    await bot.tree.sync()
    print("🔁 Commands synced")

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Handles voice state changes, like the bot being disconnected."""
    # Only handle the bot's own voice state changes
    if member.id != bot.user.id:
        return
        
    # Bot was disconnected from voice channel
    if before.channel and not after.channel:
        guild_id = member.guild.id
        if guild_id in players:
            player = players[guild_id]
            if player.player_message:
                try:
                    await player.player_message.delete()
                except discord.NotFound:
                    pass
            
            # Try to reconnect if it was an unexpected disconnect
            try:
                if member.guild.voice_client and member.guild.voice_client.is_connected():
                    await member.guild.voice_client.disconnect(force=True)
                await member.guild.voice_client.connect(reconnect=True)
                print("Successfully reconnected to voice channel")
            except Exception as e:
                print(f"Failed to reconnect to voice channel: {e}")
                players.pop(guild_id, None)  # Clean up the player instance if reconnection fails

def force_kill_python_processes():
    """Force kill any remaining Python processes."""
    current_pid = os.getpid()
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            if proc.info['name'] and 'python' in proc.info['name'].lower() and proc.info['pid'] != current_pid:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

def signal_handler(sig, frame):
    """Handle Ctrl+C and other termination signals."""
    print("\n⚠️ Received shutdown signal. Cleaning up...")
    try:
        # Create a task to run the shutdown
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(bot.close())
            # Give it a moment to clean up
            loop.run_until_complete(asyncio.sleep(1))
        else:
            asyncio.run(bot.close())
    except:
        pass
    finally:
        print("✅ Signal handler complete.")
        force_kill_python_processes()
        sys.exit(0)

# Register the signal handler
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# --- Run Bot ---
if __name__ == "__main__":
    try:
        load_dotenv()
        bot.run(os.getenv("DISCORD_TOKEN"))
    except KeyboardInterrupt:
        print("\n⚠️ Keyboard interrupt received. Shutting down...")
        try:
            asyncio.run(bot.close())
        except:
            pass
    except Exception as e:
        print(f"\n❌ Error running bot: {e}")
    finally:
        force_kill_python_processes()
        print("✅ Bot process terminated.")
        sys.exit(0)