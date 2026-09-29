import pytest
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql
from app import Base, create_app, money


@pytest.fixture
def app():
    return create_app({'TESTING':True, 'SECRET_KEY':'test-only-key',
        'DATABASE_URL':'postgresql+psycopg://test:test@127.0.0.1:1/test',
        'SESSION_COOKIE_SECURE':False})


def test_login_page_and_assets(app):
    c=app.test_client()
    assert c.get('/').status_code==200
    assert c.get('/static/app.js').status_code==200
    assert c.get('/static/style.css').status_code==200


def test_csrf_required(app):
    r=app.test_client().post('/api/login', json={'username':'ravi','password':'x'})
    assert r.status_code==403


def test_anonymous_cannot_read_accounts(app):
    assert app.test_client().get('/api/accounts').status_code==401


def test_malformed_login_is_validation_error(app):
    c=app.test_client(); token=c.get('/api/session').json['csrf']
    assert c.post('/api/login',json=[],headers={'X-CSRF-Token':token}).status_code==400


def test_live_does_not_require_database(app):
    assert app.test_client().get('/health/live').status_code==200


def test_readiness_detects_unreachable_database(app):
    r=app.test_client().get('/health/ready')
    assert r.status_code==503
    assert 'test:test' not in r.get_data(as_text=True)


def test_response_security_and_request_id(app):
    r=app.test_client().get('/api/session')
    assert len(r.headers['X-Request-ID'])==36
    assert r.headers['Cache-Control']=='no-store'
    assert "frame-ancestors 'none'" in r.headers['Content-Security-Policy']
    assert 'HttpOnly' in r.headers['Set-Cookie']


def test_schema_compiles_for_postgres():
    for table in Base.metadata.sorted_tables:
        assert str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert money(501)=='5.01'


def test_requires_configuration():
    with pytest.raises(RuntimeError):
        create_app({'SECRET_KEY':None,'DATABASE_URL':None})
