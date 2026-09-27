import os
import requests
import threading
from bs4 import BeautifulSoup
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- YOUR WEB SCRAPER GOES HERE ---
def check_pitch_availability(date, time_range):
    # Extract the start time (e.g., "09:30" from "09:30-10:30") and replace ":" with "%3A"
    start_time = time_range.split("-")[0].replace(":", "%3A")
    
    # The f-string inserts {date} and {start_time} directly into your dynamic URL
    dynamic_url = f"https://atcsports.io/venues/comu-caba?sportIds=2&placeId=69y77cwtd&dia={date}&horario={start_time}&locationName=San+Mart%C3%ADn%2C+Provincia+de+Buenos+Aires%2C+Argentina&placeSearched=69y77cwtd"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        response = requests.get(dynamic_url, headers=headers)
        response.raise_for_status() # Check for HTTP errors
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Look for the green React span indicating an available slot
        available_slots = soup.find_all('span', class_='available')
        
        if available_slots:
            print(f"Found {len(available_slots)} available slots for {date}!", flush=True)
            return True
            
        print(f"No slots available for {date} at {time_range} right now.", flush=True)
        return False
        
    except requests.exceptions.RequestException as e:
        print(f"Error fetching the webpage: {e}", flush=True)
        return False

# --- TRIGGERED WHEN YOU TYPE /check ---
async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # context.args captures the words you type after the command (e.g., /check 2026-09-29 09:30-10:30)
        date = context.args[0]
        time_range = context.args[1]
        
        # Save the parameters into the bot's memory for this specific chat
        context.chat_data['date'] = date
        context.chat_data['time_range'] = time_range
        
        await update.message.reply_text(f"Will check for {date} at {time_range} every 5 minutes!")
        
        # Start a background timer that runs the scraper function every 300 seconds (5 mins)
        context.job_queue.run_repeating(
            scrape_and_notify, 
            interval=300, 
            first=1, # Run the first check almost immediately
            chat_id=update.message.chat_id,
            name=str(update.message.chat_id) # Name the job so we can stop it later
        )
        
    except IndexError:
        # If you forget to type the date or time
        await update.message.reply_text("Please use the format: /check YYYY-MM-DD HH:MM-HH:MM")

# --- TRIGGERED BY THE 30-MINUTE TIMER ---
async def scrape_and_notify(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.chat_id
    
    # Retrieve your saved parameters from the bot's memory
    date = context.chat_data.get('date')
    time_range = context.chat_data.get('time_range')
    
    # Run your scraper function
    is_available = check_pitch_availability(date, time_range)
    
    if is_available:
        await context.bot.send_message(
            chat_id=chat_id, 
            text=f"⚽ NOTIFICATION: A pitch is available on {date} during {time_range}! Go book it now!"
        )
        # Stop checking once we find a slot
        job.schedule_removal()

# --- TRIGGERED WHEN YOU TYPE /stop ---
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Find the timer associated with your chat ID and delete it
    current_jobs = context.job_queue.get_jobs_by_name(str(update.message.chat_id))
    
    if not current_jobs:
        await update.message.reply_text("I am not currently checking for any pitches.")
        return
        
    for job in current_jobs:
        job.schedule_removal()
    
    await update.message.reply_text("Timer stopped.")


# --- DUMMY SERVER TO KEEP RENDER AWAKE ---
class DummyServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def keep_alive():
    # Render assigns a port dynamically; we catch it here and bind to 0.0.0.0
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), DummyServer)
    print(f"Dummy server listening on port {port}...", flush=True)
    server.serve_forever()

if __name__ == '__main__':
    # Start the dummy web server in the background
    threading.Thread(target=keep_alive, daemon=True).start()
    
    # Grab the token securely from Render's Environment Variables
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    
    app = ApplicationBuilder().token(bot_token).build()
    
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("stop", stop_command))
    
    print("Telegram Bot is online and listening...", flush=True)
    app.run_polling()