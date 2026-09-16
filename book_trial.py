    import os
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ApplicationBuildder, CommandHandler, ContextTypes

# The URL of the pitch booking page (Replace with the real website URL)
URL = "https://atcsports.io/venues/comu-caba?sportIds=4&placeId=69y7pkx6v&dia=2026-09-12&horario=18%3A00&locationName=Buenos+Aires%2C+Ciudad+Aut%C3%B3noma+de+Buenos+Aires%2C+Argentina&placeSearched=69y7pkx6v"

# --- YOUR WEB SCRAPER GOES HERE ---
def check_pitch_availability(date, time_range):
    # Headers make your script look like a regular web browser
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        # Note: In the future, you will need to update this logic so the URL 
        # or the search actually uses your 'date' and 'time_range' variables!
        response = requests.get(URL, headers=headers)
        response.raise_for_status() # Check for HTTP errors
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # --- CRITICAL STEP ---
        # Inspect the booking website's HTML to find exactly what element represents an open slot.
        available_slots = soup.find_all('button', class_='btn-book-now')
        
        if available_slots:
            print(f"Found {len(available_slots)} available slots for {date}!")
            return True
            
        print(f"No slots available for {date} at {time_range} right now.")
        return False
        
    except requests.exceptions.RequestException as e:
        print(f"Error fetching the webpage: {e}")
        return False

# --- TRIGGERED WHEN YOU TYPE /check ---
async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # context.args captures the words you type after the command (e.g., /check 2026-09-20 18:00-20:00)
        date = context.args[0]
        time_range = context.args[1]
        
        # Save the parameters into the bot's memory for this specific chat
        context.chat_data['date'] = date
        context.chat_data['time_range'] = time_range
        
        await update.message.reply_text(f"Will check for {date} at {time_range} every 30 minutes!")
        
        # Start a background timer that runs the scraper function every 1800 seconds (30 mins)
        context.job_queue.run_repeating(
            scrape_and_notify, 
            interval=1800, 
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

if __name__ == '__main__':
    # Using your token directly for local testing
    bot_token = "8997472269:AAEUrwPpZAGdqhgBuZLuebNaCcoD748CZkk"
    
    app = ApplicationBuilder().token(bot_token).build()
    
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("stop", stop_command))
    
    print("Bot is online and listening...")
    app.run_polling()