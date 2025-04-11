from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
import pandas as pd
import os
import time
import threading
import logging
import json
import requests
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import pickle
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import aiohttp
import asyncio
from flask_pymongo import PyMongo
from bson.objectid import ObjectId


# Decode client_secret.json from environment variable
if not os.path.exists('client_secret.json') and os.environ.get('GOOGLE_CLIENT_SECRET_BASE64'):
    with open('client_secret.json', 'wb') as f:
        f.write(base64.b64decode(os.environ['GOOGLE_CLIENT_SECRET_BASE64']))

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
app = Flask(__name__)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')

# MongoDB configuration
app.config['MONGO_URI'] = os.environ.get('MONGO_URI')
mongo = PyMongo(app)

app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload
app.config['OAUTH_CREDENTIALS_DIR'] = 'oauth_credentials'
app.config['ZOHO_CREDENTIALS_DIR'] = 'zoho_credentials'

# Ensure folders exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OAUTH_CREDENTIALS_DIR'], exist_ok=True)
os.makedirs(app.config['ZOHO_CREDENTIALS_DIR'], exist_ok=True)

# OAuth2 Configuration for Gmail
CLIENT_SECRETS_FILE = 'client_secret.json'  # Download this file from Google Cloud Console
GMAIL_SCOPES = ['https://www.googleapis.com/auth/gmail.send']
API_SERVICE_NAME = 'gmail'
API_VERSION = 'v1'

# Zoho CRM Configuration
ZOHO_CLIENT_ID = os.environ.get('ZOHO_CLIENT_ID', '')
ZOHO_CLIENT_SECRET = os.environ.get('ZOHO_CLIENT_SECRET', '')
ZOHO_AUTH_URL = 'https://accounts.zoho.com/oauth/v2/auth'
ZOHO_TOKEN_URL = 'https://accounts.zoho.com/oauth/v2/token'
ZOHO_REDIRECT_URI = os.environ.get('ZOHO_REDIRECT_URI', 'http://127.0.0.1:5000/zoho/callback')
ZOHO_SCOPES = 'ZohoCRM.modules.ALL ZohoCRM.settings.ALL ZohoCRM.templates.email.READ'
ZOHO_API_BASE_URL = 'https://www.zohoapis.com/crm/v2'

# Setup logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    filename='email_automation.log')
logger = logging.getLogger(__name__)

# Define MongoDB collection names
email_jobs = mongo.db.email_jobs
failed_emails = mongo.db.failed_emails
zoho_credentials = mongo.db.zoho_credentials

# Create indexes for better query performance
with app.app_context():
    email_jobs.create_index([('created_at', -1)])
    failed_emails.create_index([('job_id', 1)])
    zoho_credentials.create_index([('user_email', 1)], unique=True)

# OAuth Helper Functions for Gmail
def get_oauth_flow():
    """Create and return OAuth 2.0 flow instance for Gmail"""
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=GMAIL_SCOPES,
        redirect_uri=url_for('oauth2callback', _external=True)
    )
    return flow

def get_gmail_credentials(token_file):
    """Get valid user credentials from storage for Gmail."""
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

# Zoho CRM Helper Functions
def get_zoho_auth_url(email):
    """Generate Zoho CRM authorization URL"""
    state = f"{email}:{int(time.time())}"
    session['zoho_state'] = state
    
    params = {
        'client_id': ZOHO_CLIENT_ID,
        'redirect_uri': ZOHO_REDIRECT_URI,
        'scope': ZOHO_SCOPES,  # Updated to include email template read permission
        'response_type': 'code',
        'access_type': 'offline',
        'state': state
    }
    
    auth_url = f"{ZOHO_AUTH_URL}?{'&'.join([f'{k}={v}' for k, v in params.items()])}"
    return auth_url

def get_zoho_token(code):
    """Exchange authorization code for tokens"""
    data = {
        'client_id': ZOHO_CLIENT_ID,
        'client_secret': ZOHO_CLIENT_SECRET,
        'redirect_uri': ZOHO_REDIRECT_URI,
        'code': code,
        'grant_type': 'authorization_code'
    }
    
    response = requests.post(ZOHO_TOKEN_URL, data=data)
    if response.status_code == 200:
        return response.json()
    else:
        logger.error(f"Error getting Zoho token: {response.text}")
        return None

def refresh_zoho_token(refresh_token):
    """Refresh Zoho access token"""
    data = {
        'client_id': ZOHO_CLIENT_ID,
        'client_secret': ZOHO_CLIENT_SECRET,
        'refresh_token': refresh_token,
        'grant_type': 'refresh_token'
    }
    
    response = requests.post(ZOHO_TOKEN_URL, data=data)
    if response.status_code == 200:
        return response.json()
    else:
        logger.error(f"Error refreshing Zoho token: {response.text}")
        return None

def get_zoho_credentials(user_email):
    """Get valid Zoho CRM credentials for user"""
    creds = zoho_credentials.find_one({'user_email': user_email})
    
    if not creds:
        return None

    # Fix: Safely convert string to datetime only if it's not already
    expires_at = creds['expires_at']
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)

    # Check if token is expired or about to expire (within 5 minutes)
    if datetime.utcnow() >= expires_at - timedelta(minutes=5):
        # Refresh token
        token_data = refresh_zoho_token(creds['refresh_token'])
        if token_data:
            new_expiry = datetime.utcnow() + timedelta(seconds=token_data.get('expires_in', 3600))

            zoho_credentials.update_one(
                {'user_email': user_email},
                {
                    '$set': {
                        'access_token': token_data.get('access_token'),
                        'expires_at': new_expiry,
                        'updated_at': datetime.utcnow()
                    }
                }
            )
            creds['access_token'] = token_data.get('access_token')
            creds['expires_at'] = new_expiry
        else:
            logger.error(f"Failed to refresh Zoho token for {user_email}")
            return None

    return {
        'access_token': creds['access_token'],
        'refresh_token': creds['refresh_token'],
        'expires_at': expires_at
    }


async def get_zoho_templates(user_email):
    """Asynchronously fetch email templates from Zoho CRM"""
    creds = get_zoho_credentials(user_email)
    if not creds:
        logger.error(f"No valid Zoho credentials for {user_email}")
        return []
    
    headers = {
        'Authorization': f"Zoho-oauthtoken {creds['access_token']}",
        'Content-Type': 'application/json'
    }
    
    url = f"{ZOHO_API_BASE_URL}/settings/email_templates"
    logger.info(f"Requesting Zoho templates from URL: {url}")
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                logger.info(f"Zoho API status code: {response.status}")
                logger.info(f"Zoho API response headers: {response.headers}")
                
                if response.status == 200:
                    data = await response.json()
                    logger.info(f"API response: {json.dumps(data)[:500]}...")
                    
                    # Check for email_templates at the root level first
                    if 'email_templates' in data:
                        return data['email_templates']
                    elif 'data' in data and 'email_templates' in data['data']:
                        return data['data']['email_templates']
                    elif 'data' in data:
                        return data['data']
                    else:
                        # If the structure matches exactly what you shared in your example
                        # Return the data directly if it has the expected structure
                        if isinstance(data, dict) and 'info' in data and any(key in data for key in ['email_templates']):
                            return data.get('email_templates', [])
                        
                        logger.warning(f"Unexpected structure: {json.dumps(data)[:500]}...")
                        return []
                elif response.status == 401:
                    logger.error("Authentication failed. Token may be invalid or expired.")
                elif response.status == 403:
                    logger.error("Permission denied. Check OAuth scopes.")
                else:
                    logger.error(f"Error fetching Zoho templates: {await response.text()}")
        except Exception as e:
            logger.error(f"Exception while fetching Zoho templates: {str(e)}")
    
    return []

async def get_zoho_template_content(user_email, template_id):
    """Asynchronously fetch specific template content from Zoho CRM"""
    creds = get_zoho_credentials(user_email)
    if not creds:
        logger.error(f"No valid Zoho credentials for {user_email}")
        return None
        
    headers = {
        'Authorization': f"Zoho-oauthtoken {creds['access_token']}",
        'Content-Type': 'application/json'
    }
        
    url = f"{ZOHO_API_BASE_URL}/settings/email_templates/{template_id}"
    logger.info(f"Requesting Zoho template from URL: {url}")
        
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Error from Zoho API: {error_text}")
                    return None
                    
                data = await response.json()
                logger.info(f"Template response: {json.dumps(data)[:500]}...")
                
                # Extract template from response
                template = None
                
                # Handle the structure we're seeing in the logs
                if 'email_templates' in data and isinstance(data['email_templates'], list):
                    if data['email_templates']:
                        template = data['email_templates'][0]
                elif 'data' in data:
                    if isinstance(data['data'], list) and data['data']:
                        template = data['data'][0]
                    elif isinstance(data['data'], dict) and 'email_templates' in data['data']:
                        if data['data']['email_templates']:
                            template = data['data']['email_templates'][0]
                
                if not template:
                    logger.error(f"Couldn't extract template from response")
                    return None
                
                # Return the complete template object
                return template
                
        except Exception as e:
            logger.error(f"Exception while fetching Zoho template: {str(e)}")
            return None

def send_email_via_gmail_api(sender_email, recipient_email, first_name, template, subject, token_file):
    """Send email using Gmail API with OAuth 2.0"""
    try:
        # Replace placeholders in template
        personalized_template = template.replace('{{first_name}}', first_name)
        
        # Get credentials
        creds = get_gmail_credentials(token_file)
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
        job = email_jobs.find_one({'_id': ObjectId(job_id)})
        if not job:
            logger.error(f"Job {job_id} not found")
            return
        
        email_jobs.update_one(
            {'_id': ObjectId(job_id)},
            {'$set': {'status': 'processing'}}
        )
        
        try:
            # Load CSV file
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], job['filename'])
            df = pd.read_csv(file_path)
            
            # Check required columns
            if 'email' not in df.columns or 'first_name' not in df.columns:
                logger.error(f"CSV missing required columns for job {job_id}")
                email_jobs.update_one(
                    {'_id': ObjectId(job_id)},
                    {'$set': {'status': 'failed'}}
                )
                return
            
            # Update total email count
            email_jobs.update_one(
                {'_id': ObjectId(job_id)},
                {'$set': {'total_emails': len(df)}}
            )
            
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
                        job['sender_email'], 
                        row['email'], 
                        first_name, 
                        job['template'],
                        job['subject'],
                        job['token_file']
                    )
                    
                    if success:
                        email_jobs.update_one(
                            {'_id': ObjectId(job_id)},
                            {'$inc': {'sent_emails': 1}}
                        )
                    else:
                        email_jobs.update_one(
                            {'_id': ObjectId(job_id)},
                            {'$inc': {'failed_email_count': 1}}
                        )
                        failed_emails.insert_one({
                            'job_id': job_id,
                            'email': row['email'],
                            'first_name': first_name,
                            'error_message': error,
                            'retry_count': 0,
                            'created_at': datetime.utcnow()
                        })
                    
                    # Small delay to prevent rate limits
                    time.sleep(0.1)
            
            # Process any failed emails once more
            retry_failed_emails(job_id)
            
            email_jobs.update_one(
                {'_id': ObjectId(job_id)},
                {'$set': {
                    'status': 'completed',
                    'completed_at': datetime.utcnow()
                }}
            )
            
        except Exception as e:
            logger.error(f"Error processing job {job_id}: {str(e)}")
            email_jobs.update_one(
                {'_id': ObjectId(job_id)},
                {'$set': {'status': 'failed'}}
            )

def retry_failed_emails(job_id):
    """Retry sending failed emails"""
    job = email_jobs.find_one({'_id': ObjectId(job_id)})
    
    failed = failed_emails.find({'job_id': job_id, 'retry_count': 0})
    
    for failed_email in failed:
        success, error = send_email_via_gmail_api(
            job['sender_email'], 
            failed_email['email'], 
            failed_email['first_name'], 
            job['template'],
            job['subject'],
            job['token_file']
        )
        
        if success:
            email_jobs.update_one(
                {'_id': ObjectId(job_id)},
                {
                    '$inc': {'sent_emails': 1, 'failed_email_count': -1}
                }
            )
            failed_emails.delete_one({'_id': failed_email['_id']})
        else:
            failed_emails.update_one(
                {'_id': failed_email['_id']},
                {
                    '$inc': {'retry_count': 1},
                    '$set': {'error_message': error}
                }
            )
        
        time.sleep(0.1)

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/authorize')
def authorize():
    """Start the OAuth flow for Gmail"""
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
    """Handle the OAuth 2.0 callback for Gmail"""
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
    # Set gmail_authenticated flag
    session['gmail_authenticated'] = True
    
    flash('Successfully authenticated with Gmail!')
    return redirect(url_for('create_job'))

@app.route('/zoho/authorize')
def zoho_authorize():
    """Start the OAuth flow for Zoho CRM"""
    email = request.args.get('email', '')
    if not email:
        flash('Email is required')
        return redirect(url_for('create_job'))
    
    session['zoho_email'] = email
    auth_url = get_zoho_auth_url(email)
    
    return redirect(auth_url)

@app.route('/zoho/callback')
def zoho_callback():
    """Handle the OAuth 2.0 callback for Zoho CRM"""
    code = request.args.get('code')
    state = request.args.get('state')
    
    stored_state = session.get('zoho_state')
    if not state or not stored_state or state != stored_state:
        flash('Invalid state parameter')
        return redirect(url_for('create_job'))
    
    email = state.split(':', 1)[0]
    
    if not code:
        flash('Authorization code not received')
        return redirect(url_for('create_job'))
    
    # Exchange code for tokens
    token_data = get_zoho_token(code)
    if not token_data:
        flash('Failed to authenticate with Zoho')
        return redirect(url_for('create_job'))
    
    # Save tokens in database
    current_time = datetime.utcnow()
    expires_at = current_time + timedelta(seconds=token_data.get('expires_in', 3600))
    
    # Use upsert to create or update
    zoho_credentials.update_one(
        {'user_email': email},
        {
            '$set': {
                'user_email': email,
                'access_token': token_data.get('access_token'),
                'refresh_token': token_data.get('refresh_token'),
                'expires_at': expires_at,
                'updated_at': current_time
            },
            '$setOnInsert': {
                'created_at': current_time
            }
        },
        upsert=True
    )
    
    # Set zoho_authenticated flag
    session['zoho_authenticated'] = True
    
    flash('Successfully authenticated with Zoho CRM!')
    return redirect(url_for('create_job'))

@app.route('/api/zoho/templates')
def get_zoho_templates_api():
    """API endpoint to fetch Zoho CRM templates"""
    email = session.get('email')
    if not email:
        return jsonify({'success': False, 'message': 'No email in session'})
    
    templates = asyncio.run(get_zoho_templates(email))
    return jsonify({'success': True, 'templates': templates})

@app.route('/api/zoho/template/<template_id>')
def get_zoho_template_api(template_id):
    """API endpoint to fetch specific Zoho CRM template content"""
    email = session.get('email')
    if not email:
        return jsonify({'success': False, 'message': 'No email in session'})
    
    template = asyncio.run(get_zoho_template_content(email, template_id))
    if template:
        # Construct the response with properly formatted content
        return jsonify({
            'success': True, 
            'template': template,
            'content': template.get('content', ''),
            'html_content': template.get('content', '')
        })
    else:
        return jsonify({'success': False, 'message': 'Template not found'})

@app.route('/create_job', methods=['GET', 'POST'])
def create_job():
    if request.method == 'POST':
        # Get form data
        sender_email = request.form.get('sender_email')
        subject = request.form.get('subject', 'No Subject')
        template_source = request.form.get('template_source', 'custom')
        template_id = request.form.get('template_id') if template_source == 'zoho' else None
        template = ""
        
        # Check if authentication is done
        token_path = session.get('token_path')
        if not token_path or not os.path.exists(token_path):
            flash('Please authenticate with Gmail first')
            return redirect(url_for('authorize', email=sender_email))
        
        # Handle template selection
        if template_source == 'custom':
            template = request.form.get('email_template', '')
            
            if not template:
                flash('Email template is required')
                return redirect(request.url)
        else:  # template_source == 'zoho'
            if not template_id:
                flash('Please select a Zoho template')
                return redirect(request.url)
            
            # Check if edited template is provided (the user may have customized a Zoho template)
            edited_template = request.form.get('edited_template', '')
            if edited_template:
                template = edited_template
            else:
                template_data = asyncio.run(get_zoho_template_content(sender_email, template_id))
                if not template_data:
                    flash('Failed to retrieve Zoho template')
                    return redirect(request.url)
                
                template = template_data.get('content', '')
        
        # Check if file is present
        if 'csv_file' not in request.files:
            flash('CSV file is required')
            return redirect(request.url)
        
        # Validate sender email
        if not sender_email:
            flash('Sender email is required')
            return redirect(request.url)
        
        file = request.files['csv_file']
        
        # Check if file is valid
        if file.filename == '' or not allowed_file(file.filename):
            flash('Please upload a valid CSV file')
            return redirect(request.url)
        
        # Save file
        filename = secure_filename(file.filename)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)
        
        # Create job
        job_data = {
            'sender_email': sender_email,
            'token_file': token_path,
            'filename': filename,
            'template': template,
            'subject': subject,
            'status': 'pending',
            'template_source': template_source,
            'template_id': template_id,
            'total_emails': 0,
            'sent_emails': 0,
            'failed_email_count': 0,
            'created_at': datetime.utcnow()
        }
        
        result = email_jobs.insert_one(job_data)
        job_id = str(result.inserted_id)
        
        # Clear session data
        session.pop('token_path', None)
        
        # Start processing job in background
        thread = threading.Thread(target=process_email_job, args=(job_id,))
        thread.daemon = True
        thread.start()
        
        flash(f'Job created successfully. Job ID: {job_id}')
        return redirect(url_for('job_details', job_id=job_id))
    
    # For GET requests, check if we have token from OAuth flow
    token_path = session.get('token_path')
    gmail_authenticated = token_path and os.path.exists(token_path)
    sender_email = session.get('email', '')
    
    # Check Zoho authentication
    zoho_authenticated = False
    zoho_templates = []
    
    if sender_email:
        creds = get_zoho_credentials(sender_email)
        zoho_authenticated = creds is not None
        
        if zoho_authenticated:
            zoho_templates = asyncio.run(get_zoho_templates(sender_email))
    
    return render_template('create_job.html', 
                          gmail_authenticated=gmail_authenticated, 
                          zoho_authenticated=zoho_authenticated,
                          sender_email=sender_email,
                          zoho_templates=zoho_templates)

@app.route('/logout/gmail')
def logout_gmail():
    """Clear Gmail authentication to switch accounts"""
    # Remove the token path from session
    session.pop('token_path', None)
    # Keep the email to pre-fill the form (optional)
    email = session.get('email', '')
    # Clear Gmail authentication flag
    session.pop('gmail_authenticated', None)
    
    flash('Gmail account disconnected. You can now connect with a different account.')
    return redirect(url_for('create_job'))

@app.route('/logout/zoho')
def logout_zoho():
    email = session.get('zoho_email') or session.get('email')

    if email:
        # Remove Zoho credentials from MongoDB
        zoho_credentials.delete_one({'user_email': email})

    # Clear Zoho-related session data
    session.pop('zoho_authenticated', None)
    session.pop('zoho_email', None)
    session.pop('zoho_state', None)

    flash('Zoho CRM account disconnected. You can now connect with a different account.')
    return redirect(url_for('create_job'))


@app.route('/jobs')
def jobs_list():
    # Get all jobs sorted by creation date (newest first)
    jobs = list(email_jobs.find().sort('created_at', -1))
    
    # Convert ObjectId to string for each job
    for job in jobs:
        job['id'] = str(job['_id'])
    
    return render_template('jobs_list.html', jobs=jobs)

@app.route('/job/<job_id>')
def job_details(job_id):
    # Find the job by ID
    job = email_jobs.find_one({'_id': ObjectId(job_id)})
    if not job:
        flash('Job not found')
        return redirect(url_for('jobs_list'))
    
    # Add string ID for template access
    job['id'] = str(job['_id'])
    
    # Get failed emails for this job
    failed_email_list = list(failed_emails.find({'job_id': job_id}))
    
    # Convert ObjectId to string for each failed email
    for email in failed_email_list:
        email['id'] = str(email['_id'])
    
    return render_template('job_details.html', job=job, failed_emails=failed_email_list)

@app.route('/retry-failed/<job_id>', methods=['POST'])
def retry_failed(job_id):
    # Find the job by ID
    job = email_jobs.find_one({'_id': ObjectId(job_id)})
    if not job:
        flash('Job not found')
        return redirect(url_for('jobs_list'))
    
    # Start retry process in background
    thread = threading.Thread(target=retry_failed_emails, args=(job_id,))
    thread.daemon = True
    thread.start()
    
    flash('Retrying failed emails')
    return redirect(url_for('job_details', job_id=job_id))

@app.route('/api/job-status/<job_id>')
def job_status(job_id):
    # Find the job by ID
    job = email_jobs.find_one({'_id': ObjectId(job_id)})
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    
    return jsonify({
        'status': job.get('status', 'unknown'),
        'total': job.get('total_emails', 0),
        'sent': job.get('sent_emails', 0),
        'failed': job.get('failed_email_count', 0)
    })

if __name__ == '__main__':
    print("="*80)
    print("Starting Flask Application")
    print("="*80)
 