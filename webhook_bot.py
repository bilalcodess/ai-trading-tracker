import os
import json
from datetime import datetime
from typing import Dict
import asyncio

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from aiohttp import web

from config import Config

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
    
    def append_trade(self, trade_data: Dict):
        capital = trade_data.get('capital_invested') or 0
        pnl = trade_data['profit_loss']
        
        pl_pct = (pnl / capital * 100) if capital > 0 else 0
        risk_pct = abs(pnl / Config.TRADING_CAPITAL * 100) if pnl < 0 else 0
        win_loss = "Win" if pnl > 0 else "Loss"
        
        row = [
            "", trade_data['date'], datetime.now().strftime("%H:%M:%S"),
            trade_data['symbol'], trade_data['instrument_type'],
            trade_data.get('trade_direction', 'Unknown'),
            trade_data.get('buy_price') or '', trade_data.get('sell_price') or '',
            trade_data.get('quantity') or '', capital or '', pnl,
            round(pl_pct, 2), round(risk_pct, 2), "",
            trade_data.get('strategy') or '', trade_data.get('emotion') or '',
            win_loss, trade_data.get('raw_message', ''), trade_data.get('notes') or ''
        ]
        
        self.journal.append_row(row)
        print(f"✅ Trade logged: {trade_data['symbol']} | P&L: ₹{pnl}")
    
    def get_today_pnl(self) -> float:
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            all_records = self.journal.get_all_records()
            return sum(float(record.get('P&L', 0) or 0) for record in all_records if record.get('Date') == today)
        except:
            return 0.0

class GeminiExtractor:
    def __init__(self):
        genai.configure(api_key=Config.GEMINI_API_KEY)
        self.model = genai.GenerativeModel('gemini-pro')
    
    def extract(self, raw_message: str) -> Dict:
        prompt = f"""Extract trade data. If profit/loss NOT stated, return null.
TODAY: {datetime.now().strftime('%Y-%m-%d')}
MESSAGE: "{raw_message}"

Return JSON:
{{"symbol": "STOCK", "instrument_type": "Equity", "trade_direction": "Long", 
"buy_price": 100.0, "sell_price": 110.0, "quantity": 100, "capital_invested": 10000,
"profit_loss": null, "strategy": null, "emotion": null}}"""

        response = self.model.generate_content(prompt)
        text = response.text.strip()
        
        if '```' in text:
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
            text = text.strip()
        
        data = json.loads(text)
        if isinstance(data, list):
            data = data[0]
        
        if not data.get('profit_loss'):
            buy = data.get('buy_price')
            sell = data.get('sell_price')
            qty = data.get('quantity')
            if buy and sell and qty:
                pnl = (sell - buy) * qty
                data['profit_loss'] = round(pnl, 2)
                print(f"💰 Calculated P&L: ₹{pnl}")
            else:
                data['profit_loss'] = 0
        
        data['date'] = datetime.now().strftime('%Y-%m-%d')
        data['raw_message'] = raw_message
        data.setdefault('symbol', 'UNKNOWN')
        data.setdefault('instrument_type', 'Equity')
        data.setdefault('trade_direction', 'Long')
        return data

class RiskManager:
    @staticmethod
    def validate_trade(trade_data: Dict, today_pnl: float) -> Dict:
        warnings = []
        block_trade = False
        pnl = trade_data['profit_loss']
        
        if pnl < Config.MAX_LOSS_PER_TRADE:
            warnings.append(f"⚠️ Trade loss ₹{pnl} exceeds limit ₹{Config.MAX_LOSS_PER_TRADE}")
        
        projected_pnl = today_pnl + pnl
        if projected_pnl < Config.MAX_LOSS_PER_DAY:
            warnings.append(f"🚨 DAILY LIMIT! P&L: ₹{projected_pnl:.2f}, Limit: ₹{Config.MAX_LOSS_PER_DAY}")
            warnings.append("🛑 STOP TRADING TODAY!")
            block_trade = True
        
        return {'valid': not block_trade, 'warnings': warnings, 'today_pnl': projected_pnl}

class TradingBot:
    def __init__(self):
        print("🔄 Initializing webhook bot...")
        self.sheets = SheetsManager()
        self.gemini = GeminiExtractor()
        self.risk_mgr = RiskManager()
        
        # Build application WITHOUT job_queue (Python 3.13 compatibility)
        self.application = (
            Application.builder()
            .token(Config.TELEGRAM_BOT_TOKEN)
            .job_queue(None)
            .build()
        )
        
        # Add handlers
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("stats", self.stats_command))
        self.application.add_handler(CommandHandler("daily", self.daily_command))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_trade_message))
        
        print("✅ Bot initialized")
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 *AI Trading Tracker*\n\nSend: `Bought 100 Suzlon at 40, sold at 42`\n\n"
            "/stats - Today's performance\n/daily - Daily summary",
            parse_mode='Markdown'
        )
    
    async def handle_trade_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        raw_message = update.message.text
        try:
            await update.message.reply_text("🔄 Processing...")
            trade_data = self.gemini.extract(raw_message)
            today_pnl = self.sheets.get_today_pnl()
            risk_check = self.risk_mgr.validate_trade(trade_data, today_pnl)
            
            if risk_check['valid']:
                self.sheets.append_trade(trade_data)
                response = f"✅ *Logged*\n📊 {trade_data['symbol']} | ₹{trade_data['profit_loss']}\n📈 Today: ₹{risk_check['today_pnl']:.2f}"
                if risk_check['warnings']:
                    response += "\n\n" + "\n".join(risk_check['warnings'])
                await update.message.reply_text(response, parse_mode='Markdown')
            else:
                await update.message.reply_text("🚫 *BLOCKED*\n\n" + "\n".join(risk_check['warnings']), parse_mode='Markdown')
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
            print(f"Error: {e}")
    
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        today_pnl = self.sheets.get_today_pnl()
        buffer = Config.MAX_LOSS_PER_DAY + today_pnl
        await update.message.reply_text(
            f"📊 *Today*\n💰 P&L: ₹{today_pnl:.2f}\n🛡️ Buffer: ₹{buffer:.2f}",
            parse_mode='Markdown'
        )
    
    async def daily_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        today_pnl = self.sheets.get_today_pnl()
        today = datetime.now().strftime("%Y-%m-%d")
        
        try:
            all_records = self.sheets.journal.get_all_records()
            today_trades = [r for r in all_records if r.get('Date') == today]
            
            wins = sum(1 for t in today_trades if float(t.get('P&L', 0) or 0) > 0)
            losses = sum(1 for t in today_trades if float(t.get('P&L', 0) or 0) < 0)
            win_rate = (wins / len(today_trades) * 100) if today_trades else 0
            
            response = f"""📊 *Daily Summary*

💰 Total P&L: ₹{today_pnl:.2f}
📈 Trades: {len(today_trades)}
✅ Wins: {wins}
❌ Losses: {losses}
📊 Win Rate: {win_rate:.1f}%
🛡️ Buffer: ₹{Config.MAX_LOSS_PER_DAY + today_pnl:.2f}"""
        except Exception as e:
            response = f"📊 *Daily Summary*\n\n💰 Total P&L: ₹{today_pnl:.2f}"
            print(f"Daily summary error: {e}")
        
        await update.message.reply_text(response, parse_mode='Markdown')
    
    async def health_check(self, request):
        return web.Response(text="Bot is running!")
    
    async def handle_webhook(self, request):
        try:
            data = await request.json()
            update = Update.de_json(data, self.application.bot)
            await self.application.process_update(update)
            return web.Response(text="OK")
        except Exception as e:
            print(f"Webhook error: {e}")
            return web.Response(status=500, text=str(e))
    
    async def run(self):
        # Initialize application
        await self.application.initialize()
        await self.application.start()
        
        # Set webhook
        webhook_url = os.getenv('RENDER_EXTERNAL_URL', 'http://localhost:10000') + '/webhook'
        await self.application.bot.set_webhook(webhook_url)
        print(f"🌐 Webhook: {webhook_url}")
        
        # Create web app
        app = web.Application()
        app.router.add_post('/webhook', self.handle_webhook)
        app.router.add_get('/', self.health_check)
        app.router.add_get('/health', self.health_check)
        
        # Start server
        runner = web.AppRunner(app)
        await runner.setup()
        
        port = int(os.getenv('PORT', 10000))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        
        print(f"🚀 Running on port {port}")
        print("📱 Ready to receive updates!")
        
        # Keep running
        await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        bot = TradingBot()
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\n👋 Stopped")
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
