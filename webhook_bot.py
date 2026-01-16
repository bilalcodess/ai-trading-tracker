import os
import json
from datetime import datetime
from typing import Dict
import asyncio

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from google import genai
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
        self.client = genai.Client(api_key=Config.GEMINI_API_KEY)
        self.model = 'gemini-2.5-flash'
        print(f"✅ Gemini Extractor initialized with model: {self.model}")
    
    def clean_json_response(self, text: str) -> str:
        """Clean and extract JSON from Gemini response"""
        import re
        
        # Remove markdown code blocks
        if '```' in text:
            parts = text.split('```')
            for part in parts:
                part = part.strip()
                if part.startswith('json'):
                    text = part[4:].strip()
                    break
                elif part.startswith('{'):
                    text = part.strip()
                    break
        
        # Find complete JSON object using brace counting
        brace_count = 0
        start_idx = text.find('{')
        if start_idx == -1:
            return text
        
        end_idx = -1
        for i in range(start_idx, len(text)):
            if text[i] == '{':
                brace_count += 1
            elif text[i] == '}':
                brace_count -= 1
                if brace_count == 0:
                    end_idx = i + 1
                    break
        
        if end_idx > start_idx:
            text = text[start_idx:end_idx]
        
        # Remove trailing commas
        text = re.sub(r',(\s*[}\]])', r'\1', text)
        
        return text
    
    def extract(self, raw_message: str) -> Dict:
        prompt = f"""Extract this trade info as JSON:
Message: "{raw_message}"
Date: {datetime.now().strftime('%Y-%m-%d')}

Rules:
- If profit/loss explicitly stated, use it. Otherwise set to null.
- For Short trades: buy_price is cover price, sell_price is short price

JSON format:
{{"symbol":"STOCK","instrument_type":"Equity","trade_direction":"Long","buy_price":100.0,"sell_price":110.0,"quantity":100,"capital_invested":10000,"profit_loss":null,"strategy":null,"emotion":null}}

Return ONLY the JSON, no explanation."""

        max_retries = 2
        for attempt in range(max_retries):
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config={
                        'temperature': 0,
                        'max_output_tokens': 1000
                    }
                )
                
                raw_text = response.text.strip()
                print(f"📤 Gemini raw response: {raw_text[:100]}...")
                
                # Clean JSON
                cleaned = self.clean_json_response(raw_text)
                print(f"🧹 Cleaned JSON: {cleaned[:100]}...")
                
                # Parse JSON
                data = json.loads(cleaned)
                
                # Calculate P&L if not provided
                if not data.get('profit_loss') or data['profit_loss'] is None:
                    buy = data.get('buy_price')
                    sell = data.get('sell_price')
                    qty = data.get('quantity')
                    if buy and sell and qty:
                        if data.get('trade_direction') == 'Short':
                            pnl = (buy - sell) * qty
                        else:
                            pnl = (sell - buy) * qty
                        data['profit_loss'] = round(pnl, 2)
                        print(f"💰 Calculated P&L: ₹{pnl}")
                    else:
                        data['profit_loss'] = 0
                
                # Add metadata
                data['date'] = datetime.now().strftime('%Y-%m-%d')
                data['raw_message'] = raw_message
                data.setdefault('symbol', 'UNKNOWN')
                data.setdefault('instrument_type', 'Equity')
                data.setdefault('trade_direction', 'Long')
                
                return data
                
            except json.JSONDecodeError as e:
                if attempt < max_retries - 1:
                    print(f"⚠️ JSON parse failed (attempt {attempt+1}), retrying...")
                    continue
                else:
                    print(f"❌ Gemini JSON parse error: {e}")
                    raise
            except Exception as e:
                print(f"❌ Gemini extraction error: {e}")
                raise


# ... RiskManager and TradingBot classes remain the same


# ... rest of classes remain same




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
