from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import ProgrammingError

from backend.app.db.session import get_db
from backend.app.main import app


def test_database_operational_error_returns_json_503(client):
    def broken_get_db():
        raise OperationalError("select 1", {}, Exception("connection refused"))
        yield

    app.dependency_overrides[get_db] = broken_get_db
    try:
        response = client.get("/api/rubrics")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 503
    assert "数据库暂不可用" in response.json()["detail"]


def test_database_schema_error_returns_json_503(client):
    def broken_get_db():
        raise ProgrammingError("select * from rubrics", {}, Exception('relation "rubrics" does not exist'))
        yield

    app.dependency_overrides[get_db] = broken_get_db
    try:
        response = client.get("/api/rubrics")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 503
    assert "数据库表尚未初始化" in response.json()["detail"]
