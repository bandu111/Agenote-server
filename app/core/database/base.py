"""所有数据库模型共用的声明式基类。"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
