from pydantic import BaseModel,Field,field_validator


class InstrumentInput(BaseModel):
    key:str=Field(min_length=1,max_length=120)
    name:str=Field(default='',max_length=160)
    is_anchor:bool=False


class AnchorInput(BaseModel):
    is_anchor:bool


class PairObservation(BaseModel):
    left_key:str=Field(min_length=1,max_length=120)
    right_key:str=Field(min_length=1,max_length=120)
    site:str=Field(default='',max_length=120)
    session_key:str=Field(default='',max_length=120)
    observed_at:str=Field(default='',max_length=40)
    temperature:float
    humidity:float
    particles:float
    weight:float=Field(default=1.0,gt=0,le=1e6)

    @field_validator('temperature','humidity','particles')
    @classmethod
    def finite(cls,v):
        import math
        if not math.isfinite(v):raise ValueError('配对差必须为有限数值')
        return v


class PairBatch(BaseModel):
    observations:list[PairObservation]=Field(min_length=1,max_length=1000)


class PairCorrection(PairObservation):
    pass


class CalibrationRunInput(BaseModel):
    name:str=Field(min_length=2,max_length=160)
    tolerance:float=Field(default=1e-8,gt=0,le=1e-2)
    max_iter:int=Field(default=5000,ge=10,le=200000)
    alert_temperature:float=Field(default=1.0,gt=0,le=1000)
    alert_humidity:float=Field(default=5.0,gt=0,le=1000)
    alert_particles:float=Field(default=10.0,gt=0,le=100000)
