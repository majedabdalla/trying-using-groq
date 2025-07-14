import os
import asyncio
from groq import GroqClient
from agno import Agent, Conversation
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

#──── CONFIG ────────────────────────────────────────────────────────────────────
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CODER_MODEL    = "deepseek-coder-33b"
REVIEWER_MODEL = "gemma-7b-it"
#─────────────────────────────────────────────────────────────────────────────────

# Initialize Groq client
client = GroqClient(api_key=GROQ_API_KEY)

class GroqAgent(Agent):
    def __init__(self, name, model):
        super().__init__(name=name)
        self.model = model

    async def respond(self, prompt: str) -> str:
        resp = await client.completions.create(
            model=self.model,
            prompt=prompt,
            max_tokens=512,
        )
        return resp.choices[0].text.strip()

async def run_debate(task: str) -> str:
    coder    = GroqAgent("Coder", CODER_MODEL)
    reviewer = GroqAgent("Reviewer", REVIEWER_MODEL)
    conv     = Conversation(participants=[coder, reviewer])
    conv.add_user_message(f"Task: {task}")

    for _ in range(3):  # 3 debate rounds
        msg_c = await coder.respond(conv.history_text())
        conv.add_agent_message(coder.name, msg_c)
        msg_r = await reviewer.respond(conv.history_text())
        conv.add_agent_message(reviewer.name, msg_r)

    return conv.history_text()

# Telegram handlers
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a coding task, e.g. 'Greet new members'.")

async def handle_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    task = update.message.text
    status = await update.message.reply_text("🤖 Debating… please wait ~30s")
    try:
        result = await run_debate(task)
        await status.edit_text(f"📝 **Debate & Code**:\n```{result}```", parse_mode="Markdown")
    except Exception as e:
        await status.edit_text(f"❌ Error: {e}")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task))
    print("Bot is up!")
    app.run_polling()

if __name__ == "__main__":
    main()

