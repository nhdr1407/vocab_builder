import os
import json
import logging
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import asyncio
from typing import Dict, List, Tuple, Optional
import random
import time
from functools import wraps

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
    
    def create_session(self, user_id: int, session_type: str, data: Dict) -> str:
        """Create a new session"""
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
        if session_id in self.sessions:
            self.sessions[session_id]['last_activity'] = datetime.now()
            return self.sessions[session_id]
        return None
    
    def update_session(self, session_id: str, data: Dict):
        """Update session data"""
        if session_id in self.sessions:
            self.sessions[session_id]['data'].update(data)
            self.sessions[session_id]['last_activity'] = datetime.now()
    
    def end_session(self, session_id: str):
        """End a session"""
        if session_id in self.sessions:
            del self.sessions[session_id]
    
    def cleanup_old_sessions(self, max_age_hours: int = 2):
        """Remove old inactive sessions"""
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

class VocabularyBot:
    def __init__(self, telegram_token: str, google_creds_json: str, sheet_name: str):
        self.telegram_token = telegram_token
        self.sheet_name = sheet_name
        self.session_manager = SessionManager()
        self._batch_operations = []
        self._last_batch_time = time.time()
        
        # Initialize Google Sheets with retry logic
        self._initialize_sheets(google_creds_json)
    
    def _initialize_sheets(self, google_creds_json: str):
        """Initialize Google Sheets connection"""
        try:
            scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
            creds_dict = json.loads(google_creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
            self.gc = gspread.authorize(creds)
            
            # Get or create the spreadsheet
            try:
                self.spreadsheet = self.gc.open(self.sheet_name)
                self.sheet = self.spreadsheet.worksheet('vocabulary')
            except gspread.SpreadsheetNotFound:
                self.spreadsheet = self.gc.create(self.sheet_name)
                self.spreadsheet.share('', perm_type='anyone', role='reader')
                self.sheet = self.spreadsheet.add_worksheet('vocabulary', 1000, 12)
                self._initialize_headers()
            except gspread.WorksheetNotFound:
                self.sheet = self.spreadsheet.add_worksheet('vocabulary', 1000, 12)
                self._initialize_headers()
                
            logger.info("Google Sheets initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Google Sheets: {e}")
            raise
    
    def _initialize_headers(self):
        """Initialize spreadsheet headers"""
        headers = [
            'Word', 'Definition', 'Date Added', 'Last Reviewed', 'Next Review',
            'Success Count', 'Failure Count', 'Interval Days', 'Ease Factor',
            'User ID', 'Total Reviews', 'Average Quality'
        ]
        self.sheet.insert_row(headers, 1)
    
    @retry_on_failure(max_retries=3, delay=2)
    async def _execute_batch_operations(self):
        """Execute batched operations for better performance"""
        if not self._batch_operations:
            return
        
        try:
            # Group operations by type
            updates = []
            appends = []
            
            for op in self._batch_operations:
                if op['type'] == 'update':
                    updates.append(op)
                elif op['type'] == 'append':
                    appends.append(op)
            
            # Execute batch updates
            if updates:
                cells_to_update = []
                for op in updates:
                    cells_to_update.append(gspread.Cell(
                        row=op['row'], col=op['col'], value=op['value']
                    ))
                if cells_to_update:
                    self.sheet.update_cells(cells_to_update)
            
            # Execute batch appends
            if appends:
                rows_to_add = [op['row'] for op in appends]
                if rows_to_add:
                    self.sheet.append_rows(rows_to_add)
            
            self._batch_operations.clear()
            logger.info(f"Executed batch operations: {len(updates)} updates, {len(appends)} appends")
            
        except Exception as e:
            logger.error(f"Batch operation failed: {e}")
            raise
    
    def _queue_operation(self, operation: Dict):
        """Queue an operation for batch execution"""
        self._batch_operations.append(operation)
        
        # Auto-execute if batch is large or time limit reached
        if (len(self._batch_operations) >= 10 or 
            time.time() - self._last_batch_time > 30):
            asyncio.create_task(self._execute_batch_operations())
            self._last_batch_time = time.time()
    
    @retry_on_failure(max_retries=3, delay=1)
    async def add_words_batch(self, words_data: List[Tuple[str, str]], user_id: int) -> Tuple[int, int]:
        """Add multiple words in batch"""
        try:
            now = datetime.now()
            next_review = now + timedelta(days=1)
            successful = 0
            failed = 0
            
            rows_to_add = []
            for word, definition in words_data:
                if word and definition:  # Skip empty entries
                    row = [
                        word.lower().strip(),
                        definition.strip(),
                        now.strftime('%Y-%m-%d %H:%M:%S'),
                        '',
                        next_review.strftime('%Y-%m-%d %H:%M:%S'),
                        0, 0, 1, 2.5, user_id, 0, 0
                    ]
                    rows_to_add.append(row)
                    successful += 1
                else:
                    failed += 1
            
            if rows_to_add:
                self.sheet.append_rows(rows_to_add)
            
            return successful, failed
            
        except Exception as e:
            logger.error(f"Error in batch add: {e}")
            return 0, len(words_data)
    
    @retry_on_failure(max_retries=3, delay=1)
    async def get_due_words(self, user_id: int, limit: int = 5) -> List[Dict]:
        """Get words due for review with improved performance"""
        try:
            # Get all records (consider pagination for very large datasets)
            records = self.sheet.get_all_records()
            now = datetime.now()
            due_words = []
            
            for i, record in enumerate(records, start=2):
                if record['User ID'] != user_id:
                    continue
                
                next_review_str = record['Next Review']
                if next_review_str:
                    try:
                        next_review = datetime.strptime(next_review_str, '%Y-%m-%d %H:%M:%S')
                        if next_review <= now:
                            record['row_number'] = i
                            due_words.append(record)
                    except ValueError:
                        continue
            
            # Sort by priority (overdue words first, then by ease factor)
            due_words.sort(key=lambda x: (
                datetime.strptime(x['Next Review'], '%Y-%m-%d %H:%M:%S'),
                x.get('Ease Factor', 2.5)
            ))
            
            return due_words[:limit]
            
        except Exception as e:
            logger.error(f"Error getting due words: {e}")
            return []
    
    async def update_word_progress_batch(self, updates: List[Dict]):
        """Update multiple word progress entries in batch"""
        try:
            cells_to_update = []
            
            for update_data in updates:
                row_number = update_data['row_number']
                correct = update_data['correct']
                confidence = update_data.get('confidence', 3)
                response_time = update_data.get('response_time', 5)
                
                # Get current record
                record = self.sheet.row_values(row_number)
                success_count = int(record[5]) if record[5] else 0
                failure_count = int(record[6]) if record[6] else 0
                interval_days = int(record[7]) if record[7] else 1
                ease_factor = float(record[8]) if record[8] else 2.5
                total_reviews = int(record[10]) if len(record) > 10 and record[10] else 0
                avg_quality = float(record[11]) if len(record) > 11 and record[11] else 0
                
                # Calculate quality score
                quality = SpacedRepetitionSM2.quality_from_performance(correct, confidence, response_time)
                
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
                
                # Queue updates
                cells_to_update.extend([
                    gspread.Cell(row_number, 4, now.strftime('%Y-%m-%d %H:%M:%S')),  # Last Reviewed
                    gspread.Cell(row_number, 5, next_review.strftime('%Y-%m-%d %H:%M:%S')),  # Next Review
                    gspread.Cell(row_number, 6, success_count),  # Success Count
                    gspread.Cell(row_number, 7, failure_count),  # Failure Count
                    gspread.Cell(row_number, 8, new_interval),  # Interval Days
                    gspread.Cell(row_number, 9, new_ease),  # Ease Factor
                    gspread.Cell(row_number, 11, total_reviews),  # Total Reviews
                    gspread.Cell(row_number, 12, round(avg_quality, 2)),  # Average Quality
                ])
            
            # Execute batch update
            if cells_to_update:
                self.sheet.update_cells(cells_to_update)
            
            return True
            
        except Exception as e:
            logger.error(f"Error in batch update: {e}")
            return False
    
    @retry_on_failure(max_retries=2, delay=1)
    async def get_user_analytics(self, user_id: int) -> Dict:
        """Get comprehensive user analytics"""
        try:
            records = self.sheet.get_all_records()
            user_words = [r for r in records if r['User ID'] == user_id]
            
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
            mastered_words = len([w for w in user_words if w['Success Count'] >= 5 and w['Ease Factor'] > 2.0])
            learning_words = len([w for w in user_words if 0 < w['Success Count'] < 5])
            new_words = len([w for w in user_words if w['Success Count'] == 0])
            
            # Get due words count
            due_words = await self.get_due_words(user_id, 1000)
            due_count = len(due_words)
            
            # Calculate averages
            avg_ease_factor = sum(w['Ease Factor'] for w in user_words) / total_words
            total_reviews = sum(w.get('Total Reviews', 0) for w in user_words)
            total_success = sum(w['Success Count'] for w in user_words)
            total_attempts = sum(w['Success Count'] + w['Failure Count'] for w in user_words)
            success_rate = (total_success / total_attempts * 100) if total_attempts > 0 else 0
            
            # Calculate learning streak (simplified)
            recent_reviews = [w for w in user_words if w['Last Reviewed']]
            streak_days = len(set(
                datetime.strptime(w['Last Reviewed'], '%Y-%m-%d %H:%M:%S').date()
                for w in recent_reviews
                if w['Last Reviewed']
            )) if recent_reviews else 0
            
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
        successful, failed = await context.bot_data['vocab_bot'].add_words_batch(words_to_add, user_id)
        
        response_text = f"✅ **Added {successful} word(s) successfully!**\n\n"
        
        if successful <= 3:  # Show details for small batches
            for word, definition in words_to_add[:successful]:
                response_text += f"📖 **{word}** - {definition}\n"
        
        if failed > 0 or invalid_lines:
            response_text += f"\n⚠️ {failed + len(invalid_lines)} line(s) had issues"
        
        response_text += f"\n🎯 Words will appear in your next review session!"
        
        await update.message.reply_text(response_text, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Error in add_word_handler: {e}")
        await update.message.reply_text("❌ Error adding words. Please try again later.")

async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enhanced review session with multiple modes"""
    user_id = update.effective_user.id
    
    # Clean up old sessions
    context.bot_data['vocab_bot'].session_manager.cleanup_old_sessions()
    
    due_words = await context.bot_data['vocab_bot'].get_due_words(user_id, 10)
    
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
    word = current_word['Word']
    definition = current_word['Definition']
    
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
        all_words = await context.bot_data['vocab_bot'].sheet.get_all_records()
        other_definitions = [w['Definition'] for w in all_words 
                           if w['Definition'] != definition and w['User ID'] == session['user_id']]
        
        if len(other_definitions) >= 3:
            choices = random.sample(other_definitions, 3) + [definition]
            random.shuffle(choices)
            correct_index = choices.index(definition)
            
            keyboard = []
            for i, choice in enumerate(choices):
                is_correct = (i == correct_index)
                callback_data = f"choice_{'correct' if is_correct else 'incorrect'}_{i}_{session_id}"
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
        await context.bot_data['vocab_bot'].update_word_progress_batch(results)
    
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
            f"🔤 **{current_word['Word'].upper()}**\n\n"
            f"📖 **Definition:** {current_word['Definition']}\n\n"
            f"How well did you know this?"
        )
        
        await query.edit_message_text(
            message_text, parse_mode='Markdown', reply_markup=reply_markup
        )
    
    # Handle answers
    elif data.startswith('answer_') or data.startswith('choice_'):
        parts = data.split('_')
        result_type = parts[1]  #
