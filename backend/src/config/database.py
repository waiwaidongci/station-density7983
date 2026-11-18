from sqlalchemy import create_engine,inspect,text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .settings import DATABASE_URL
class Base(DeclarativeBase): pass
engine=create_engine(DATABASE_URL,pool_pre_ping=True)
Session=sessionmaker(bind=engine,expire_on_commit=False)
def ensure_schema():
    """Idempotent column upgrades for an already-initialised volume."""
    inspector=inspect(engine)
    if 'station_readings' not in inspector.get_table_names():return
    columns={c['name'] for c in inspector.get_columns('station_readings')}
    if 'instrument_key' not in columns:
        with engine.begin() as c:
            c.execute(text('ALTER TABLE station_readings ADD COLUMN instrument_key VARCHAR(120)'))
    with engine.begin() as c:
        c.execute(text('CREATE INDEX IF NOT EXISTS ix_station_readings_instrument_key '
                       'ON station_readings (instrument_key)'))
def db_session():
    with Session() as db: yield db
