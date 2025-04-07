from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
import pandas as pd
import os
import time
import threading
import logging
from werkzeug.utils import secure_filename
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import pickle
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import os

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
app = Flask(__name__)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev_key_fallback')

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///email_automation.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload
app.config['OAUTH_CREDENTIALS_DIR'] = 'oauth_credentials'

# Ensure folders exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OAUTH_CREDENTIALS_DIR'], exist_ok=True)

# OAuth2 Configuration
CLIENT_SECRETS_FILE = 'client_secret.json'  # Download this file from Google Cloud Console
SCOPES = ['https://www.googleapis.com/auth/gmail.send']
API_SERVICE_NAME = 'gmail'
API_VERSION = 'v1'

# Setup logging
logging.basicConfig(level=logging.INFO,
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                   filename='email_automation.log')
logger = logging.getLogger(__name__)

# Initialize database
db = SQLAlchemy(app)

# Database models
class EmailJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_email = db.Column(db.String(100), nullable=False)
    token_file = db.Column(db.String(200))  # Path to token file
    filename = db.Column(db.String(100))
    template = db.Column(db.Text)
    subject = db.Column(db.String(200))
    status = db.Column(db.String(20), default='pending')
    total_emails = db.Column(db.Integer, default=0)
    sent_emails = db.Column(db.Integer, default=0)
    failed_email_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    # Add relationship
    failed_emails = db.relationship('FailedEmail', back_populates='job')

class FailedEmail(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('email_job.id'), nullable=False)
    email = db.Column(db.String(100))
    first_name = db.Column(db.String(100))
    error_message = db.Column(db.Text)
    retry_count = db.Column(db.Integer, default=0)
    job = db.relationship('EmailJob', back_populates='failed_emails')

# Create database tables
with app.app_context():
    db.create_all()

# OAuth Helper Functions
def get_oauth_flow():
    """Create and return OAuth 2.0 flow instance"""
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=url_for('oauth2callback', _external=True)
    )
    return flow

def get_credentials(token_file):
    """Get valid user credentials from storage."""
    creds = None
    
    if os.path.exists(token_file):
        with open(token_file, 'rb') as token:
            creds = pickle.load(token)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_file, 'wb') as token:
                pickle.dump(creds, token)
        else:
            return None  # Need to authenticate
            
    return creds

def send_email_via_gmail_api(sender_email, recipient_email, first_name, template, subject, token_file):
    """Send email using Gmail API with OAuth 2.0"""
    try:
        # Replace placeholders in template
        personalized_template = template.replace('{{first_name}}', first_name)
        
        # Get credentials
        creds = get_credentials(token_file)
        if not creds:
            return False, "OAuth credentials not found or invalid"
        
        # Build Gmail API service
        service = build(API_SERVICE_NAME, API_VERSION, credentials=creds)
        
        # Create message
        message = MIMEMultipart('alternative')
        message['Subject'] = subject
        message['From'] = sender_email
        message['To'] = recipient_email
        
        # Add HTML content
        message.attach(MIMEText(personalized_template, 'html'))
        
        # Encode the message
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
        
        # Send message
        sent_message = service.users().messages().send(
            userId='me',
            body={'raw': raw_message}
        ).execute()
        
        logger.info(f"Successfully sent email to {recipient_email}, Message ID: {sent_message['id']}")
        return True, None
        
    except Exception as e:
        logger.error(f"Error sending email to {recipient_email}: {str(e)}")
        return False, str(e)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ['csv']

def process_email_job(job_id):
    """Background process to send emails in batches"""
    with app.app_context():
        job = EmailJob.query.get(job_id)
        if not job:
            logger.error(f"Job {job_id} not found")
            return
        
        job.status = 'processing'
        db.session.commit()
        
        try:
            # Load CSV file
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], job.filename)
            df = pd.read_csv(file_path)
            
            # Check required columns
            if 'email' not in df.columns or 'first_name' not in df.columns:
                logger.error(f"CSV missing required columns for job {job_id}")
                job.status = 'failed'
                db.session.commit()
                return
            
            # Update total email count
            job.total_emails = len(df)
            db.session.commit()
            
            # Send emails in batches
            batch_size = 100
            for i in range(0, len(df), batch_size):
                batch = df.iloc[i:i+batch_size]
                
                for _, row in batch.iterrows():
                    # Skip empty or invalid emails
                    if pd.isna(row['email']) or not row['email'].strip():
                        continue
                    
                    first_name = row['first_name'] if not pd.isna(row['first_name']) else ''
                    success, error = send_email_via_gmail_api(
                        job.sender_email, 
                        row['email'], 
                        first_name, 
                        job.template,
                        job.subject,
                        job.token_file
                    )
                    
                    if success:
                        job.sent_emails += 1
                    else:
                        job.failed_email_count += 1
                        failed_email = FailedEmail(
                            job_id=job.id,
                            email=row['email'],
                            first_name=first_name,
                            error_message=error
                        )
                        db.session.add(failed_email)
                    
                    db.session.commit()
                    # Small delay to prevent rate limits
                    time.sleep(0.1)
            
            # Process any failed emails once more
            retry_failed_emails(job_id)
            
            job.status = 'completed'
            job.completed_at = datetime.now()
            db.session.commit()
            
        except Exception as e:
            logger.error(f"Error processing job {job_id}: {str(e)}")
            job.status = 'failed'
            db.session.commit()

def retry_failed_emails(job_id):
    """Retry sending failed emails"""
    job = EmailJob.query.get(job_id)
    
    failed_emails = FailedEmail.query.filter_by(job_id=job_id, retry_count=0).all()
    
    for failed in failed_emails:
        success, error = send_email_via_gmail_api(
            job.sender_email, 
            failed.email, 
            failed.first_name, 
            job.template,
            job.subject,
            job.token_file
        )
        
        if success:
            job.sent_emails += 1
            job.failed_email_count -= 1
            db.session.delete(failed)
        else:
            failed.retry_count += 1
            failed.error_message = error
        
        db.session.commit()
        time.sleep(0.1)

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/authorize')
def authorize():
    """Start the OAuth flow"""
    # Generate a unique email identifier for the session
    email = request.args.get('email', '')
    if not email:
        flash('Email is required')
        return redirect(url_for('create_job'))
    
    session['email'] = email
    
    # Create flow instance
    flow = get_oauth_flow()
    
    # Generate authorization URL
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'  # Force to show consent screen to get refresh token
    )
    
    # Store the state in the session
    session['state'] = state
    
    # Redirect to Google's OAuth 2.0 server
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
    """Handle the OAuth 2.0 callback"""
    # Retrieve state from session
    state = session.get('state')
    email = session.get('email')
    
    if not state or not email:
        flash('Session expired or invalid. Please try again.')
        return redirect(url_for('create_job'))
    
    # Create flow instance
    flow = get_oauth_flow()
    
    # Use the authorization server's response to fetch the OAuth 2.0 tokens
    flow.fetch_token(authorization_response=request.url)
    
    # Store credentials
    token_filename = f"{email.replace('@', '_at_').replace('.', '_dot_')}_{int(time.time())}.pickle"
    token_path = os.path.join(app.config['OAUTH_CREDENTIALS_DIR'], token_filename)
    
    with open(token_path, 'wb') as token:
        pickle.dump(flow.credentials, token)
    
    # Store token path in session for job creation
    session['token_path'] = token_path
    
    flash('Successfully authenticated with Gmail!')
    return redirect(url_for('create_job'))

@app.route('/create_job', methods=['GET', 'POST'])
def create_job():
    if request.method == 'POST':
        # Get sender email
        sender_email = request.form.get('sender_email')
        subject = request.form.get('subject', 'No Subject')
        
        # Check if authentication is done
        token_path = session.get('token_path')
        if not token_path or not os.path.exists(token_path):
            flash('Please authenticate with Gmail first')
            return redirect(url_for('authorize', email=sender_email))
        
        # Check if file and template are present
        if 'csv_file' not in request.files or not request.form.get('email_template'):
            flash('Both CSV file and email template are required')
            return redirect(request.url)
        
        # Validate sender email
        if not sender_email:
            flash('Sender email is required')
            return redirect(request.url)
        
        file = request.files['csv_file']
        template = request.form.get('email_template')
        
        # Check if file is valid
        if file.filename == '' or not allowed_file(file.filename):
            flash('Please upload a valid CSV file')
            return redirect(request.url)
        
        # Save file
        filename = secure_filename(file.filename)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)
        
        # Create job
        new_job = EmailJob(
            sender_email=sender_email,
            token_file=token_path,
            filename=filename,
            template=template,
            subject=subject,
            status='pending'
        )
        
        db.session.add(new_job)
        db.session.commit()
        
        # Clear session data
        session.pop('token_path', None)
        
        # Start processing job in background
        thread = threading.Thread(target=process_email_job, args=(new_job.id,))
        thread.daemon = True
        thread.start()
        
        flash(f'Job created successfully. Job ID: {new_job.id}')
        return redirect(url_for('job_details', job_id=new_job.id))
    
    # For GET requests, check if we have token from OAuth flow
    token_path = session.get('token_path')
    authenticated = token_path and os.path.exists(token_path)
    sender_email = session.get('email', '')
    
    return render_template('create_job.html', authenticated=authenticated, sender_email=sender_email)

@app.route('/jobs')
def jobs_list():
    jobs = EmailJob.query.order_by(EmailJob.created_at.desc()).all()
    return render_template('jobs_list.html', jobs=jobs)

@app.route('/job/<int:job_id>')
def job_details(job_id):
    job = EmailJob.query.filter_by(id=job_id).first_or_404()
    failed_emails = FailedEmail.query.filter_by(job_id=job_id).all()
    
    return render_template('job_details.html', job=job, failed_emails=failed_emails)

@app.route('/retry-failed/<int:job_id>', methods=['POST'])
def retry_failed(job_id):
    job = EmailJob.query.filter_by(id=job_id).first_or_404()
    
    # Start retry process in background
    thread = threading.Thread(target=retry_failed_emails, args=(job.id,))
    thread.daemon = True
    thread.start()
    
    flash('Retrying failed emails')
    return redirect(url_for('job_details', job_id=job.id))

@app.route('/api/job-status/<int:job_id>')
def job_status(job_id):
    job = EmailJob.query.filter_by(id=job_id).first_or_404()
    
    return jsonify({
        'status': job.status,
        'total': job.total_emails,
        'sent': job.sent_emails,
        'failed': job.failed_email_count
    })

if __name__ == '__main__':
    print("="*80)
    print("Starting Flask Application")
    print("Server running on: http://127.0.0.1:5000")
    print("="*80)
    app.run(debug=True, host='127.0.0.1', port=5000)