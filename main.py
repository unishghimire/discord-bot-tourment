import discord
from discord.ext import commands
import os
from flask import Flask
from threading import Thread

# --- WEB SERVER FOR RENDER ---
app = Flask('')

@app.route('/')
def home():
    return "I am alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# --- DISCORD BOT LOGIC ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

# --- START BOTH ---
keep_alive()  # Starts the web server
token = os.environ.get('DISCORD_TOKEN') # We will set this in Render
bot.run(token)
