import os
import json
import time
import sqlite3
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, Response
from openai import OpenAI
import pdfplumber
import docx2txt
from dotenv import load_dotenv
from collections import defaultdict
import logging
import re
import threading
import queue

# Setup logging
logging.basicConfig(level=logging.DEBUG)

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__, template_folder='.', static_folder='.')
app.secret_key = os.urandom(24)
os.makedirs('uploads', exist_ok=True)

# Initialize OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Global state management
questions = []
current_question = 0
evaluations = []
use_voice = False
asked_questions = set()
resume_questions = []
voice_answer_queue = queue.Queue()

# Interview context
interview_context = {
    'questions': [],
    'current_question_idx': 0,
    'previous_answers': [],
    'scores': [],
    'question_depth': 0,
    'max_depth': 2,
    'interview_track': None,
    'sub_track': None,
    'asked_questions': set(),
    'job_description': None
}

# Structure for organizing predefined questions
structure = {
    'mba': {
        'resume_flow': [],
        'school_based': defaultdict(list),
        'interest_areas': defaultdict(list)
    },
    'bank': {
        'resume_flow': [],
        'bank_type': defaultdict(list),
        'technical_analytical': defaultdict(list)
    }
}

# PDF paths
mba_pdf_path = "MBA_Question.pdf"
bank_pdf_path = "Bank_Question.pdf"

def normalize_text(text):
    """Normalize text by removing extra whitespace and converting to lowercase."""
    return " ".join(text.strip().split()).lower()

def strip_numbering(text):
    """Remove leading numbers (e.g., '1. ') from text."""
    return re.sub(r'^\d+\.\s*', '', text).strip()

def load_questions_into_memory(pdf_path, section_type):
    """Load predefined questions from a PDF file into memory."""
    if not os.path.exists(pdf_path):
        logging.error(f"PDF file '{pdf_path}' not found.")
        return False
    
    try:
        with pdfplumber.open(pdf_path) as pdf:
            full_text = ''.join(page.extract_text() or '' for page in pdf.pages)
        lines = full_text.split('\n')
        current_section = None
        current_subsection = None
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            if section_type == 'mba':
                if "1. Resume Flow" in line:
                    current_section = 'resume_flow'
                    current_subsection = None
                    logging.debug("Switched to Resume Flow")
                    continue
                elif "2. Pre-Defined Question Selection" in line:
                    current_section = 'school_based'
                    current_subsection = None
                    logging.debug("Switched to School Based")
                    continue
                elif "3. Interface to Select Question Areas" in line:
                    current_section = 'interest_areas'
                    current_subsection = None
                    logging.debug("Switched to Interest Areas")
                    continue
                
                if current_section == 'school_based':
                    if "For IIMs" in line:
                        current_subsection = 'IIM'
                        logging.debug("Switched to IIM")
                        continue
                    elif "For ISB" in line:
                        current_subsection = 'ISB'
                        logging.debug("Switched to ISB")
                        continue
                    elif "For Other B-Schools" in line:
                        current_subsection = 'Other'
                        logging.debug("Switched to Other B-Schools")
                        continue
                
                if current_section == 'interest_areas':
                    if "General Business & Leadership" in line:
                        current_subsection = 'General Business'
                        logging.debug("Switched to General Business")
                        continue
                    elif "Finance & Economics" in line:
                        current_subsection = 'Finance'
                        logging.debug("Switched to Finance")
                        continue
                    elif "Marketing & Strategy" in line:
                        current_subsection = 'Marketing'
                        logging.debug("Switched to Marketing")
                        continue
                    elif "Operations & Supply Chain" in line:
                        current_subsection = 'Operations'
                        logging.debug("Switched to Operations")
                        continue
            
            elif section_type == 'bank':
                if "Resume-Based Questions" in line:
                    current_section = 'resume_flow'
                    current_subsection = None
                    logging.debug("Switched to Resume-Based Questions")
                    continue
                elif "Bank-Type Specific Questions" in line:
                    current_section = 'bank_type'
                    current_subsection = None
                    logging.debug("Switched to Bank-Type Specific Questions")
                    continue
                elif "Technical & Analytical Questions" in line:
                    current_section = 'technical_analytical'
                    current_subsection = None
                    logging.debug("Switched to Technical & Analytical Questions")
                    continue
                elif "Current Affairs" in line:
                    current_section = 'technical_analytical'
                    current_subsection = 'Current Affairs'
                    logging.debug("Switched to Current Affairs")
                    continue
                
                if current_section == 'bank_type':
                    if "Public Sector Banks" in line:
                        current_subsection = 'Public Sector Banks'
                        logging.debug("Switched to Public Sector Banks")
                        continue
                    elif "Private Banks" in line:
                        current_subsection = 'Private Banks'
                        logging.debug("Switched to Private Banks")
                        continue
                    elif "Regulatory Roles" in line:
                        current_subsection = 'Regulatory Roles'
                        logging.debug("Switched to Regulatory Roles")
                        continue
                
                if current_section == 'technical_analytical' and current_subsection != 'Current Affairs':
                    if "Banking Knowledge" in line:
                        current_subsection = 'Banking Knowledge'
                        logging.debug("Switched to Banking Knowledge")
                        continue
                    elif "Logical Reasoning" in line:
                        current_subsection = 'Logical Reasoning'
                        logging.debug("Switched to Logical Reasoning")
                        continue
                    elif "Situational Judgement" in line:
                        current_subsection = 'Situational Judgement'
                        logging.debug("Switched to Situational Judgement")
                        continue
            
            if line and line[0].isdigit() and '.' in line.split()[0]:
                question = strip_numbering(line)
                is_sequence = bool(re.search(r'\d+,\d+,\d+.*,_', question))
                question_data = {'text': question, 'type': 'sequence' if is_sequence else 'standard'}
                if len(question.split()) > 15 and not is_sequence:
                    question_data['text'] = ' '.join(question.split()[:15]) + '?'
                if not question_data['text'].endswith('?'):
                    question_data['text'] += '?'
                if current_section == 'resume_flow':
                    structure[section_type]['resume_flow'].append(question_data)
                    logging.debug(f"Added to {section_type}.resume_flow: {question_data}")
                elif current_section in ['school_based', 'bank_type', 'technical_analytical'] and current_subsection:
                    structure[section_type][current_section][current_subsection].append(question_data)
                    logging.debug(f"Added to {section_type}.{current_section}[{current_subsection}]: {question_data}")
        
        logging.info(f"Loaded predefined questions for {section_type}: "
                     f"resume_flow={len(structure[section_type]['resume_flow'])}, "
                     f"school_based={dict(structure[section_type].get('school_based', {}))}, "
                     f"interest_areas={dict(structure[section_type].get('interest_areas', {}))}, "
                     f"bank_type={dict(structure[section_type].get('bank_type', {}))}, "
                     f"technical_analytical={dict(structure[section_type].get('technical_analytical', {}))}")
        return True
    except Exception as e:
        logging.error(f"Error loading questions from {pdf_path}: {e}")
        return False

# Load questions at startup and provide fallback if loading fails
if not load_questions_into_memory(mba_pdf_path, 'mba'):
    logging.error("Failed to load MBA questions. Using fallback questions.")
    structure['mba']['school_based']['IIM'] = [
        {'text': "Why pursue an MBA at IIM?", 'type': 'standard'},
        {'text': "What are your career goals?", 'type': 'standard'},
        {'text': "How does IIM’s curriculum help you?", 'type': 'standard'},
        {'text': "How will you contribute at IIM?", 'type': 'standard'},
        {'text': "Which specialization interests you?", 'type': 'standard'}
    ]

if not load_questions_into_memory(bank_pdf_path, 'bank'):
    logging.error("Failed to load banking questions. Using fallback questions.")
    structure['bank']['resume_flow'] = [
        {'text': "Walk us through your resume?", 'type': 'standard'},
        {'text': "What strengths fit banking?", 'type': 'standard'},
        {'text': "Why transition to banking?", 'type': 'standard'}
    ]
    structure['bank']['bank_type']['Public Sector Banks'] = [
        {'text': "Why choose a public sector bank?", 'type': 'standard'},
        {'text': "How promote financial inclusion?", 'type': 'standard'},
        {'text': "Explain PM Jan Dhan Yojana?", 'type': 'standard'}
    ]
    structure['bank']['bank_type']['Private Banks'] = [
        {'text': "Why join a private bank?", 'type': 'standard'},
        {'text': "How achieve sales targets?", 'type': 'standard'},
        {'text': "How resolve customer complaints?", 'type': 'standard'}
    ]
    structure['bank']['bank_type']['Regulatory Roles'] = [
        {'text': "Explain RBI monetary policy tools?", 'type': 'standard'},
        {'text': "How does repo rate affect inflation?", 'type': 'standard'},
        {'text': "What’s RBI’s Digital Rupee stance?", 'type': 'standard'}
    ]
    structure['bank']['technical_analytical']['Banking Knowledge'] = [
        {'text': "Define CASA ratio?", 'type': 'standard'},
        {'text': "How is NIM calculated?", 'type': 'standard'},
        {'text': "Explain NPA categories?", 'type': 'standard'}
    ]
    structure['bank']['technical_analytical']['Logical Reasoning'] = [
        {'text': "Complete series: 2,5,10,17,26,?", 'type': 'sequence'},
        {'text': "Next in series: A,D,I,P,?", 'type': 'standard'},
        {'text': "Odd one out: 14,28,49,65,98?", 'type': 'standard'}
    ]
    structure['bank']['technical_analytical']['Situational Judgement'] = [
        {'text': "Resolve a disputed transaction?", 'type': 'standard'},
        {'text': "Politely reject a loan application?", 'type': 'standard'},
        {'text': "Handle an irate customer?", 'type': 'standard'}
    ]
    structure['bank']['technical_analytical']['Current Affairs'] = [
        {'text': "How AI transforms banking?", 'type': 'standard'},
        {'text': "Discuss RBI digital lending guidelines?", 'type': 'standard'},
        {'text': "Impact of rising repo rates?", 'type': 'standard'}
    ]

def generate_resume_questions(resume_text, job_type):
    """Generate interview questions based on resume text."""
    if not resume_text:
        logging.warning("Empty resume text provided.")
        return ["Tell me about yourself?"]
    
    prompt = f"""Based on the following resume, generate 15 unique and relevant {'MBA' if job_type == 'mba' else 'banking'} interview questions tailored to the candidate's experience and background. Each question should be a complete sentence, concise, and end with a question mark. Avoid truncating questions mid-sentence.

Resume: {resume_text}"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=7000
        )
        questions_text = response.choices[0].message.content
        logging.debug(f"Raw questions text: {questions_text}")
        questions = [strip_numbering(q.strip()) for q in questions_text.split('\n') if q.strip() and q not in asked_questions]
        questions = [q if q.endswith('?') else q + '?' for q in questions]
        questions = [q for q in questions if len(q.split()) >= 3 and len(q.split()) <= 25 and q.endswith('?')]
        logging.debug(f"Generated resume questions for {job_type}: {questions}")
        if not questions or len(questions) < 7:
            logging.warning(f"Insufficient questions generated for {job_type}. Using fallback questions.")
            if job_type == 'mba':
                questions = [
                    "What’s your biggest career achievement?",
                    "What skills do you bring to MBA?",
                    "Describe a challenge in your last role?",
                    "Why did you choose your career path?",
                    "How has your experience prepared you?",
                    "What motivates you to pursue an MBA?",
                    "How do you handle team conflicts?",
                    "What’s your leadership style?",
                    "How do you stay updated on trends?",
                    "What’s your long-term career vision?"
                ]
            else:
                questions = [
                    "What’s your biggest banking achievement?",
                    "What skills do you bring to banking?",
                    "Describe a banking challenge faced?",
                    "Why choose a banking career?",
                    "How has experience prepared you?",
                    "What motivates you for banking?",
                    "How do you handle customer issues?",
                    "What’s your approach to sales targets?",
                    "How do you stay updated on banking?",
                    "What’s your long-term banking goal?"
                ]
        return questions[:10]
    except Exception as e:
        logging.error(f"Error generating resume questions for {job_type}: {e}")
        return [
            f"What motivated you to apply for {'MBA' if job_type == 'mba' else 'banking'}?",
            "Can you walk me through your career?",
            f"What’s a key {'MBA' if job_type == 'mba' else 'banking'} lesson?"
        ]

def evaluate_sequence_response(question, answer):
    """Evaluate answers to sequence questions."""
    if "2,5,10,17,26" in question:
        correct_answer = "38"
        try:
            user_answer = str(answer).strip()
            if user_answer == correct_answer:
                return "[Correct answer provided] Score: 10/10", 10
            else:
                return f"[Incorrect answer, expected {correct_answer}] Score: 0/10", 0
        except Exception as e:
            logging.error(f"Error evaluating sequence answer: {e}")
            return "[Invalid answer format] Score: 0/10", 0
    return "[Sequence evaluation not implemented] Score: 5/10", 5

def evaluate_response(question, answer, job_description):
    """Evaluate the candidate's answer using the OpenAI model or sequence evaluation."""
    is_sequence = bool(re.search(r'\d+,\d+,\d+.*,_', question))
    if is_sequence:
        return evaluate_sequence_response(question, answer)
    
    def fallback_evaluation(question, answer):
        answer = answer.lower().strip()
        if len(answer) < 5 or not any(c.isalpha() for c in answer):
            return "[Answer is irrelevant or gibberish] Score: 0/10", 0
        
        question_keywords = set(normalize_text(question).split())
        answer_keywords = set(normalize_text(answer).split())
        common_keywords = question_keywords.intersection(answer_keywords)
        
        if not common_keywords:
            return "[Answer is irrelevant to the question] Score: 0/10", 0
        
        score = min(10, max(3, len(answer.split()) // 5))
        feedback = "[Answer is relevant but could use more detail]" if score < 7 else "[Answer is relevant and detailed]"
        return f"{feedback} Score: {score}/10", score

    evaluation_prompt = f"""Evaluate the following answer for the question in the context of a {job_description} role. Assess relevance, depth, and insightfulness.

Question: {question}
Answer: {answer}

Provide feedback and a score out of 10:
- 0: Completely irrelevant, gibberish, or no answer.
- 0-1: Barely relevant, lacks substance.
- 2-6: Somewhat relevant, basic understanding, limited detail.
- 7-8: Relevant, good understanding, decent detail.
- 9-10: Highly relevant, detailed, insightful.

Ensure the score reflects the answer's quality relative to the question. Format: '[Feedback] Score: X/10'"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": evaluation_prompt}],
            temperature=0.5,
            max_tokens=150
        )
        evaluation_text = response.choices[0].message.content.strip()
        score_match = re.search(r'Score:\s*(\d+)/10', evaluation_text)
        score = int(score_match.group(1)) if score_match else 5
        return evaluation_text, score
    except Exception as e:
        logging.error(f"Error in OpenAI evaluation: {e}")
        return fallback_evaluation(question, answer)

def generate_next_question(question, answer, score, interview_track, job_type, attempt=1):
    """Generate a related question based on the interview type."""
    if attempt > 2:
        return None
    
    prompt = f"""Given the question and answer below for a {'MBA' if job_type == 'mba' else 'banking'} candidate interview (score: {score}/10), generate a related question. The question should be a complete sentence, concise, and end with a question mark. Focus on {'experience, skills, or career goals' if interview_track == 'resume' else 'academic motivations, school fit, or contributions' if interview_track == 'school_based' else 'passion, knowledge, or application' if interview_track == 'interest_areas' else 'banking operations, customer service, or regulatory knowledge' if interview_track == 'bank_type' else 'technical banking knowledge, logical reasoning, situational judgement, or current banking affairs' if interview_track == 'technical_analytical' else 'relevance'}.

Question: {question}
Answer: {answer}
Score: {score}/10"""
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=500
        )
        next_question = response.choices[0].message.content.strip()
        next_question = strip_numbering(next_question)
        if not next_question.endswith('?'):
            next_question += '?'
        if len(next_question.split()) > 25:
            next_question = ' '.join(next_question.split()[:25]) + '?'
        if next_question in asked_questions or not next_question or len(next_question.split()) < 3:
            if attempt == 1:
                if job_type == 'mba':
                    if interview_track == "resume":
                        return "How did that shape your career goals?"
                    elif interview_track == "school_based":
                        return "How does this align with your school choice?"
                    elif interview_track == "interest_areas":
                        return "Why is this area important to you?"
                    return "Can you elaborate further?"
                else:
                    if interview_track == "resume":
                        return "How did that shape your banking goals?"
                    elif interview_track == "bank_type":
                        return "How does this align with bank operations?"
                    elif interview_track == "technical_analytical":
                        return "Can you apply this to a banking scenario?"
                    return "Can you elaborate further?"
            return None
        logging.debug(f"Generated related question: {next_question}")
        return next_question
    except Exception as e:
        logging.error(f"Error generating related question: {e}")
        if attempt == 1:
            if job_type == 'mba':
                if interview_track == "resume":
                    return "What skills did you gain from that?"
                elif interview_track == "school_based":
                    return "How will this help you at the school?"
                elif interview_track == "interest_areas":
                    return "How do you plan to pursue this interest?"
                return "Can you provide more details?"
            else:
                if interview_track == "resume":
                    return "What banking skills did you gain?"
                elif interview_track == "bank_type":
                    return "How will this help in banking?"
                elif interview_track == "technical_analytical":
                    return "How do you analyze banking data?"
                return "Can you provide more details?"
        return None

def generate_conversational_reply(answer, job_type):
    """Generate a friendly, human-like reply to the candidate's answer, ensuring no questions are included."""
    system_prompt = f"As a friendly {'HR' if job_type == 'mba' else 'banking HR'} interviewer, generate a short, complete sentence as a reply to the candidate’s answer. Keep it engaging and human-like, and ensure it's a full thought. The reply must be a statement (ending with a period or exclamation mark) and must not contain any questions (do not end with a question mark). Provide only feedback or encouragement without asking for further information."
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": answer}
            ],
            temperature=0.8,
            max_tokens=5000
        )
        reply = response.choices[0].message.content.strip()
        # Ensure the reply ends with a period or exclamation mark, and not a question mark
        if reply.endswith('?'):
            reply = reply[:-1] + '.'
        elif not reply.endswith(('.', '!')):
            reply += '.'
        # Double-check for any question marks in the middle of the reply
        if '?' in reply:
            reply = reply.replace('?', '.')  # Replace any question marks with periods
        return reply
    except Exception as e:
        logging.error(f"Error generating reply: {e}")
        return "Thanks for your response."

def wait_for_silence(timeout=10):
    """Wait for voice input with a timeout to prevent infinite loops."""
    silence_start = None
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            answer = voice_answer_queue.get_nowait()
            silence_start = None
            return answer
        except queue.Empty:
            if silence_start is None:
                silence_start = time.time()
            elif time.time() - silence_start >= timeout:
                logging.warning("No voice input received after timeout.")
                return None
            time.sleep(0.1)
    logging.warning("Voice input timeout reached.")
    return None

def authenticate_user(username, password):
    """Authenticate user against users.db and return allowed type."""
    try:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute('SELECT Allowed FROM users WHERE Username = ? AND password = ?', (username, password))
        result = cursor.fetchone()
        conn.close()
        if result:
            return result[0]
        return None
    except Exception as e:
        logging.error(f"Error accessing users.db: {e}")
        return None

@app.route('/')
def index():
    if 'allowed' not in session:
        return redirect(url_for('login'))
    return render_template('index.html')

@app.route('/login.html')
def login():
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login_post():
    username = request.form.get('username')
    password = request.form.get('password')
    allowed = authenticate_user(username, password)
    if allowed:
        session['allowed'] = allowed
        return jsonify({'success': True, 'allowed': allowed})
    return jsonify({'success': False, 'error': 'Invalid username or password'}), 401

@app.route('/logout')
def logout():
    session.pop('allowed', None)
    return redirect(url_for('login'))

@app.route('/start_interview', methods=['POST'])
def start_interview():
    global questions, current_question, evaluations, use_voice, asked_questions, resume_questions, interview_context
    
    if 'allowed' not in session:
        return jsonify({"error": "Unauthorized access. Please log in."}), 401
    
    allowed = session['allowed']
    language = request.form['language']
    mode = request.form['mode']
    interview_track = request.form['interview_track']
    sub_track = request.form.get('sub_track', '')
    use_voice = mode == 'voice'
    resume_file = request.files.get('resume')

    if not resume_file:
        return jsonify({"error": "Resume file is required"}), 400
    
    if allowed == 'MBA' and interview_track in ['bank_type', 'technical_analytical']:
        return jsonify({"error": "Access denied. You are only allowed to access MBA interviews."}), 403
    if allowed == 'Bank' and interview_track in ['school_based', 'interest_areas']:
        return jsonify({"error": "Access denied. You are only allowed to access Banking interviews."}), 403
    
    resume_path = os.path.join('uploads', resume_file.filename)
    resume_file.save(resume_path)
    resume_text = ""
    if resume_path.lower().endswith('.pdf'):
        with pdfplumber.open(resume_path) as pdf:
            resume_text = ''.join(page.extract_text() or '' for page in pdf.pages)
    elif resume_path.lower().endswith('.docx'):
        resume_text = docx2txt.process(resume_path)
    else:
        os.remove(resume_path)
        return jsonify({"error": "Unsupported file format"}), 400
    os.remove(resume_path)

    job_type = 'mba' if allowed == 'MBA' else 'bank'
    job_description = "MBA Candidate" if job_type == 'mba' else "Banking Candidate"

    questions = []
    current_question = 0
    evaluations = []
    asked_questions = set()
    
    resume_questions = generate_resume_questions(resume_text, job_type)
    asked_questions.update(resume_questions)
    
    if job_type == 'mba':
        if interview_track == "resume":
            predefined_questions = [q['text'] for q in structure['mba']['resume_flow'][:3]]
            questions = resume_questions + [q for q in predefined_questions if q not in resume_questions]
        elif interview_track == "school_based":
            school_questions = [q['text'] for q in structure['mba']['school_based'][sub_track]] if sub_track in structure['mba']['school_based'] else [q['text'] for sublist in structure['mba']['school_based'].values() for q in sublist]
            questions = resume_questions[:5] + school_questions
        elif interview_track == "interest_areas":
            interest_questions = [q['text'] for q in structure['mba']['interest_areas'][sub_track]] if sub_track in structure['mba']['interest_areas'] else [q['text'] for sublist in structure['mba']['interest_areas'].values() for q in sublist]
            questions = resume_questions[:5] + interest_questions
    else:
        if interview_track == "resume":
            predefined_questions = [q['text'] for q in structure['bank']['resume_flow'][:3]]
            questions = resume_questions + [q for q in predefined_questions if q not in resume_questions]
        elif interview_track == "bank_type":
            bank_questions = [q['text'] for q in structure['bank']['bank_type'][sub_track]] if sub_track in structure['bank']['bank_type'] else [q['text'] for sublist in structure['bank']['bank_type'].values() for q in sublist]
            questions = resume_questions[:5] + bank_questions
        elif interview_track == "technical_analytical":
            technical_questions = [q['text'] for q in structure['bank']['technical_analytical'][sub_track]] if sub_track in structure['bank']['technical_analytical'] else [q['text'] for sublist in structure['bank']['technical_analytical'].values() for q in sublist]
            questions = resume_questions[:5] + technical_questions

    questions = [strip_numbering(q) for q in questions if q not in asked_questions]
    asked_questions.update(questions)

    if not questions:
        return jsonify({"error": f"No questions available for the selected track: {interview_track} - {sub_track}"}), 400
    
    interview_context.update({
        'questions': questions,
        'current_question_idx': 0,
        'previous_answers': [],
        'scores': [],
        'question_depth': 0,
        'max_depth': 2,
        'interview_track': interview_track,
        'sub_track': sub_track,
        'asked_questions': asked_questions,
        'job_description': job_description
    })
    
    return jsonify({
        "message": f"Starting {job_type} interview",
        "total_questions": len(questions),
        "current_question": questions[0],
        "question_number": 1,
        "use_voice": use_voice
    })

@app.route('/submit_answer', methods=['POST'])
def submit_answer():
    global current_question, evaluations, questions, asked_questions, interview_context
    
    if 'allowed' not in session:
        return jsonify({"error": "Unauthorized access. Please log in."}), 401
    
    job_type = 'mba' if session['allowed'] == 'MBA' else 'bank'
    
    if use_voice:
        answer = wait_for_silence()
        if answer is None:
            answer = "No response provided after 10 seconds of silence."
    else:
        answer = request.json.get('answer', "No response provided")
    
    main_question = questions[current_question]
    interview_track = interview_context["interview_track"]
    
    question_type = 'standard'
    for section in structure[job_type].values():
        subsections = section.values() if isinstance(section, dict) else [section]
        for subsection in subsections:
            for q in subsection:
                if isinstance(q, dict) and q.get('text') == main_question and q.get('type') == 'sequence':
                    question_type = 'sequence'
                    break
    
    if main_question in resume_questions:
        category = "resume"
    elif main_question in [q['text'] for q in structure[job_type]['resume_flow']]:
        category = "personal"
    else:
        category = "sequence" if question_type == 'sequence' else "predefined" if job_type == 'mba' else "technical"
    
    reply = generate_conversational_reply(answer, job_type)
    evaluation, score = evaluate_response(main_question, answer, interview_context["job_description"])
    
    evaluations.append({
        "question": main_question,
        "answer": answer,
        "evaluation": evaluation,
        "score": score,
        "category": category
    })
    interview_context["previous_answers"].append(answer)
    interview_context["scores"].append(score)
    interview_context["current_question_idx"] = current_question

    if interview_context["question_depth"] < interview_context["max_depth"]:
        next_question = generate_next_question(main_question, answer, score, interview_track, job_type)
        if not next_question and interview_context["question_depth"] == 0:
            next_question = "Can you elaborate on that?"
        if next_question and next_question not in asked_questions:
            questions.insert(current_question + 1, next_question)
            asked_questions.add(next_question)
            interview_context["question_depth"] += 1
            current_question += 1
            return jsonify({
                "reply": reply,
                "current_question": next_question,
                "question_number": current_question + 1,
                "total_questions": len(questions),
                "next_question": True
            })
    
    interview_context["question_depth"] = 0
    current_question += 1
    if current_question < len(questions):
        return jsonify({
            "reply": reply,
            "current_question": questions[current_question],
            "question_number": current_question + 1,
            "total_questions": len(questions),
            "next_question": True
        })
    else:
        personal_count = len([q for q in questions if q in [q['text'] for q in structure[job_type]['resume_flow']]])
        technical_count = len(questions) - personal_count
        overall_score = calculate_overall_score(evaluations, personal_count, technical_count)
        return jsonify({
            "reply": "Thanks for the chat! That’s all for today.",
            "finished": True,
            "evaluations": evaluations,
            "overall_score": overall_score
        })

def calculate_overall_score(evaluations, personal_count, technical_count):
    """Calculate the overall score based on evaluations."""
    if not evaluations or (personal_count + technical_count == 0):
        return 0
    total_score = sum(e["score"] for e in evaluations)
    return round((total_score / (len(evaluations) * 10)) * 100, 2)

@app.route('/submit_voice_answer', methods=['POST'])
def submit_voice_answer():
    """Simulate submitting a voice answer to the queue."""
    if 'allowed' not in session:
        return jsonify({"error": "Unauthorized access. Please log in."}), 401
    
    answer = request.json.get('answer', "No response provided")
    voice_answer_queue.put(answer)
    return jsonify({"message": "Voice answer received"})

@app.route('/generate_speech', methods=['POST'])
def generate_speech():
    """Generate speech using OpenAI TTS model and return full audio."""
    if 'allowed' not in session:
        return jsonify({"error": "Unauthorized access. Please log in."}), 401

    data = request.json
    text = data.get('text', '')
    voice = data.get('voice', 'sage')  # Default to nova
    language = data.get('language', 'en-IN')

    if not text:
        return jsonify({"error": "Text is required"}), 400

    # Validate voice
    supported_voices = ['sage', 'nova','alloy','echo']
    if voice not in supported_voices:
        return jsonify({
            "error": f"Invalid voice: '{voice}'. Supported voices are: {', '.join(supported_voices)}"
        }), 400

    try:
        start_time = time.time()
        response = client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice=voice,
            input=text,
            instructions="""Voice: Natural Indian English—clear and expressive, with the rhythm and tone typical of an educated Indian speaker.Phrasing: Use familiar Indian speech patterns—slightly sing-song intonation, smooth flow, and gentle emphasis on important words, like in everyday conversation.Punctuation: Pause where an Indian speaker naturally would—slightly longer at commas and sentence ends, with a touch of emotion or inflection when needed.Tone: Friendly, confident, and relatable—like you're explaining something to a friend or colleague, using the warmth and ease found in Indian conversations.""",
            response_format="mp3"
        )
        # Read the full audio content
        audio_content = response.content
        logging.info(f"Audio generated in {time.time() - start_time:.2f} seconds")
        return Response(audio_content, mimetype='audio/mp3')
    except Exception as e:
        logging.error(f"Error generating speech: {e}")
        return jsonify({"error": "Failed to generate speech"}), 500

if __name__ == "__main__":
    app.run(debug=True, port=5000)