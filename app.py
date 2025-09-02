from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from datetime import datetime
import os
import hashlib
import uuid
import mimetypes

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-change-this'
database_url = os.environ.get('DATABASE_URL', 'sqlite:///imageboard.db')
# Fix postgres URL for SQLAlchemy 1.4+
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://')
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB max file size

# Ensure upload directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)

# Database Models
class Board(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(10), unique=True, nullable=False)
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    threads = db.relationship('Thread', backref='board', lazy=True, cascade='all, delete-orphan')

class Thread(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    board_id = db.Column(db.Integer, db.ForeignKey('board.id'), nullable=False)
    subject = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    bumped_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_pinned = db.Column(db.Boolean, default=False)
    is_locked = db.Column(db.Boolean, default=False)
    posts = db.relationship('Post', backref='thread', lazy=True, cascade='all, delete-orphan')

class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.Integer, db.ForeignKey('thread.id'), nullable=False)
    name = db.Column(db.String(100), default='Anonymous')
    email = db.Column(db.String(100))
    subject = db.Column(db.String(200))
    comment = db.Column(db.Text)
    filename = db.Column(db.String(255))
    original_filename = db.Column(db.String(255))
    file_size = db.Column(db.Integer)
    image_width = db.Column(db.Integer)
    image_height = db.Column(db.Integer)
    thumbnail = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    post_number = db.Column(db.Integer, nullable=False)

# Helper Functions
def allowed_file(filename):
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'webm', 'mp4'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def generate_filename(original_filename):
    """Generate unique filename while preserving extension"""
    ext = original_filename.rsplit('.', 1)[1].lower()
    unique_id = str(uuid.uuid4())[:8]
    return f"{unique_id}.{ext}"

def get_next_post_number(thread_id):
    """Get the next post number for a thread"""
    last_post = Post.query.filter_by(thread_id=thread_id).order_by(Post.post_number.desc()).first()
    return (last_post.post_number + 1) if last_post else 1

# Routes
@app.route('/')
def index():
    boards = Board.query.all()
    return render_template('index.html', boards=boards)

@app.route('/<board_name>/')
def board_view(board_name):
    board = Board.query.filter_by(name=board_name).first_or_404()
    page = request.args.get('page', 1, type=int)
    threads = Thread.query.filter_by(board_id=board.id)\
        .order_by(Thread.is_pinned.desc(), Thread.bumped_at.desc())\
        .paginate(page=page, per_page=10, error_out=False)
    
    # Get preview posts for each thread
    for thread in threads.items:
        thread.preview_posts = Post.query.filter_by(thread_id=thread.id)\
            .order_by(Post.created_at.asc()).limit(5).all()
        thread.total_posts = Post.query.filter_by(thread_id=thread.id).count()
    
    return render_template('board.html', board=board, threads=threads)

@app.route('/<board_name>/thread/<int:thread_id>')
def thread_view(board_name, thread_id):
    board = Board.query.filter_by(name=board_name).first_or_404()
    thread = Thread.query.filter_by(id=thread_id, board_id=board.id).first_or_404()
    posts = Post.query.filter_by(thread_id=thread_id).order_by(Post.created_at.asc()).all()
    
    # Ensure the thread has an OP post for proper display
    if not posts:
        flash('Thread has no posts')
        return redirect(url_for('board_view', board_name=board_name))
    
    return render_template('thread.html', board=board, thread=thread, posts=posts)

@app.route('/<board_name>/post', methods=['POST'])
def create_post(board_name):
    board = Board.query.filter_by(name=board_name).first_or_404()
    
    name = request.form.get('name', 'Anonymous')
    email = request.form.get('email', '')
    subject = request.form.get('subject', '')
    comment = request.form.get('comment', '')
    thread_id = request.form.get('thread_id')
    
    # Handle file upload
    file = request.files.get('file')
    filename = None
    original_filename = None
    file_size = None
    
    if file and file.filename and allowed_file(file.filename):
        original_filename = secure_filename(file.filename)
        filename = generate_filename(original_filename)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)
        file_size = os.path.getsize(file_path)
    
    # Create new thread if no thread_id provided
    if not thread_id:
        if not subject and not comment and not filename:
            flash('Thread must have subject, comment, or image')
            return redirect(url_for('board_view', board_name=board_name))
        
        thread = Thread(
            board_id=board.id,
            subject=subject or 'No Subject',
            created_at=datetime.utcnow(),
            bumped_at=datetime.utcnow()
        )
        db.session.add(thread)
        db.session.flush()
        thread_id = thread.id
        post_number = 1
    else:
        thread = Thread.query.get_or_404(thread_id)
        if thread.is_locked:
            flash('Thread is locked')
            return redirect(url_for('thread_view', board_name=board_name, thread_id=thread_id))
        post_number = get_next_post_number(thread_id)
        # Bump thread
        thread.bumped_at = datetime.utcnow()
    
    # Create post
    post = Post(
        thread_id=thread_id,
        name=name,
        email=email,
        subject=subject,
        comment=comment,
        filename=filename,
        original_filename=original_filename,
        file_size=file_size,
        post_number=post_number,
        created_at=datetime.utcnow()
    )
    
    db.session.add(post)
    db.session.commit()
    
    return redirect(url_for('thread_view', board_name=board_name, thread_id=thread_id))

@app.route('/admin/create_board', methods=['GET', 'POST'])
def create_board():
    if request.method == 'POST':
        name = request.form['name']
        title = request.form['title']
        description = request.form.get('description', '')
        
        if Board.query.filter_by(name=name).first():
            flash('Board already exists')
            return redirect(url_for('create_board'))
        
        board = Board(name=name, title=title, description=description)
        db.session.add(board)
        db.session.commit()
        flash('Board created successfully')
        return redirect(url_for('board_view', board_name=name))
    
    return render_template('create_board.html')

# Initialize database and app
with app.app_context():
    db.create_all()
    
    # Create default boards if they don't exist
    if not Board.query.first():
        default_boards = [
            {'name': 'b', 'title': 'Random', 'description': 'Random discussions'},
            {'name': 'g', 'title': 'Technology', 'description': 'Technology discussions'},
            {'name': 'v', 'title': 'Video Games', 'description': 'Video game discussions'},
        ]
        
        for board_data in default_boards:
            board = Board(**board_data)
            db.session.add(board)
        
        db.session.commit()

# Initialize database
def create_tables():
    db.create_all()
    
    # Create default boards if they don't exist
    if not Board.query.first():
        default_boards = [
            {'name': 'b', 'title': 'Random', 'description': 'Random discussions'},
            {'name': 'g', 'title': 'Technology', 'description': 'Technology discussions'},
            {'name': 'v', 'title': 'Video Games', 'description': 'Video game discussions'},
        ]
        
        for board_data in default_boards:
            board = Board(**board_data)
            db.session.add(board)
        
        db.session.commit()

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))