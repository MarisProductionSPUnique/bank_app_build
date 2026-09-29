"""Run only against a disposable, dedicated database; tables are created and dropped."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select, func, event
from app import create_app, Base, Account, Transfer, Ledger

URL=os.getenv('TEST_DATABASE_URL')
pytestmark=pytest.mark.skipif(not URL,reason='TEST_DATABASE_URL is required for PostgreSQL integration tests')


@pytest.fixture
def app(monkeypatch):
    app=create_app({'TESTING':True,'SECRET_KEY':'integration-only','DATABASE_URL':URL,
        'SESSION_COOKIE_SECURE':False,'ENABLE_LAB':True})
    engine=app.extensions['bank_engine']
    # Require an explicitly disposable database name before dropping anything.
    assert engine.url.database.endswith('_test'), 'Database name must end in _test'
    Base.metadata.drop_all(engine)
    for name in ('RAVI','PRIYA','TRAINER'):
        monkeypatch.setenv(name+'_PASSWORD','classroom-test-password')
    result=app.test_cli_runner().invoke(args=['init-db'])
    assert result.exit_code==0, result.output
    yield app
    Base.metadata.drop_all(engine)
    engine.dispose()


def login(app, user='ravi'):
    c=app.test_client(); token=c.get('/api/session').json['csrf']
    r=c.post('/api/login',json={'username':user,'password':'classroom-test-password'},headers={'X-CSRF-Token':token})
    assert r.status_code==200
    return c,r.json['csrf']


def pay(c,token,amount='500',key=None):
    return c.post('/api/transfers',json={'recipient':'DEMO1002','amount':amount},
        headers={'X-CSRF-Token':token,'Idempotency-Key':key or str(uuid.uuid4())})


def test_transfer_balances_ledger_and_retry(app):
    c,token=login(app); key=str(uuid.uuid4())
    assert pay(c,token,key=key).status_code==201
    assert pay(c,token,key=key).json['replay'] is True
    assert pay(c,token,amount='501',key=key).status_code==409
    assert c.get('/api/accounts').json['account']['balance']=='9500.00'
    p,_=login(app,'priya')
    assert p.get('/api/accounts').json['account']['balance']=='5500.00'
    with app.extensions['bank_db']() as db:
        assert db.scalar(select(func.count()).select_from(Transfer))==1
        assert db.scalar(select(func.sum(Ledger.amount)))==0
        assert db.scalar(select(func.count()).select_from(Ledger))==2


def test_concurrent_transfers_cannot_overspend(app):
    def send(_):
        c,token=login(app)
        return pay(c,token,'8000').status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(send,range(2)))==[201,409]


def test_concurrent_duplicate_debits_once(app):
    key=str(uuid.uuid4())
    def send(_):
        c,token=login(app)
        return pay(c,token,key=key).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(send,range(2)))==[200,201]


def test_database_exception_rolls_back_debit(app):
    c,token=login(app)
    def reject(*args):
        raise RuntimeError('Test ledger failure')
    event.listen(Ledger,'before_insert',reject)
    try:
        assert pay(c,token).status_code==500
    finally:
        event.remove(Ledger,'before_insert',reject)
    assert c.get('/api/accounts').json['account']['balance']=='10000.00'
    assert c.get('/api/transactions').json['transactions']==[]


def test_lab_is_trainer_only(app):
    c,token=login(app)
    assert c.post('/api/lab/application-error',headers={'X-CSRF-Token':token}).status_code==403
    c,token=login(app,'trainer')
    assert c.post('/api/lab/application-error',headers={'X-CSRF-Token':token}).status_code==500


@pytest.mark.parametrize('amount',['0','-1','NaN','Infinity','1.001','1000001'])
def test_invalid_amount(app,amount):
    c,token=login(app)
    assert pay(c,token,amount).status_code==400
