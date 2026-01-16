import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "AI Trading Journal")
    
    # Risk Management
    MAX_LOSS_PER_DAY = float(os.getenv("MAX_LOSS_PER_DAY", -5000))
    MAX_LOSS_PER_TRADE = float(os.getenv("MAX_LOSS_PER_TRADE", -2000))
    TRADING_CAPITAL = float(os.getenv("TRADING_CAPITAL", 100000))
    MAX_RISK_PCT = 2.0
