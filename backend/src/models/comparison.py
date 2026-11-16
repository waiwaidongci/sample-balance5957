from datetime import datetime
from sqlalchemy import String,JSON,DateTime,Float,Integer
from sqlalchemy.orm import Mapped,mapped_column
from src.config.database import Base
class ComparisonGroup(Base):
    __tablename__='comparison_groups'
    id:Mapped[int]=mapped_column(primary_key=True)
    name:Mapped[str]=mapped_column(String(100),unique=True)
    description:Mapped[str]=mapped_column(String(500),default='')
    tolerance:Mapped[float]=mapped_column(Float)
    max_iterations:Mapped[int]=mapped_column(Integer)
    cap_ratio:Mapped[float]=mapped_column(Float)
    target_snapshot:Mapped[list]=mapped_column(JSON)
    target_checksum:Mapped[str]=mapped_column(String(64))
    target_epoch:Mapped[int]=mapped_column(Integer,default=1)
    version:Mapped[int]=mapped_column(Integer,default=0)
    published_version:Mapped[int|None]=mapped_column(Integer,nullable=True)
    status:Mapped[str]=mapped_column(String(20),default='draft')
    last_error:Mapped[str|None]=mapped_column(String(500),nullable=True)
    created_by:Mapped[int]=mapped_column(Integer)
    created_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
    updated_at:Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow,onupdate=datetime.utcnow)
