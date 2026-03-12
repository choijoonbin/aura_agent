"""
tests/conftest.py — 테스트 환경 전역 픽스처.

psycopg2 가 설치되지 않은 테스트 환경에서 SQLAlchemy가
import-time 에 postgresql 방언 드라이버를 초기화할 때 실패하는 것을 방지한다.
(db/session.py 에서 create_engine 이 모듈 로드 시점에 호출됨)
"""
import sys
from unittest.mock import MagicMock

# psycopg2 가 없으면 가짜 모듈을 sys.modules 에 주입한다.
# SQLAlchemy dialect 초기화가 드라이버 임포트를 시도하기 전에 처리된다.
if "psycopg2" not in sys.modules:
    sys.modules["psycopg2"] = MagicMock()
    sys.modules["psycopg2.extensions"] = MagicMock()
    sys.modules["psycopg2.extras"] = MagicMock()
