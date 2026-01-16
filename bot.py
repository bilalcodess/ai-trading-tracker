import json
from datetime import datetime
from typing import Dict, Optional
import asyncio

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials

from config import Config

# ============================================
# GOOGLE SHEETS CLIENT
# ============================================

class SheetsManager:
    def __init__(self):
        scope = [
            'https://spreadsheets.google.com/feeds',
            'https://www.googleapis.com/auth/drive'
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            'service_account.json', scope
        )
        self.client = gspread.authorize(creds)
        self.sheet = self.client.open(Config.SPREADSHEET_NAME)
        self.journal = self.sheet.worksheet("Trade Journal")
        
        # Initialize headers if empty
        if self.journal.row_count == 0 or self.journal.cell(1, 1).value == "":
            self._setup_headers()
    
    def _setup_headers(self):
        """Set up column headers"""
        headers = [
            "Trade ID", "Date", "Time", "Symbol", "Instrument", "Direction",
            "Buy Price", "Sell Price", "Quantity", "Capital", "P&L", "P&L %",
            "Risk %", "R-Multiple", "Strategy", "Emotion", "Win/Loss", "Raw Message", "Notes"
        ]
        self.journal.update('A1:S1', [headers])
        print("✅ Headers initialized")
    
    def append_trade(self, trade_data: Dict):
        """Append trade to journal"""
        capital = trade_data.get('capital_invested') or 0
        pnl = trade_data['profit_loss']
        
        pl_pct = (pnl / capital * 100) if capital > 0 else 0
        risk_pct = abs(pnl / Config.TRADING_CAPITAL * 100) if pnl < 0 else 0
        win_loss = "Win" if pnl > 0 else "Loss"
        
        row = [
            "",  # Auto ID
            trade_data['date'],
            datetime.now().strftime("%H:%M:%S"),
            trade_data['symbol'],
            trade_data['instrument_type'],
            trade_data.get('trade_direction', 'Unknown'),
            trade_data.get('buy_price') or '',
            trade_data.get('sell_price') or '',
            trade_data.get('quantity') or '',
            capital or '',
            pnl,
            round(pl_pct, 2),
            round(risk_pct, 2),
            "",  # R-Multiple
            trade_data.get('strategy') or '',
            trade_data.get('emotion') or '',
            win_loss,
            trade_data.get('raw_message', ''),
            trade_data.get('notes') or ''
        ]
        
        self.journal.append_row(row)
        print(f"✅ Trade logged: {trade_data['symbol']} | P&L: ₹{pnl}")
    
    def get_today_pnl(self) -> float:
        """Get today's total P&L"""
        today = datetime.now().strftime("%Y-%m-%d")
        
        try:
            all_records = self.journal.get_all_records()
            today_pnl = sum(
                float(record.get('P&L', 0) or 0)
                for record in all_records
                if record.get('Date') == today
            )
            return today_pnl
        except:
            return 0.0

# ============================================
# GEMINI EXTRACTOR
# ============================================

class GeminiExtractor:
    def __init__(self):
        genai.configure(api_key=Config.GEMINI_API_KEY)
        self.model = genai.GenerativeModel('gemini-2.5-flash')
    
    def extract(self, raw_message: str) -> Dict:
        """Extract trade data using Gemini with auto P&L calculation"""
        
        prompt = f"""You are a trading data extractor for Indian stock markets.

Extract ALL available information from this message. If profit/loss is NOT mentioned, leave it as null - we will calculate it.

TODAY: {datetime.now().strftime('%Y-%m-%d')}

MESSAGE: "{raw_message}"

EXTRACTION RULES:
1. symbol: Stock/Index name (SUZLON, NIFTY, TATAMOTORS, BANKNIFTY)
2. instrument_type: "Equity", "Intraday", "Option", "Future", or "Swing"
   - If "CE"/"PE"/"call"/"put" → Option
   - If "intraday" mentioned → Intraday
   - Default → Equity
3. trade_direction: 
   - "bought"/"buy"/"long" → "Long"
   - "sold"/"sell"/"short" → "Short"
   - Unknown → "Unknown"
4. buy_price: Entry price per unit (number or null)
5. sell_price: Exit price per unit (number or null)
6. quantity: Number of shares/lots (number or null)
7. capital_invested: Total amount invested (number or null)
8. profit_loss: ONLY if explicitly stated (e.g., "profit 2000", "loss 1500")
   - If NOT mentioned, return null
   - Profit = positive number
   - Loss = negative number
9. strategy: "Breakout", "VWAP", "Momentum", "Reversal", "News", or null
10. emotion: "FOMO", "Revenge", "Calm", "Disciplined", or null

Return ONLY this JSON (no extra text):
{{
  "date": "{datetime.now().strftime('%Y-%m-%d')}",
  "symbol": "EXAMPLE",
  "instrument_type": "Equity",
  "trade_direction": "Long",
  "buy_price": 100.0,
  "sell_price": 110.0,
  "quantity": 100,
  "capital_invested": 10000,
  "profit_loss": null,
  "strategy": null,
  "emotion": null,
  "notes": null
}}"""

        try:
            response = self.model.generate_content(prompt)
            response_text = response.text.strip()
            
            # Remove markdown code blocks
            if response_text.startswith('```'):
                response_text = response_text.split('```')[1]
                if response_text.startswith('json'):
                    response_text = response_text[4:]
                response_text = response_text.strip()
            
            extracted = json.loads(response_text)
            
            # Handle list response
            if isinstance(extracted, list):
                extracted = extracted[0] if extracted else {}
            
            # AUTO-CALCULATE P&L if not provided
            profit_loss = extracted.get('profit_loss')
            
            if profit_loss is None or profit_loss == 0:
                # Method 1: Calculate from buy/sell prices and quantity
                buy_price = extracted.get('buy_price')
                sell_price = extracted.get('sell_price')
                quantity = extracted.get('quantity')
                
                if buy_price and sell_price and quantity:
                    # Standard calculation: (sell - buy) * quantity
                    calculated_pnl = (sell_price - buy_price) * quantity
                    extracted['profit_loss'] = round(calculated_pnl, 2)
                    print(f"💰 Auto-calculated P&L: ₹{calculated_pnl}")
                
                # Method 2: Calculate from invested and exit amounts (for options)
                elif extracted.get('capital_invested'):
                    # Check for exit amount in notes or parse message
                    capital = extracted['capital_invested']
                    # If we can't auto-calculate, set to 0 (user must specify)
                    extracted['profit_loss'] = 0
                    print("⚠️ P&L set to 0 - need exit amount for options")
                else:
                    # No way to calculate
                    extracted['profit_loss'] = 0
                    print("⚠️ Insufficient data to calculate P&L - set to 0")
            
            # Add raw message
            extracted['raw_message'] = raw_message
            
            # Set defaults
            extracted.setdefault('date', datetime.now().strftime('%Y-%m-%d'))
            extracted.setdefault('symbol', 'UNKNOWN')
            extracted.setdefault('instrument_type', 'Equity')
            extracted.setdefault('trade_direction', 'Unknown')
            
            return extracted
            
        except json.JSONDecodeError as e:
            print(f"❌ JSON error: {e}")
            print(f"Response: {response.text[:200]}")
            raise ValueError(f"Failed to parse response: {e}")
        except Exception as e:
            print(f"❌ Extraction error: {e}")
            raise

# ============================================
# RISK MANAGER
# ============================================

class RiskManager:
    @staticmethod
    def validate_trade(trade_data: Dict, today_pnl: float) -> Dict:
        """Validate trade against risk rules"""
        
        warnings = []
        block_trade = False
        pnl = trade_data['profit_loss']
        
        # Check 1: Max loss per trade
        if pnl < Config.MAX_LOSS_PER_TRADE:
            warnings.append(
                f"⚠️ Trade loss ₹{pnl} exceeds limit ₹{Config.MAX_LOSS_PER_TRADE}"
            )
        
        # Check 2: Daily max loss
        projected_pnl = today_pnl + pnl
        if projected_pnl < Config.MAX_LOSS_PER_DAY:
            warnings.append(
                f"🚨 DAILY LIMIT BREACH!\n"
                f"Today's P&L: ₹{projected_pnl:.2f}\n"
                f"Limit: ₹{Config.MAX_LOSS_PER_DAY}"
            )
            warnings.append("🛑 STOP TRADING TODAY!")
            block_trade = True
        
        # Check 3: Risk %
        if trade_data.get('capital_invested'):
            risk_pct = abs(pnl / Config.TRADING_CAPITAL * 100)
            if risk_pct > Config.MAX_RISK_PCT:
                warnings.append(
                    f"⚠️ Risk {risk_pct:.2f}% > {Config.MAX_RISK_PCT}% limit"
                )
        
        # Check 4: Emotional trading
        emotion = trade_data.get('emotion')
        if emotion and emotion in ['FOMO', 'Revenge', 'Fear', 'Greed']:
            warnings.append(f"⚠️ Emotional trade: {emotion}")
        
        return {
            'valid': not block_trade,
            'warnings': warnings,
            'today_pnl': projected_pnl
        }

# ============================================
# TELEGRAM BOT
# ============================================

class TradingBot:
    def __init__(self):
        print("🔄 Initializing bot...")
        self.sheets = SheetsManager()
        self.gemini = GeminiExtractor()
        self.risk_mgr = RiskManager()
        
        self.app = Application.builder().token(Config.TELEGRAM_BOT_TOKEN).build()
        
        # Handlers
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("stats", self.stats_command))
        self.app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, 
            self.handle_trade_message
        ))
        
        print("✅ Bot initialized")
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Welcome message"""
        msg = (
            "🤖 *AI Trading Tracker Active*\n\n"
            "Send me trades in plain text:\n"
            "• `Bought 200 Suzlon at 42.5, sold at 44, profit 3000`\n"
            "• `Nifty 23500 CE, invested 15k, exited at 17.5k`\n"
            "• `Loss 1200 in BankNifty PE`\n\n"
            "Commands:\n"
            "/stats - Today's performance"
        )
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    async def handle_trade_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process trade message"""
        raw_message = update.message.text
        
        try:
            # Step 1: Extract
            await update.message.reply_text("🔄 Processing...")
            trade_data = self.gemini.extract(raw_message)
            
            # Step 2: Risk check
            today_pnl = self.sheets.get_today_pnl()
            risk_check = self.risk_mgr.validate_trade(trade_data, today_pnl)
            
            # Step 3: Save or block
            if risk_check['valid']:
                self.sheets.append_trade(trade_data)
                
                response = (
                    f"✅ *Trade Logged*\n\n"
                    f"📊 {trade_data['symbol']} | {trade_data['instrument_type']}\n"
                    f"💰 P&L: ₹{trade_data['profit_loss']}\n"
                    f"📈 Today Total: ₹{risk_check['today_pnl']:.2f}\n"
                )
                
                if trade_data.get('strategy'):
                    response += f"🎯 Strategy: {trade_data['strategy']}\n"
                
                if risk_check['warnings']:
                    response += "\n" + "\n".join(risk_check['warnings'])
                
                await update.message.reply_text(response, parse_mode='Markdown')
            else:
                # Blocked
                response = "🚫 *TRADE NOT LOGGED*\n\n"
                response += "\n".join(risk_check['warnings'])
                await update.message.reply_text(response, parse_mode='Markdown')
                
        except Exception as e:
            error_msg = f"❌ Error: {str(e)}\n\nMessage: {raw_message}"
            await update.message.reply_text(error_msg)
            print(f"Error processing: {e}")
    
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show today's stats"""
        today_pnl = self.sheets.get_today_pnl()
        buffer = Config.MAX_LOSS_PER_DAY + today_pnl
        
        response = (
            f"📊 *Today's Performance*\n\n"
            f"💰 Total P&L: ₹{today_pnl:.2f}\n"
            f"🛡️ Loss Buffer: ₹{buffer:.2f}\n"
            f"📉 Daily Limit: ₹{Config.MAX_LOSS_PER_DAY}\n"
        )
        
        await update.message.reply_text(response, parse_mode='Markdown')
    
    def run(self):
        """Start polling"""
        print("\n🚀 Trading Bot Started!")
        print("📱 Send /start to your bot on Telegram\n")
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)

# ============================================
# MAIN
# ============================================

if __name__ == "__main__":
    try:
        bot = TradingBot()
        bot.run()
    except KeyboardInterrupt:
        print("\n👋 Bot stopped")
    except Exception as e:
        print(f"❌ Fatal error: {e}")
