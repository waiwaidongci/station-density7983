from enum import Enum
class Role(str,Enum):
    admin='admin'; analyst='analyst'; viewer='viewer'
class RunState(str,Enum):
    queued='queued'; running='running'; completed='completed'; failed='failed'; stale='stale'
class CalibrationState(str,Enum):
    queued='queued'; running='running'; staged='staged'; published='published'
    failed='failed'; stale='stale'
class ReviewState(str,Enum):
    draft='draft'; approved='approved'
