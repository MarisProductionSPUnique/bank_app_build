"""Generate local-only configuration. Never overwrites an existing .env."""
from pathlib import Path
import secrets

path=Path(__file__).parent / '.env'
password=secrets.token_hex(20)
values={
    'SECRET_KEY':secrets.token_hex(32), 'DB_PASSWORD':password,
    'DATABASE_URL':f'postgresql+psycopg://bank:{password}@localhost:5432/bank_demo',
    'RAVI_PASSWORD':secrets.token_urlsafe(18),
    'PRIYA_PASSWORD':secrets.token_urlsafe(18),
    'TRAINER_PASSWORD':secrets.token_urlsafe(18),
    'COOKIE_SECURE':'false', 'ENABLE_LAB':'true',
}
with path.open('x') as output:
    output.write('\n'.join(f'{key}={value}' for key,value in values.items())+'\n')
path.chmod(0o600)
print('Created .env. Open it locally to see classroom passwords. Do not commit or share this file.')
