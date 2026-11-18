from sqlalchemy import Boolean,Column,Float,Integer,JSON,String,Text,UniqueConstraint
from ..config.database import Base


class CalibrationInstrument(Base):
    __tablename__='calibration_instruments'
    id=Column(Integer,primary_key=True)
    key=Column(String(120),unique=True,nullable=False)
    name=Column(String(160),default='')
    is_anchor=Column(Boolean,default=False,nullable=False)
    created_at=Column(String(40),nullable=False)


class CalibrationPair(Base):
    """One co-located, same-session comparison of two instruments.

    Differences are canonical: left_key < right_key and difference is
    ``left reading - right reading``.  Reversed input is flipped on write.
    """
    __tablename__='calibration_pairs'
    __table_args__=(UniqueConstraint('observation_key',name='uq_calibration_observation'),)
    id=Column(Integer,primary_key=True)
    observation_key=Column(String(200),nullable=False)
    left_key=Column(String(120),nullable=False,index=True)
    right_key=Column(String(120),nullable=False,index=True)
    site=Column(String(120),default='')
    session_key=Column(String(120),default='')
    observed_at=Column(String(40),default='')
    temperature=Column(Float,nullable=False)
    humidity=Column(Float,nullable=False)
    particles=Column(Float,nullable=False)
    weight=Column(Float,nullable=False)
    revision=Column(Integer,default=0,nullable=False)
    updated_at=Column(String(40),nullable=False)


class CalibrationState(Base):
    __tablename__='calibration_state'
    id=Column(Integer,primary_key=True)
    graph_version=Column(Integer,default=0,nullable=False)
    published_version_id=Column(Integer,nullable=True)


class CalibrationRun(Base):
    __tablename__='calibration_runs'
    id=Column(Integer,primary_key=True)
    name=Column(String(160),nullable=False)
    state=Column(String(20),default='queued',nullable=False)
    graph_version=Column(Integer,nullable=False)
    parameters=Column(JSON,nullable=False)
    progress=Column(JSON,default=dict)
    result=Column(JSON,nullable=True)
    error=Column(Text,default='')
    created_at=Column(String(40),nullable=False)
    heartbeat_at=Column(String(40),default='')
    completed_at=Column(String(40),default='')


class CalibrationVersion(Base):
    __tablename__='calibration_versions'
    __table_args__=(UniqueConstraint('run_id',name='uq_calibration_version_run'),)
    id=Column(Integer,primary_key=True)
    run_id=Column(Integer,nullable=False)
    graph_version=Column(Integer,nullable=False)
    state=Column(String(20),default='staged',nullable=False)
    summary=Column(JSON,nullable=True)
    checksum=Column(String(64),default='')
    created_at=Column(String(40),nullable=False)
    published_at=Column(String(40),default='')


class CalibrationOffset(Base):
    __tablename__='calibration_offsets'
    __table_args__=(UniqueConstraint('version_id','instrument_key',name='uq_calibration_offset'),)
    id=Column(Integer,primary_key=True)
    version_id=Column(Integer,nullable=False,index=True)
    instrument_key=Column(String(120),nullable=False,index=True)
    temperature=Column(Float,nullable=False)
    humidity=Column(Float,nullable=False)
    particles=Column(Float,nullable=False)


class CalibrationResidual(Base):
    __tablename__='calibration_residuals'
    __table_args__=(UniqueConstraint('version_id','pair_id',name='uq_calibration_residual'),)
    id=Column(Integer,primary_key=True)
    version_id=Column(Integer,nullable=False,index=True)
    pair_id=Column(Integer,nullable=False)
    left_key=Column(String(120),nullable=False)
    right_key=Column(String(120),nullable=False)
    weight=Column(Float,nullable=False)
    temperature=Column(Float,nullable=False)
    humidity=Column(Float,nullable=False)
    particles=Column(Float,nullable=False)


class CalibrationComponentCache(Base):
    __tablename__='calibration_component_cache'
    fingerprint=Column(String(64),primary_key=True)
    nodes=Column(Integer,nullable=False)
    edges=Column(Integer,nullable=False)
    anchors=Column(Integer,nullable=False)
    status=Column(String(20),nullable=False)
    result=Column(JSON,nullable=False)
    created_at=Column(String(40),nullable=False)
