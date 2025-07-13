#!/usr/bin/env python3
"""
Simple startup script for the vocabulary bot
This ensures proper initialization order and error handling
"""

import sys
import os
import asyncio
import logging

# Add current directory to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

async def main():
    """Main startup function"""
    try:
        # Import after path setup
        from main import VocabularyBot
        from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
        from telegram import Update
        
        # Import all handlers
        from main import (
            start, help_command, add_word_handler, review_command, 
            stats_command, button_handler, handle_spelling_input,
            cleanup_handler, backup_database
        )
        
        # Get environment variables
        telegram_token = os.getenv('TELEGRAM_TOKEN')
        
        if not telegram_token:
            logger.error("TELEGRAM_TOKEN environment variable is required!")
            sys.exit(1)
        
        logger.info("Initializing vocabulary bot...")
        
        # Initialize the vocabulary bot
        vocab_bot = VocabularyBot(telegram_token)
        await vocab_bot.initialize()
        
        logger.info("Creating Telegram application...")
        
        # Create Telegram application
        application = Application.builder().token(telegram_token).build()
        
        # Store vocab_bot in bot_data for access in handlers
        application.bot_data['vocab_bot'] = vocab_bot
        
        # Add handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("review", review_command))
        application.add_handler(CommandHandler("stats", stats_command))
        application.add_handler(CallbackQueryHandler(button_handler))
        
        # Add spelling input handler (must come before general message handler)
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~(filters.Regex(r'.*=.*')), 
            handle_spelling_input
        ))
        
        # Add word addition handler
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(r'.*=.*'), 
            add_word_handler
        ))
        
        # Add periodic cleanup job (every 30 minutes) - with fallback
        try:
            if application.job_queue:
                application.job_queue.run_repeating(cleanup_handler, interval=1800, first=300)
                application.job_queue.run_repeating(backup_database, interval=86400, first=3600)
                logger.info("Scheduled cleanup and backup jobs")
            else:
                logger.warning("JobQueue not available - running without scheduled tasks")
        except Exception as e:
            logger.warning(f"Could not set up scheduled jobs: {e}")
        
        logger.info("Starting bot polling...")
        
        # Start the bot
        await application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
        
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        sys.exit(1)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)
