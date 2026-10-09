import functools
import json
import logging
import os
import secrets
import shutil
import time
import traceback
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

import click
from dotenv import load_dotenv
from flask import Flask, Response, g, jsonify, render_template, request, session
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, DateTime,
                        ForeignKey, Integer, String, UniqueConstraint, create_engine,
                        or_, select, text)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import declarative_base, sessionmaker
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import CounterMetricFamily

load_dotenv()
Base = declarative_base()


class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    trainer = Column(Boolean, nullable=False, default=False)
    login_failures = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime(timezone=True), nullable=True)


class Account(Base):
    __tablename__ = 'accounts'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), unique=True, nullable=False)
    number = Column(String(20), unique=True, nullable=False)
    balance = Column(BigInteger, nullable=False)  # Integer paise, never floating point.
    __table_args__ = (CheckConstraint('balance >= 0'),)


class Beneficiary(Base):
    __tablename__ = 'beneficiaries'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    account_id = Column(Integer, ForeignKey('accounts.id'), nullable=False)
    __table_args__ = (UniqueConstraint('user_id', 'account_id'),)


class Transfer(Base):
    __tablename__ = 'transfers'
    id = Column(String(36), primary_key=True)
    sender = Column(Integer, ForeignKey('accounts.id'), nullable=False)
    receiver = Column(Integer, ForeignKey('accounts.id'), nullable=False)
    amount = Column(BigInteger, nullable=False)
    key = Column(String(100), nullable=False)
    request_id = Column(String(36), nullable=False)
    created = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    __table_args__ = (UniqueConstraint('sender', 'key'), CheckConstraint('amount > 0'),
                      CheckConstraint('sender <> receiver'))


class Ledger(Base):
    __tablename__ = 'ledger_entries'
    id = Column(Integer, primary_key=True)
    transfer_id = Column(String(36), ForeignKey('transfers.id'), nullable=False)
    account_id = Column(Integer, ForeignKey('accounts.id'), nullable=False)
    amount = Column(BigInteger, nullable=False)  # Signed paise.
    __table_args__ = (UniqueConstraint('transfer_id', 'account_id'),)


def money(paise):
    return f'{Decimal(paise) / 100:.2f}'


class ProcessIOCollector:
    """Expose Linux process read/write I/O counters at scrape time."""

    def collect(self):
        try:
            with open('/proc/self/io', encoding='utf-8') as handle:
                counters = dict(line.strip().split(':', 1) for line in handle if ':' in line)
            read_bytes = int(counters['read_bytes'])
            write_bytes = int(counters['write_bytes'])
        except (OSError, ValueError, KeyError):
            return

        yield CounterMetricFamily('bank_process_read_bytes', 'Bytes read by application process from storage', value=read_bytes)
        yield CounterMetricFamily('bank_process_write_bytes', 'Bytes written by application process to storage', value=write_bytes)


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(SECRET_KEY=os.getenv('SECRET_KEY'),
        DATABASE_URL=os.getenv('DATABASE_URL'), SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE', 'true') == 'true',
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=30), MAX_CONTENT_LENGTH=16384,
        ENABLE_LAB=os.getenv('ENABLE_LAB', 'false') == 'true')
    if config:
        app.config.update(config)
    if not app.config['SECRET_KEY'] or not app.config['DATABASE_URL']:
        raise RuntimeError('Set SECRET_KEY and DATABASE_URL; see README.md')
    url = app.config['DATABASE_URL']
    if url.startswith(('postgres://', 'postgresql://')):
        url = 'postgresql+psycopg://' + url.split('://', 1)[1]
    if not url.startswith('postgresql+psycopg://'):
        raise RuntimeError('This demo requires PostgreSQL for row locking and concurrent transfers')
    engine = create_engine(url, pool_pre_ping=True, connect_args={'connect_timeout': 5}, hide_parameters=True)
    DB = sessionmaker(engine, expire_on_commit=False)
    app.extensions.update(bank_engine=engine, bank_db=DB)
    http_requests = Counter('bank_http_requests_total', 'Total HTTP requests', ['method', 'path', 'status'])
    http_duration = Histogram('bank_http_request_duration_seconds', 'HTTP request duration in seconds', ['method', 'path'])
    login_total = Counter('bank_login_total', 'Bank login attempts', ['result'])
    transfer_total = Counter('bank_transfer_total', 'Bank transfer attempts', ['result'])
    database_errors = Counter('bank_database_errors_total', 'Database errors observed by the application')
    app_errors = Counter('bank_application_errors_total', 'Unhandled application errors')
    # Application HTTP payload counters; these are not network-interface metrics.
    http_in_bytes = Counter('bank_http_request_body_bytes_total', 'Known HTTP request body bytes received')
    http_out_bytes = Counter('bank_http_response_body_bytes_total', 'Known HTTP response body bytes sent')
    http_5xx = Counter('bank_http_server_errors_total', 'HTTP 5xx response count (not network packet errors)')
    # Filesystem visible to this app process (not the Render service disk quota).
    # These callback gauges are refreshed when the /metrics endpoint is scraped.
    disk_total = Gauge('bank_disk_total_bytes', 'Total application-visible filesystem capacity in bytes')
    disk_used = Gauge('bank_disk_used_bytes', 'Used application-visible filesystem space in bytes')
    disk_free = Gauge('bank_disk_free_bytes', 'Available application-visible filesystem space in bytes')
    disk_total.set_function(lambda: shutil.disk_usage('/').total)
    disk_used.set_function(lambda: shutil.disk_usage('/').used)
    disk_free.set_function(lambda: shutil.disk_usage('/').free)
    REGISTRY.register(ProcessIOCollector())
    process_count = Gauge('bank_visible_process_count', 'Process count visible in application PID namespace')
    def visible_process_count():
        try:
            return sum(entry.isdigit() for entry in os.listdir('/proc'))
        except OSError:
            return float('nan')
    process_count.set_function(visible_process_count)

    logger = logging.getLogger('bank')
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)

    def event(name, **fields):
        logger.info(json.dumps(dict(time=datetime.now(timezone.utc).isoformat(),
            request_id=getattr(g, 'request_id', None), event=name, **fields)))

    def fail(message, status):
        return jsonify(error=message, request_id=g.request_id), status

    def auth(trainer=False):
        def decorate(fn):
            @functools.wraps(fn)
            def wrapped(*args, **kwargs):
                if not session.get('user_id'):
                    return fail('Please sign in', 401)
                with DB() as db:
                    user = db.get(User, session['user_id'])
                    if not user:
                        session.clear()
                        return fail('Please sign in again', 401)
                    if trainer and not user.trainer:
                        return fail('Trainer access required', 403)
                return fn(*args, **kwargs)
            return wrapped
        return decorate

    @app.before_request
    def before():
        g.request_id = str(uuid.uuid4())
        g.started = time.monotonic()
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            expected = session.get('csrf', '')
            supplied = request.headers.get('X-CSRF-Token', '')
            if not expected or not secrets.compare_digest(expected, supplied):
                return fail('Invalid CSRF token; refresh the page', 403)

    @app.after_request
    def after(response):
        response.headers['X-Request-ID'] = g.request_id
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if not request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-store'
        duration = time.monotonic() - g.started
        http_requests.labels(request.method, request.path, str(response.status_code)).inc()
        http_duration.labels(request.method, request.path).observe(duration)
        # Omit unknown lengths; don't buffer request/response payloads to measure them.
        if request.content_length is not None and request.content_length >= 0:
            http_in_bytes.inc(request.content_length)
        response_size = response.calculate_content_length()
        if response_size is not None and response_size >= 0:
            http_out_bytes.inc(response_size)
        if 500 <= response.status_code < 600:
            http_5xx.inc()
        event('request_completed', method=request.method, path=request.path,
              status=response.status_code, duration_ms=round(duration*1000, 1))
        return response

    @app.errorhandler(SQLAlchemyError)
    def database_error(exc):
        # Do not log SQL parameters, connection URLs or exception strings.
        database_errors.inc()
        event('database_error', exception_type=type(exc).__name__)
        return fail('Database operation unavailable; check application logs', 503)

    @app.errorhandler(Exception)
    def unexpected(exc):
        if isinstance(exc, HTTPException):
            return fail(exc.name, exc.code)
        app_errors.inc()
        event('application_error', exception_type=type(exc).__name__, traceback=traceback.format_tb(exc.__traceback__))
        return fail('Application error; trace the request ID in logs', 500)

    @app.get('/')
    def home():
        return render_template('index.html')

    @app.get('/api/session')
    def state():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        result = dict(csrf=session['csrf'], user=None, lab=app.config['ENABLE_LAB'])
        if session.get('user_id'):
            with DB() as db:
                user = db.get(User, session['user_id'])
                if user:
                    result['user'] = dict(name=user.username, trainer=user.trainer)
        return jsonify(result)

    @app.post('/api/login')
    def login():
        data = request.get_json()
        if not isinstance(data, dict) or not isinstance(data.get('username'), str) or not isinstance(data.get('password'), str):
            return fail('Enter username and password', 400)
        with DB.begin() as db:
            user = db.scalar(select(User).where(User.username == data['username'].lower()).with_for_update())
            now = datetime.now(timezone.utc)
            if user and user.locked_until and user.locked_until > now:
                return fail('Too many sign-in attempts; try again in 10 minutes', 429)
            if user and user.locked_until:
                user.login_failures = 0
                user.locked_until = None
            if not user or not check_password_hash(user.password_hash, data['password']):
                if user:
                    user.login_failures += 1
                    if user.login_failures >= 10:
                        user.locked_until = now + timedelta(minutes=10)
                login_total.labels('rejected').inc()
                event('login_rejected')
                return fail('Incorrect username or password', 401)
            user.login_failures = 0
            user.locked_until = None
            session.clear()
            session.update(user_id=user.id, csrf=secrets.token_urlsafe(32))
            session.permanent = True
            login_total.labels('succeeded').inc()
            event('login_succeeded', user_id=user.id)
            return jsonify(csrf=session['csrf'], user=dict(name=user.username, trainer=user.trainer))

    @app.post('/api/logout')
    def logout():
        session.clear()
        return jsonify(message='Signed out')

    @app.get('/api/accounts')
    @auth()
    def accounts():
        with DB() as db:
            a = db.scalar(select(Account).where(Account.user_id == session['user_id']))
            return jsonify(account=dict(number=a.number, balance=money(a.balance), currency='INR'))

    @app.route('/api/beneficiaries', methods=['GET', 'POST'])
    @auth()
    def beneficiaries():
        with DB.begin() as db:
            if request.method == 'POST':
                data = request.get_json()
                if not isinstance(data, dict) or not isinstance(data.get('account'), str):
                    return fail('Enter a recipient account number', 400)
                # Serialize additions for this owner to avoid duplicate insert races.
                db.scalar(select(User).where(User.id == session['user_id']).with_for_update())
                a = db.scalar(select(Account).where(Account.number == data['account'].upper().strip()))
                if not a or a.user_id == session['user_id']:
                    return fail('Enter a different, valid demo account', 400)
                old = db.scalar(select(Beneficiary).where(Beneficiary.user_id == session['user_id'], Beneficiary.account_id == a.id))
                if not old:
                    db.add(Beneficiary(user_id=session['user_id'], account_id=a.id))
            rows = db.execute(select(Account.number, User.username).join(User, User.id == Account.user_id)
                .join(Beneficiary, Beneficiary.account_id == Account.id).where(Beneficiary.user_id == session['user_id'])).all()
            return jsonify(beneficiaries=[dict(account=n, name=u) for n, u in rows])

    @app.post('/api/transfers')
    @auth()
    def transfer():
        data = request.get_json()
        if not isinstance(data, dict):
            return fail('Expected a JSON object', 400)
        key = request.headers.get('Idempotency-Key', '')
        if not 8 <= len(key) <= 100:
            return fail('An Idempotency-Key of 8–100 characters is required', 400)
        try:
            value = Decimal(str(data.get('amount', '')))
            if not value.is_finite() or value <= 0 or value > Decimal('1000000') or value.as_tuple().exponent < -2:
                raise ValueError()
            amount = int(value * 100)
        except (InvalidOperation, ValueError):
            return fail('Enter an amount from ₹0.01 to ₹10,00,000, with at most 2 decimal places', 400)
        if not isinstance(data.get('recipient'), str):
            return fail('Choose a recipient account', 400)
        with DB.begin() as db:
            sender = db.scalar(select(Account).where(Account.user_id == session['user_id']))
            recipient = db.scalar(select(Account).where(Account.number == data['recipient']))
            if not recipient or recipient.id == sender.id:
                return fail('Invalid recipient', 400)
            # Lock in deterministic order. A fresh read after waiting sees committed balances.
            locked = db.scalars(select(Account).where(Account.id.in_([sender.id, recipient.id]))
                .order_by(Account.id).with_for_update().execution_options(populate_existing=True)).all()
            previous = db.scalar(select(Transfer).where(Transfer.sender == sender.id, Transfer.key == key))
            if previous:
                if previous.receiver != recipient.id or previous.amount != amount:
                    return fail('This retry key was already used for different payment details', 409)
                return jsonify(transfer_id=previous.id, message='Existing transfer returned; no second debit', replay=True)
            if sender.balance < amount:
                transfer_total.labels('rejected').inc()
                event('transfer_rejected', reason='insufficient_funds')
                return fail('Insufficient balance', 409)
            event('transfer_started', sender_id=sender.id, receiver_id=recipient.id)
            sender.balance -= amount
            recipient.balance += amount
            tid = str(uuid.uuid4())
            db.add(Transfer(id=tid, sender=sender.id, receiver=recipient.id, amount=amount, key=key, request_id=g.request_id))
            db.flush()
            db.add_all([Ledger(transfer_id=tid, account_id=sender.id, amount=-amount),
                        Ledger(transfer_id=tid, account_id=recipient.id, amount=amount)])
        transfer_total.labels('succeeded').inc()
        event('transfer_committed', transfer_id=tid)
        return jsonify(transfer_id=tid, message='Demo transfer completed', replay=False), 201

    @app.get('/api/transactions')
    @auth()
    def transactions():
        with DB() as db:
            a = db.scalar(select(Account).where(Account.user_id == session['user_id']))
            rows = db.scalars(select(Transfer).where(or_(Transfer.sender == a.id, Transfer.receiver == a.id)).order_by(Transfer.created.desc()).limit(100)).all()
            return jsonify(transactions=[dict(id=t.id, amount=money(t.amount), direction='Debit' if t.sender == a.id else 'Credit',
                created=t.created.isoformat(), status='Completed', request_id=t.request_id) for t in rows])

    @app.get('/metrics')
    def metrics():
        return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

    @app.get('/health/live')
    def live():
        return jsonify(status='up', component='Flask application')

    @app.get('/health/ready')
    def ready():
        with engine.connect() as conn:
            conn.execute(text('SELECT 1'))
            conn.execute(select(Account.id).limit(1))
        return jsonify(status='ready', database='reachable')

    @app.post('/api/lab/<scenario>')
    @auth(trainer=True)
    def lab(scenario):
        if not app.config['ENABLE_LAB']:
            return fail('Support lab disabled', 404)
        event('lab_scenario', scenario=scenario)
        if scenario == 'application-error':
            raise RuntimeError('Intentional classroom exception')
        if scenario == 'database-error':
            return fail('SIMULATED database outage; the real database remains running', 503)
        if scenario == 'validation-error':
            return fail('SIMULATED missing payment amount', 400)
        if scenario == 'slow':
            time.sleep(2)
            return jsonify(message='SIMULATED slow response completed after 2 seconds')
        return fail('Unknown scenario', 404)

    @app.cli.command('init-db')
    def init_db():
        """Create schema and seed a new, dedicated demo database (no destructive reset)."""
        passwords = {n: os.getenv(n.upper() + '_PASSWORD') for n in ('ravi', 'priya', 'trainer')}
        if any(not p or len(p) < 12 for p in passwords.values()):
            raise click.ClickException('Set RAVI_PASSWORD, PRIYA_PASSWORD, TRAINER_PASSWORD (12+ characters each)')
        Base.metadata.create_all(engine)
        with DB.begin() as db:
            for i, name in enumerate(passwords, 1):
                if db.scalar(select(User).where(User.username == name)):
                    continue
                u = User(username=name, password_hash=generate_password_hash(passwords[name]), trainer=name == 'trainer')
                db.add(u)
                db.flush()
                db.add(Account(user_id=u.id, number=f'DEMO100{i}', balance=1000000 if name != 'priya' else 500000))
            db.flush()
            for u in db.scalars(select(User)).all():
                for a in db.scalars(select(Account).where(Account.user_id != u.id)).all():
                    if not db.scalar(select(Beneficiary).where(Beneficiary.user_id == u.id, Beneficiary.account_id == a.id)):
                        db.add(Beneficiary(user_id=u.id, account_id=a.id))
        click.echo('Schema and demo accounts ready. Existing balances and passwords preserved.')

    return app
