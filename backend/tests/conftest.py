import os
import sys
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

import pytest


@pytest.fixture()
def env(tmp_path,monkeypatch):
    db_path=tmp_path/'test.db'
    monkeypatch.setenv('DATABASE_URL','sqlite:///'+str(db_path))
    monkeypatch.setenv('ARTIFACT_DIR',str(tmp_path/'artifacts'))
    monkeypatch.setenv('CALIBRATION_CHECKPOINT_DIR',str(tmp_path/'checkpoints'))
    monkeypatch.setenv('REDIS_URL','redis://127.0.0.1:6399/0')
    # Re-import with the isolated settings.
    for name in list(sys.modules):
        if name=='src' or name.startswith('src.'):
            del sys.modules[name]
    from src.config.database import Base,engine,Session
    from src.models import user,source,record,run,note,audit,calibration
    Base.metadata.create_all(engine)
    from src.models.user import User
    from src.utils.security import hash_password
    with Session() as db:
        u=User(email='admin@test.demo',password_hash=hash_password('x'),role='admin')
        db.add(u);db.commit()
        user_id=u.id
    return {'Session':Session,'user_id':user_id,'tmp':tmp_path}


@pytest.fixture()
def stub_queue(env,monkeypatch):
    # Patch the client class so every already-imported queue instance is covered.
    from redis import Redis
    monkeypatch.setattr(Redis,'rpush',lambda *a,**k:0)
    return env


@pytest.fixture()
def admin(env):
    from src.models.user import User
    def get(db):return db.get(User,env['user_id'])
    return get
