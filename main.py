import os
import json
import time
from flask import Flask, render_template, request, jsonify
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
os.makedirs('uploads', exist_ok=True)

# Initialize OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Global state management
questions = []
current_question = 0
evaluations = []
job_description = "MBA Candidate"
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
    'question_depth': 0,  # Renamed from follow_up_depth
    'max_depth': 2,       # Renamed from max_follow_ups
    'interview_track': None,
    'sub_track': None,
    'asked_questions': set()
}

# Structure for organizing predefined questions
structure = {
    'resume_flow': [],
    'school_based': defaultdict(list),
    'interest_areas': defaultdict(list)
}

# PDF path for MBA questions
pdf_path = "MBA_Question.pdf"

def normalize_text(text):
    """Normalize text by removing extra whitespace and converting to lowercase."""
    return " ".join(text.strip().split()).lower()

def strip_numbering(text):
    """Remove leading numbers (e.g., '1. ') from text."""
    return re.sub(r'^\d+\.\s*', '', text).strip()

def load_questions_into_memory():
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
            
            elif current_section == 'school_based':
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
            
            elif current_section == 'interest_areas':
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
            
            if line and line[0].isdigit() and '.' in line.split()[0]:
                question = strip_numbering(line)
                if len(question.split()) > 15:
                    question = ' '.join(question.split()[:15]) + '?'
                if not question.endswith('?'):
                    question += '?'
                if current_section == 'resume_flow':
                    structure['resume_flow'].append(question)
                    logging.debug(f"Added to resume_flow: {question}")
                elif current_section == 'school_based' and current_subsection:
                    structure['school_based'][current_subsection].append(question)
                    logging.debug(f"Added to school_based[{current_subsection}]: {question}")
                elif current_section == 'interest_areas' and current_subsection:
                    structure['interest_areas'][current_subsection].append(question)
                    logging.debug(f"Added to interest_areas[{current_subsection}]: {question}")
        
        logging.info(f"Loaded predefined questions: resume_flow={len(structure['resume_flow'])}, "
                     f"school_based={dict(structure['school_based'])}, "
                     f"interest_areas={dict(structure['interest_areas'])}")
        return True
    except Exception as e:
        logging.error(f"Error loading questions from PDF: {e}")
        return False

# Load questions at startup and provide fallback if loading fails
if not load_questions_into_memory():
    logging.error("Failed to load questions at startup. Using fallback questions.")
    structure['school_based']['IIM'] = [
        "Why pursue an MBA at IIM?",
        "What are your career goals?",
        "How does IIM’s curriculum help you?",
        "How will you contribute at IIM?",
        "Which specialization interests you?"
    ]

def generate_resume_questions(resume_text):
    """Generate interview questions based on resume text."""
    if not resume_text:
        logging.warning("Empty resume text provided.")
        return ["Tell me about yourself?"]
    
    prompt = f"Based on the following resume, generate 10 unique and relevant interview questions tailored to the candidate's experience and background. Each question should be concise (less than 15 words) and end with a question mark:\n\n{resume_text}"
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=500
        )
        questions_text = response.choices[0].message.content
        questions = [strip_numbering(q.strip()) for q in questions_text.split('\n') if q.strip() and q not in asked_questions]
        questions = [q if q.endswith('?') else q + '?' for q in questions]
        questions = [q if len(q.split()) <= 15 else ' '.join(q.split()[:15]) + '?' for q in questions]
        logging.debug(f"Generated resume questions: {questions}")
        if not questions or len(questions) < 7:
            logging.warning("Insufficient or no valid questions generated from resume.")
            questions = [
                "What’s your biggest career achievement?",
                "What skills do you bring to MBA?",
                "Describe a challenge in your last role?",
                "Why did you choose your career path?",
                "How has your experience prepared you?"
            ]
        return questions[:10]
    except Exception as e:
        logging.error(f"Error generating resume questions: {e}")
        return [
            "What motivated you to apply for MBA?",
            "Can you walk me through your career?",
            "What’s a key lesson from your experience?"
        ]

def evaluate_response(question, answer, job_description):
    """Evaluate the candidate's answer using the OpenAI model with a fallback."""
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

def generate_next_question(question, answer, score, interview_track, attempt=1):
    """Generate a related question based on the interview type."""
    if attempt > 2:
        return None
    
    if interview_track == "resume":
        prompt = f"""Given the question and answer below for a resume-based MBA candidate interview (score: {score}/10), generate a related question focusing on the candidate's experience, skills, or career goals.

Question: {question}
Answer: {answer}
Score: {score}/10"""
    elif interview_track == "school_based":
        prompt = f"""Given the question and answer below for a school-based MBA candidate interview (score: {score}/10), generate a related question focusing on the candidate's academic motivations, school fit, or contributions.

Question: {question}
Answer: {answer}
Score: {score}/10"""
    elif interview_track == "interest_areas":
        prompt = f"""Given the question and answer below for an interest-area-based MBA candidate interview (score: {score}/10), generate a related question focusing on the candidate's passion, knowledge, or application in that area.

Question: {question}
Answer: {answer}
Score: {score}/10"""
    else:
        prompt = f"""Given the question and answer below for an MBA candidate interview (score: {score}/10), generate a relevant related question.

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
        if len(next_question.split()) > 15:
            next_question = ' '.join(next_question.split()[:15]) + '?'
        if not next_question.endswith('?'):
            next_question += '?'
        if next_question in asked_questions or not next_question:
            if attempt == 1:
                if interview_track == "resume":
                    return "How did that shape your career goals?"
                elif interview_track == "school_based":
                    return "How does this align with your school choice?"
                elif interview_track == "interest_areas":
                    return "Why is this area important to you?"
                return "Can you elaborate further?"
            return None
        logging.debug(f"Generated related question: {next_question}")
        return next_question
    except Exception as e:
        logging.error(f"Error generating related question: {e}")
        if attempt == 1:
            if interview_track == "resume":
                return "What skills did you gain from that?"
            elif interview_track == "school_based":
                return "How will this help you at the school?"
            elif interview_track == "interest_areas":
                return "How do you plan to pursue this interest?"
            return "Can you provide more details?"
        return None

def generate_conversational_reply(answer):
    """Generate a friendly, human-like reply to the candidate's answer."""
    system_prompt = "As a friendly HR interviewer, generate a short, complete sentence as a reply to the candidate’s answer. Keep it engaging and human-like, and ensure it's a full thought."
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": answer}
            ],
            temperature=0.8,
            max_tokens=50
        )
        reply = response.choices[0].message.content.strip()
        if not reply.endswith(('.', '!', '?')):
            reply += '.'
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

@app.route('/submit_answer', methods=['POST'])
def submit_answer():
    """Handle submission of an answer and proceed with the interview."""
    global current_question, evaluations, questions, asked_questions, interview_context
    
    if use_voice:
        answer = wait_for_silence()
        if answer is None:
            answer = "No response provided after 10 seconds of silence."
    else:
        answer = request.json.get('answer', "No response provided")
    
    main_question = questions[current_question]
    interview_track = interview_context["interview_track"]
    
    if main_question in resume_questions:
        category = "resume"
    elif main_question in structure['resume_flow']:
        category = "personal"
    else:
        category = "predefined"
    
    reply = generate_conversational_reply(answer)
    evaluation, score = evaluate_response(main_question, answer, job_description)
    
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

    # Generate related question if applicable
    if interview_context["question_depth"] < interview_context["max_depth"]:
        next_question = generate_next_question(main_question, answer, score, interview_track)
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
        personal_count = len([q for q in questions if q in structure['resume_flow']])
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

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start_interview', methods=['POST'])
def start_interview():
    global questions, current_question, evaluations, use_voice, asked_questions, resume_questions, interview_context
    
    language = request.form['language']
    mode = request.form['mode']
    interview_track = request.form['interview_track']
    sub_track = request.form.get('sub_track', '')
    use_voice = mode == 'voice'
    resume_file = request.files.get('resume')

    if not resume_file:
        return jsonify({"error": "Resume file is required"}), 400
    
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

    questions = []
    current_question = 0
    evaluations = []
    asked_questions = set()
    
    resume_questions = generate_resume_questions(resume_text)
    asked_questions.update(resume_questions)
    
    if interview_track == "resume":
        predefined_questions = structure['resume_flow'][:3]
        questions = resume_questions + [q for q in predefined_questions if q not in resume_questions]
    elif interview_track == "school_based":
        school_questions = structure['school_based'][sub_track] if sub_track in structure['school_based'] else [q for sublist in structure['school_based'].values() for q in sublist]
        questions = resume_questions[:5] + school_questions
    elif interview_track == "interest_areas":
        interest_questions = structure['interest_areas'][sub_track] if sub_track in structure['interest_areas'] else [q for sublist in structure['interest_areas'].values() for q in sublist]
        questions = resume_questions[:5] + interest_questions

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
        'asked_questions': asked_questions
    })
    
    return jsonify({
        "message": "Starting interview",
        "total_questions": len(questions),
        "current_question": questions[0],
        "question_number": 1,
        "use_voice": use_voice
    })

@app.route('/submit_voice_answer', methods=['POST'])
def submit_voice_answer():
    """Simulate submitting a voice answer to the queue."""
    answer = request.json.get('answer', "No response provided")
    voice_answer_queue.put(answer)
    return jsonify({"message": "Voice answer received"})

if __name__ == "__main__":
    app.run(debug=True, port=5000)