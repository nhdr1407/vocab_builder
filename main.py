import os
import sqlite3
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import asyncio
from typing import Dict, List, Tuple, Optional
import random
import time
from functools import wraps
import aiosqlite
import threading

# Configure logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

def retry_on_failure(max_retries=3, delay=1):
    """Decorator for retrying failed operations"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        logger.error(f"Failed after {max_retries} attempts: {e}")
                        raise e
                    logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay}s...")
                    await asyncio.sleep(delay * (attempt + 1))
            return None
        return wrapper
    return decorator

class SessionManager:
    """Manages user review sessions"""
    
    def __init__(self):
        self.sessions = {}
        self._lock = threading.Lock()
    
    def create_session(self, user_id: int, session_type: str, data: Dict) -> str:
        """Create a new session"""
        with self._lock:
            session_id = f"{user_id}_{session_type}_{int(time.time())}"
            self.sessions[session_id] = {
                'user_id': user_id,
                'type': session_type,
                'data': data,
                'created_at': datetime.now(),
                'last_activity': datetime.now()
            }
            return session_id
    
    def get_session(self, session_id: str) -> Optional[Dict]:
        """Get session data"""
        with self._lock:
            if session_id in self.sessions:
                self.sessions[session_id]['last_activity'] = datetime.now()
                return self.sessions[session_id]
            return None
    
    def update_session(self, session_id: str, data: Dict):
        """Update session data"""
        with self._lock:
            if session_id in self.sessions:
                self.sessions[session_id]['data'].update(data)
                self.sessions[session_id]['last_activity'] = datetime.now()
    
    def end_session(self, session_id: str):
        """End a session"""
        with self._lock:
            if session_id in self.sessions:
                del self.sessions[session_id]
    
    def cleanup_old_sessions(self, max_age_hours: int = 2):
        """Remove old inactive sessions"""
        with self._lock:
            cutoff = datetime.now() - timedelta(hours=max_age_hours)
            to_remove = [
                sid for sid, session in self.sessions.items()
                if session['last_activity'] < cutoff
            ]
            for sid in to_remove:
                del self.sessions[sid]

class SpacedRepetitionSM2:
    """Improved SM-2 spaced repetition algorithm"""
    
    @staticmethod
    def calculate_next_review(ease_factor: float, interval: int, quality: int) -> Tuple[int, float]:
        """
        Calculate next review using SM-2 algorithm
        quality: 0-5 (0=total blackout, 5=perfect response)
        """
        if quality < 3:
            # Reset interval for poor performance
            interval = 1
        else:
            if interval == 0:
                interval = 1
            elif interval == 1:
                interval = 6
            else:
                interval = round(interval * ease_factor)
        
        # Update ease factor
        ease_factor = ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
        ease_factor = max(1.3, ease_factor)
        
        return interval, ease_factor
    
    @staticmethod
    def quality_from_performance(correct: bool, confidence: int, response_time: float) -> int:
        """Convert performance metrics to SM-2 quality score (0-5)"""
        if not correct:
            return max(0, confidence - 2)  # 0-1 for incorrect answers
        
        # Base quality for correct answers
        base_quality = 3 + confidence  # 3-5 for correct answers
        
        # Adjust for response time (faster = higher quality)
        if response_time < 3:
            time_bonus = 1
        elif response_time < 10:
            time_bonus = 0
        else:
            time_bonus = -1
        
        return max(3, min(5, base_quality + time_bonus))

class VocabularyDatabase:
    """SQLite database manager for vocabulary"""
    
    def __init__(self, db_path: str = "vocabulary.db"):
        self.db_path = db_path
        self._lock = asyncio.Lock()
        
    async def initialize(self):
        """Initialize database with tables"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS vocabulary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    word TEXT NOT NULL,
                    definition TEXT NOT NULL,
                    date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_reviewed TIMESTAMP,
                    next_review TIMESTAMP NOT NULL,
                    success_count INTEGER DEFAULT 0,
                    failure_count INTEGER DEFAULT 0,
                    interval_days INTEGER DEFAULT 1,
                    ease_factor REAL DEFAULT 2.5,
                    total_reviews INTEGER DEFAULT 0,
                    average_quality REAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_next_review 
                ON vocabulary(user_id, next_review)
            """)
            
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_word 
                ON vocabulary(user_id, word)
            """)
            
            await db.commit()
        
        logger.info("Database initialized successfully")
    
    @retry_on_failure(max_retries=3, delay=1)
    async def add_words_batch(self, words_data: List[Tuple[str, str]], user_id: int) -> Tuple[int, int]:
        """Add multiple words in batch"""
        async with self._lock:
            try:
                async with aiosqlite.connect(self.db_path) as db:
                    successful = 0
                    failed = 0
                    
                    for word, definition in words_data:
                        if not word or not definition:
                            failed += 1
                            continue
                        
                        word = word.lower().strip()
                        definition = definition.strip()
                        
                        # Check if word already exists for this user
                        cursor = await db.execute(
                            "SELECT id FROM vocabulary WHERE user_id = ? AND word = ?",
                            (user_id, word)
                        )
                        existing = await cursor.fetchone()
                        
                        if existing:
                            failed += 1
                            continue
                        
                        next_review = datetime.now() + timedelta(days=1)
                        
                        await db.execute("""
                            INSERT INTO vocabulary 
                            (user_id, word, definition, next_review)
                            VALUES (?, ?, ?, ?)
                        """, (user_id, word, definition, next_review))
                        
                        successful += 1
                    
                    await db.commit()
                    return successful, failed
                    
            except Exception as e:
                logger.error(f"Error in batch add: {e}")
                return 0, len(words_data)
    
    @retry_on_failure(max_retries=3, delay=1)
    async def get_due_words(self, user_id: int, limit: int = 5) -> List[Dict]:
        """Get words due for review"""
        async with self._lock:
            try:
                async with aiosqlite.connect(self.db_path) as db:
                    db.row_factory = aiosqlite.Row
                    
                    cursor = await db.execute("""
                        SELECT * FROM vocabulary 
                        WHERE user_id = ? AND next_review <= ?
                        ORDER BY next_review ASC, ease_factor ASC
                        LIMIT ?
                    """, (user_id, datetime.now(), limit))
                    
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
                    
            except Exception as e:
                logger.error(f"Error getting due words: {e}")
                return []
    
    @retry_on_failure(max_retries=3, delay=1)
    async def update_word_progress_batch(self, updates: List[Dict]):
        """Update multiple word progress entries in batch"""
        async with self._lock:
            try:
                async with aiosqlite.connect(self.db_path) as db:
                    for update_data in updates:
                        word_id = update_data['word_id']
                        correct = update_data['correct']
                        confidence = update_data.get('confidence', 3)
                        response_time = update_data.get('response_time', 5)
                        
                        # Get current record
                        cursor = await db.execute(
                            "SELECT * FROM vocabulary WHERE id = ?", (word_id,)
                        )
                        record = await cursor.fetchone()
                        
                        if not record:
                            continue
                        
                        success_count = record['success_count']
                        failure_count = record['failure_count']
                        interval_days = record['interval_days']
                        ease_factor = record['ease_factor']
                        total_reviews = record['total_reviews']
                        avg_quality = record['average_quality']
                        
                        # Calculate quality score
                        quality = SpacedRepetitionSM2.quality_from_performance(
                            correct, confidence, response_time
                        )
                        
                        # Update counts
                        if correct:
                            success_count += 1
                        else:
                            failure_count += 1
                        
                        total_reviews += 1
                        avg_quality = ((avg_quality * (total_reviews - 1)) + quality) / total_reviews
                        
                        # Calculate next review
                        new_interval, new_ease = SpacedRepetitionSM2.calculate_next_review(
                            ease_factor, interval_days, quality
                        )
                        
                        now = datetime.now()
                        next_review = now + timedelta(days=new_interval)
                        
                        # Update record
                        await db.execute("""
                            UPDATE vocabulary SET
                                last_reviewed = ?,
                                next_review = ?,
                                success_count = ?,
                                failure_count = ?,
                                interval_days = ?,
                                ease_factor = ?,
                                total_reviews = ?,
                                average_quality = ?
                            WHERE id = ?
                        """, (
                            now, next_review, success_count, failure_count,
                            new_interval, new_ease, total_reviews, round(avg_quality, 2),
                            word_id
                        ))
                    
                    await db.commit()
                    return True
                    
            except Exception as e:
                logger.error(f"Error in batch update: {e}")
                return False
    
    @retry_on_failure(max_retries=2, delay=1)
    async def get_user_analytics(self, user_id: int) -> Dict:
        """Get comprehensive user analytics"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                
                # Get all user words
                cursor = await db.execute(
                    "SELECT * FROM vocabulary WHERE user_id = ?", (user_id,)
                )
                user_words = await cursor.fetchall()
                
                if not user_words:
                    return {
                        'total_words': 0,
                        'mastered_words': 0,
                        'learning_words': 0,
                        'new_words': 0,
                        'due_count': 0,
                        'avg_ease_factor': 0,
                        'total_reviews': 0,
                        'success_rate': 0,
                        'streak_days': 0
                    }
                
                total_words = len(user_words)
                mastered_words = len([w for w in user_words 
                                    if w['success_count'] >= 5 and w['ease_factor'] > 2.0])
                learning_words = len([w for w in user_words 
                                    if 0 < w['success_count'] < 5])
                new_words = len([w for w in user_words if w['success_count'] == 0])
                
                # Count due words
                cursor = await db.execute("""
                    SELECT COUNT(*) as due_count FROM vocabulary 
                    WHERE user_id = ? AND next_review <= ?
                """, (user_id, datetime.now()))
                due_result = await cursor.fetchone()
                due_count = due_result['due_count'] if due_result else 0
                
                # Calculate averages
                avg_ease_factor = sum(w['ease_factor'] for w in user_words) / total_words
                total_reviews = sum(w['total_reviews'] for w in user_words)
                total_success = sum(w['success_count'] for w in user_words)
                total_attempts = sum(w['success_count'] + w['failure_count'] for w in user_words)
                success_rate = (total_success / total_attempts * 100) if total_attempts > 0 else 0
                
                # Calculate learning streak (simplified)
                cursor = await db.execute("""
                    SELECT DATE(last_reviewed) as review_date 
                    FROM vocabulary 
                    WHERE user_id = ? AND last_reviewed IS NOT NULL
                    GROUP BY DATE(last_reviewed)
                    ORDER BY review_date DESC
                """, (user_id,))
                review_dates = await cursor.fetchall()
                
                streak_days = 0
                if review_dates:
                    current_date = datetime.now().date()
                    for row in review_dates:
                        review_date = datetime.fromisoformat(row['review_date']).date()
                        if (current_date - review_date).days <= streak_days + 1:
                            streak_days += 1
                            current_date = review_date
                        else:
                            break
                
                return {
                    'total_words': total_words,
                    'mastered_words': mastered_words,
                    'learning_words': learning_words,
                    'new_words': new_words,
                    'due_count': due_count,
                    'avg_ease_factor': round(avg_ease_factor, 2),
                    'total_reviews': total_reviews,
                    'success_rate': round(success_rate, 1),
                    'streak_days': streak_days
                }
                
        except Exception as e:
            logger.error(f"Error getting analytics: {e}")
            return {}
    
    async def get_random_definitions_for_mcq(self, user_id: int, exclude_word: str, count: int = 3) -> List[str]:
        """Get random definitions for multiple choice questions"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    SELECT definition FROM vocabulary 
                    WHERE user_id = ? AND word != ?
                    ORDER BY RANDOM()
                    LIMIT ?
                """, (user_id, exclude_word, count))
                
                rows = await cursor.fetchall()
                return [row[0] for row in rows]
                
        except Exception as e:
            logger.error(f"Error getting random definitions: {e}")
            return []

class VocabularyBot:
    def __init__(self, telegram_token: str):
        self.telegram_token = telegram_token
        self.session_manager = SessionManager()
        self.db = VocabularyDatabase()
        
    async def initialize(self):
        """Initialize the bot and database"""
        await self.db.initialize()
        logger.info("Vocabulary bot initialized successfully")

# Bot command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    welcome_text = """
🎓 **Welcome to your Advanced Vocabulary Builder!**

📝 **Adding words is super simple:**

**Single word:**
`word = definition`

**Multiple words (one per line):**
```
serendipity = pleasant surprise
ubiquitous = present everywhere
ephemeral = lasting for a short time
```

📚 **Commands:**
/review - Start a review session
/stats - See detailed analytics
/help - Show this message

🧠 **Review Modes:**
• Definition recall
• Multiple choice
• Spelling tests
• Confidence-based scoring

Ready to supercharge your vocabulary? 🚀
    """
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command handler"""
    await start(update, context)

async def add_word_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enhanced word addition handler supporting multiple words"""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    
    # Split into lines for multiple word support
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    
    words_to_add = []
    invalid_lines = []
    
    for line in lines:
        if '=' not in line:
            invalid_lines.append(line)
            continue
        
        try:
            word, definition = line.split('=', 1)
            word = word.strip()
            definition = definition.strip()
            
            if word and definition:
                words_to_add.append((word, definition))
            else:
                invalid_lines.append(line)
        except Exception:
            invalid_lines.append(line)
    
    if not words_to_add:
        await update.message.reply_text(
            "❌ Please use this format:\n\n"
            "**Single word:** `word = definition`\n"
            "**Multiple words:**\n"
            "```\nword1 = definition1\nword2 = definition2```",
            parse_mode='Markdown'
        )
        return
    
    try:
        # Add words in batch
        successful, failed = await context.bot_data['vocab_bot'].db.add_words_batch(words_to_add, user_id)
        
        response_text = f"✅ **Added {successful} word(s) successfully!**\n\n"
        
        if successful <= 3:  # Show details for small batches
            for word, definition in words_to_add[:successful]:
                response_text += f"📖 **{word}** - {definition}\n"
        
        if failed > 0 or invalid_lines:
            response_text += f"\n⚠️ {failed + len(invalid_lines)} line(s) had issues"
            if failed > 0:
                response_text += " (duplicates or errors)"
        
        response_text += f"\n🎯 Words will appear in your next review session!"
        
        await update.message.reply_text(response_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error in add_word_handler: {e}")
        await update.message.reply_text("❌ Error adding words. Please try again later.")

async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enhanced review session with multiple modes"""
    user_id = update.effective_user.id
    
    # Clean up old sessions (replaces scheduled job)
    context.bot_data['vocab_bot'].session_manager.cleanup_old_sessions()
    
    # Trigger backup occasionally (every 50th review)
    import random
    if random.randint(1, 50) == 1:
        try:
            await backup_database(context)
        except Exception as e:
            logger.warning(f"Background backup failed: {e}")
    
    due_words = await context.bot_data['vocab_bot'].db.get_due_words(user_id, 10)
    
    if not due_words:
        await update.message.reply_text(
            "🎉 **No words due for review right now!**\n\n"
            "Add more words or check back later. Keep learning! 📚"
        )
        return
    
    # Create review session
    session_data = {
        'words': due_words,
        'current_index': 0,
        'results': [],
        'mode': 'definition_recall',  # Default mode
        'start_time': time.time()
    }
    
    session_id = context.bot_data['vocab_bot'].session_manager.create_session(
        user_id, 'review', session_data
    )
    
    # Show review mode selection
    keyboard = [
        [InlineKeyboardButton("📖 Definition Recall", callback_data=f"mode_definition_{session_id}")],
        [InlineKeyboardButton("🎯 Multiple Choice", callback_data=f"mode_choice_{session_id}")],
        [InlineKeyboardButton("✍️ Spelling Test", callback_data=f"mode_spelling_{session_id}")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🎓 **Review Session Started!**\n\n"
        f"📊 {len(due_words)} words due for review\n\n"
        f"Choose your review mode:",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def show_review_question(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: str):
    """Show review question based on mode"""
    session = context.bot_data['vocab_bot'].session_manager.get_session(session_id)
    if not session:
        await update.effective_message.reply_text("❌ Session expired. Start a new review!")
        return
    
    data = session['data']
    words = data['words']
    index = data['current_index']
    mode = data['mode']
    
    if index >= len(words):
        await complete_review_session(update, context, session_id)
        return
    
    current_word = words[index]
    word = current_word['word']
    definition = current_word['definition']
    
    # Store question start time for response time calculation
    data['question_start_time'] = time.time()
    context.bot_data['vocab_bot'].session_manager.update_session(session_id, data)
    
    message_text = f"📝 **Question {index + 1}/{len(words)}**\n\n"
    
    if mode == 'definition_recall':
        keyboard = [
            [InlineKeyboardButton("💡 Show Definition", callback_data=f"show_def_{session_id}")],
            [
                InlineKeyboardButton("✅ I Know", callback_data=f"answer_correct_5_{session_id}"),
                InlineKeyboardButton("❌ I Don't Know", callback_data=f"answer_incorrect_1_{session_id}")
            ]
        ]
        message_text += f"🔤 **{word.upper()}**\n\nDo you remember the definition?"
        
    elif mode == 'choice':
        # Generate multiple choice options
        other_definitions = await context.bot_data['vocab_bot'].db.get_random_definitions_for_mcq(
            session['user_id'], word, 3
        )
        
        if len(other_definitions) >= 3:
            choices = other_definitions + [definition]
            random.shuffle(choices)
            correct_index = choices.index(definition)
            
            keyboard = []
            for i, choice in enumerate(choices):
                is_correct = (i == correct_index)
                callback_data = f"choice_{'correct' if is_correct else 'incorrect'}_{4 if is_correct else 1}_{session_id}"
                keyboard.append([InlineKeyboardButton(f"{chr(65+i)}. {choice[:50]}", callback_data=callback_data)])
            
            message_text += f"🔤 **{word.upper()}**\n\nChoose the correct definition:"
        else:
            # Fallback to definition recall if not enough choices
            data['mode'] = 'definition_recall'
            await show_review_question(update, context, session_id)
            return
            
    elif mode == 'spelling':
        # Show definition, ask for spelling
        keyboard = [
            [InlineKeyboardButton("✍️ Type the word", callback_data=f"spell_prompt_{session_id}")],
            [InlineKeyboardButton("💡 Show Answer", callback_data=f"spell_show_{session_id}")]
        ]
        message_text += f"📖 **Definition:** {definition}\n\nWhat's the word?"
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            message_text, parse_mode='Markdown', reply_markup=reply_markup
        )
    else:
        await update.effective_message.reply_text(
            message_text, parse_mode='Markdown', reply_markup=reply_markup
        )

async def complete_review_session(update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: str):
    """Complete review session with detailed results"""
    session = context.bot_data['vocab_bot'].session_manager.get_session(session_id)
    if not session:
        return
    
    data = session['data']
    results = data['results']
    total_time = time.time() - data['start_time']
    
    # Update progress in batch
    if results:
        await context.bot_data['vocab_bot'].db.update_word_progress_batch(results)
    
    # Calculate statistics
    total_questions = len(results)
    correct_answers = len([r for r in results if r['correct']])
    accuracy = (correct_answers / total_questions * 100) if total_questions > 0 else 0
    avg_confidence = sum(r.get('confidence', 3) for r in results) / total_questions if results else 0
    avg_response_time = sum(r.get('response_time', 5) for r in results) / total_questions if results else 0
    
    # Generate performance feedback
    if accuracy >= 90:
        performance_emoji = "🔥"
        performance_text = "Excellent!"
    elif accuracy >= 75:
        performance_emoji = "👏"
        performance_text = "Great job!"
    elif accuracy >= 60:
        performance_emoji = "👍"
        performance_text = "Good work!"
    else:
        performance_emoji = "💪"
        performance_text = "Keep practicing!"
    
    results_text = f"""
🎊 **Review Complete!** {performance_emoji}

📊 **Results:**
• Score: {correct_answers}/{total_questions} ({accuracy:.1f}%)
• Average confidence: {avg_confidence:.1f}/5
• Average response time: {avg_response_time:.1f}s
• Total time: {total_time/60:.1f} minutes

{performance_text} Your vocabulary is growing stronger! 🧠✨

💡 Words you struggled with will appear more frequently.
    """
    
    await update.effective_message.reply_text(results_text, parse_mode='Markdown')
    
    # Clean up session
    context.bot_data['vocab_bot'].session_manager.end_session(session_id)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enhanced button handler for multiple review modes"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    # Mode selection
    if data.startswith('mode_'):
        parts = data.split('_')
        mode = parts[1]
        session_id = '_'.join(parts[2:])
        
        session = context.bot_data['vocab_bot'].session_manager.get_session(session_id)
        if session:
            session['data']['mode'] = mode
            context.bot_data['vocab_bot'].session_manager.update_session(session_id, session['data'])
            await show_review_question(update, context, session_id)
    
    # Show definition in definition recall mode
    elif data.startswith('show_def_'):
        session_id = data.replace('show_def_', '')
        session = context.bot_data['vocab_bot'].session_manager.get_session(session_id)
        if not session:
            return
        
        current_word = session['data']['words'][session['data']['current_index']]
        
        keyboard = [
            [
                InlineKeyboardButton("😊 Easy (5)", callback_data=f"answer_correct_5_{session_id}"),
                InlineKeyboardButton("🙂 Good (4)", callback_data=f"answer_correct_4_{session_id}")
            ],
            [
                InlineKeyboardButton("😐 Hard (3)", callback_data=f"answer_correct_3_{session_id}"),
                InlineKeyboardButton("😞 Forgot (1)", callback_data=f"answer_incorrect_1_{session_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = (
            f"📝 **Question {session['data']['current_index'] + 1}/{len(session['data']['words'])}**\n\n"
            f"🔤 **{current_word['word'].upper()}**\n\n"
            f"📖 **Definition:** {current_word['definition']}\n\n"
            f"How well did you know this?"
        )
        
        await query.edit_message_text(
            message_text, parse_mode='Markdown', reply_markup=reply_markup
        )
    
    # Handle answers
    elif data.startswith('answer_') or data.startswith('choice_'):
        parts = data.split('_')
        result_type = parts[1]  # correct/incorrect
        confidence = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 3
        session_id = '_'.join(parts[3:]) if len(parts) > 3 else '_'.join(parts[2:])
        
        session = context.bot_data['vocab_bot'].session_manager.get_session(session_id)
        if not session:
            return
        
        data_session = session['data']
        current_index = data_session['current_index']
        current_word = data_session['words'][current_index]
        
        # Calculate response time
        response_time = time.time() - data_session.get('question_start_time', time.time())
        
        # Record result
        result = {
            'word_id': current_word['id'],
            'correct': result_type == 'correct',
            'confidence': confidence,
            'response_time': response_time,
            'word': current_word['word']
        }
        data_session['results'].append(result)
        
        # Move to next question
        data_session['current_index'] += 1
        context.bot_data['vocab_bot'].session_manager.update_session(session_id, data_session)
        
        await show_review_question(update, context, session_id)
    
    # Spelling mode handlers
    elif data.startswith('spell_'):
        action = data.split('_')[1]
        session_id = '_'.join(data.split('_')[2:])
        
        session = context.bot_data['vocab_bot'].session_manager.get_session(session_id)
        if not session:
            return
            
        current_word = session['data']['words'][session['data']['current_index']]
        
        if action == 'prompt':
            await query.edit_message_text(
                f"✍️ **Type the word for this definition:**\n\n"
                f"📖 {current_word['definition']}\n\n"
                f"Reply with your answer!",
                parse_mode='Markdown'
            )
            # Set flag to expect text input
            session['data']['expecting_spell_input'] = True
            context.bot_data['vocab_bot'].session_manager.update_session(session_id, session['data'])
            
        elif action == 'show':
            keyboard = [
                [
                    InlineKeyboardButton("✅ I knew it", callback_data=f"answer_correct_4_{session_id}"),
                    InlineKeyboardButton("❌ I didn't know", callback_data=f"answer_incorrect_1_{session_id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"📖 **Definition:** {current_word['definition']}\n\n"
                f"✅ **Answer:** {current_word['word']}\n\n"
                f"Did you know this?",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )

async def handle_spelling_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle spelling test input"""
    user_id = update.effective_user.id
    user_input = update.message.text.strip().lower()
    
    # Find active spelling session
    session_manager = context.bot_data['vocab_bot'].session_manager
    active_session = None
    active_session_id = None
    
    for session_id, session in session_manager.sessions.items():
        if (session['user_id'] == user_id and 
            session['type'] == 'review' and 
            session['data'].get('expecting_spell_input')):
            active_session = session
            active_session_id = session_id
            break
    
    if not active_session:
        return  # No active spelling session
    
    data_session = active_session['data']
    current_word = data_session['words'][data_session['current_index']]
    correct_word = current_word['word'].lower()
    
    # Check if spelling is correct (allow minor typos)
    is_correct = user_input == correct_word
    if not is_correct:
        # Simple fuzzy matching for typos
        import difflib
        similarity = difflib.SequenceMatcher(None, user_input, correct_word).ratio()
        is_correct = similarity >= 0.8  # 80% similarity threshold
    
    # Clear the input expectation flag
    data_session['expecting_spell_input'] = False
    
    # Calculate confidence based on accuracy
    confidence = 5 if user_input == correct_word else (4 if is_correct else 1)
    
    response_time = time.time() - data_session.get('question_start_time', time.time())
    
    # Record result
    result = {
        'word_id': current_word['id'],
        'correct': is_correct,
        'confidence': confidence,
        'response_time': response_time,
        'word': current_word['word']
    }
    data_session['results'].append(result)
    data_session['current_index'] += 1
    
    # Show feedback
    if is_correct:
        feedback = f"✅ **Correct!** {current_word['word']}"
        if user_input != correct_word:
            feedback += f"\n(You wrote: {user_input})"
    else:
        feedback = f"❌ **Incorrect!**\nYou wrote: {user_input}\nCorrect: {current_word['word']}"
    
    await update.message.reply_text(feedback, parse_mode='Markdown')
    
    # Continue to next question
    session_manager.update_session(active_session_id, data_session)
    
    # Small delay before next question
    await asyncio.sleep(1)
    await show_review_question(update, context, active_session_id)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enhanced stats with comprehensive analytics"""
    user_id = update.effective_user.id
    
    try:
        analytics = await context.bot_data['vocab_bot'].db.get_user_analytics(user_id)
        
        if not analytics or analytics['total_words'] == 0:
            await update.message.reply_text(
                "📊 **No vocabulary data yet!**\n\n"
                "Start adding words to see your progress! 🚀"
            )
            return
        
        # Create progress bar
        mastery_percentage = (analytics['mastered_words'] / analytics['total_words']) * 100
        progress_bar = "█" * int(mastery_percentage / 10) + "░" * (10 - int(mastery_percentage / 10))
        
        # Learning stage distribution
        stages_text = f"""
📚 **Learning Stages:**
🆕 New: {analytics['new_words']}
📖 Learning: {analytics['learning_words']}
🎯 Mastered: {analytics['mastered_words']}
        """
        
        # Performance metrics
        performance_text = f"""
📊 **Performance:**
✅ Success Rate: {analytics['success_rate']}%
🧠 Avg Difficulty: {analytics['avg_ease_factor']}/2.5
📝 Total Reviews: {analytics['total_reviews']}
🔥 Study Streak: {analytics['streak_days']} days
        """
        
        # Study recommendations
        if analytics['due_count'] > 0:
            recommendation = f"🎯 {analytics['due_count']} words ready for review!"
        elif analytics['success_rate'] < 70:
            recommendation = "💪 Focus on reviewing difficult words"
        elif analytics['new_words'] > analytics['mastered_words']:
            recommendation = "📚 Continue adding new vocabulary"
        else:
            recommendation = "🌟 Great progress! Keep it up!"
        
        stats_text = f"""
📊 **Your Vocabulary Analytics**

📈 **Progress: {mastery_percentage:.1f}%**
{progress_bar}

📋 **Overview:**
Total Words: **{analytics['total_words']}**
{stages_text}
{performance_text}

💡 **Recommendation:**
{recommendation}

🎓 Keep learning! Every word counts! ✨
        """
        
        await update.message.reply_text(stats_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error in stats_command: {e}")
        await update.message.reply_text("❌ Error getting stats. Please try again.")

async def cleanup_handler(context: ContextTypes.DEFAULT_TYPE):
    """Periodic cleanup of old sessions"""
    try:
        # Clean up old sessions
        if 'vocab_bot' in context.bot_data:
            context.bot_data['vocab_bot'].session_manager.cleanup_old_sessions()
        
        logger.info("Cleanup completed successfully")
    except Exception as e:
        logger.error(f"Error in cleanup: {e}")

async def backup_database(context: ContextTypes.DEFAULT_TYPE):
    """Create periodic database backups"""
    try:
        import shutil
        from datetime import datetime
        
        db_path = context.bot_data['vocab_bot'].db.db_path
        backup_path = f"vocabulary_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        
        shutil.copy2(db_path, backup_path)
        logger.info(f"Database backup created: {backup_path}")
        
        # Keep only last 5 backups
        import glob
        backups = sorted(glob.glob("vocabulary_backup_*.db"))
        if len(backups) > 5:
            for old_backup in backups[:-5]:
                os.remove(old_backup)
                logger.info(f"Removed old backup: {old_backup}")
                
    except Exception as e:
        logger.error(f"Error creating backup: {e}")

def main():
    """Main function to run the bot"""
    # Get environment variables
    telegram_token = os.getenv('TELEGRAM_TOKEN')
    
    if not telegram_token:
        raise ValueError("Missing required environment variable: TELEGRAM_TOKEN")
    
    # Create event loop for initialization
    async def initialize_and_run():
        # Initialize the vocabulary bot
        vocab_bot = VocabularyBot(telegram_token)
        await vocab_bot.initialize()
        
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
        
        # Note: Scheduled jobs disabled for simplified deployment
        # Manual cleanup can be triggered or run periodically in production
        logger.info("Bot starting with SQLite database...")
        logger.info("Note: Automatic cleanup and backup jobs are disabled in this version")
        
        # Start the bot
        await application.run_polling(allowed_updates=Update.ALL_TYPES)
    
    # Run the bot
    asyncio.run(initialize_and_run())

if __name__ == '__main__':
    main()
