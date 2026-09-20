"""P6 接口测试：账号与登录。

这个文件覆盖的重点不是"接口能返回 200"，而是几条**安全性质**：

1. 口令只存哈希，且同一口令两次注册得到不同哈希（加盐有效）
2. 响应体里**永远不出现** token 与 password_hash
3. "用户不存在"与"口令错误"返回完全一致，不泄露账号是否存在
4. 令牌被篡改（改载荷、改算法）一律拒绝
5. **两个账号的学习数据互不可见** —— 账号体系存在的意义就在这一条
"""

from __future__ import annotations

from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.learner_kp_state import LearnerKpState
from app.models.learner_profile import LearnerProfile
from app.models.session import Session as TutorSession
from app.models.user import User

client = TestClient(app)

PASSWORD = "test-password-123"


def _name(prefix: str = "u") -> str:
    """生成符合规则、且不会互相冲突的登录名（字母开头，3–20 位）。"""
    return f"{prefix}{uuid4().hex[:10]}"


@pytest.fixture()
def cleanup_users():
    """记录本用例创建的账号，结束时连学习数据一起清掉。"""
    created: list[str] = []
    yield created

    from app.db.session import SessionLocal

    with SessionLocal() as db:
        for username in created:
            user = db.query(User).filter(User.username == username).first()
            if user is None:
                continue
            learner_id = user.learner_id
            db.query(LearnerKpState).filter(LearnerKpState.learner_id == learner_id).delete()
            db.query(LearnerProfile).filter(LearnerProfile.learner_id == learner_id).delete()
            for item in db.query(TutorSession).filter(TutorSession.learner_id == learner_id).all():
                db.delete(item)
            db.delete(user)
        db.commit()
    client.cookies.clear()


def register(username: str, password: str = PASSWORD, display_name: str | None = None):
    body: dict = {"username": username, "password": password}
    if display_name:
        body["display_name"] = display_name
    return client.post("/api/auth/register", json=body)


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
def test_register_creates_account_and_logs_in(cleanup_users) -> None:
    username = _name()
    cleanup_users.append(username)

    res = register(username, display_name="小测")
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["user"]["username"] == username
    assert body["user"]["display_name"] == "小测"
    assert body["user"]["learner_id"], "必须分配学习档案标识"
    assert "shizhi_session" in res.cookies or res.cookies, "注册后应直接登录"

    # 注册即登录：紧接着访问 /me 应该认得出是谁
    me = client.get("/api/auth/me").json()
    assert me is not None and me["username"] == username


def test_register_normalizes_username_case(cleanup_users) -> None:
    """统一小写入库：避免 Tom / tom 变成两个账号。"""
    suffix = uuid4().hex[:8]
    username = f"Tom{suffix}"
    cleanup_users.append(username.lower())

    res = register(username)
    assert res.status_code == 200
    assert res.json()["user"]["username"] == username.lower()


@pytest.mark.parametrize("bad", ["ab", "1abc", "a b", "has-dash", "x" * 21, ""])
def test_register_rejects_invalid_username(bad, cleanup_users) -> None:
    assert register(bad).status_code == 422


@pytest.mark.parametrize("bad", ["", "12345", "x" * 65])
def test_register_rejects_invalid_password(bad, cleanup_users) -> None:
    assert register(_name(), password=bad).status_code == 422


def test_register_rejects_duplicate_username(cleanup_users) -> None:
    username = _name()
    cleanup_users.append(username)

    assert register(username).status_code == 200
    client.cookies.clear()
    second = register(username)
    assert second.status_code == 409
    # 提示要说人话，不要把数据库约束名抛给用户
    assert "登录名" in second.json()["detail"]


def test_two_accounts_with_same_password_get_different_hashes(cleanup_users) -> None:
    """同一口令两个账号 → 哈希必须不同。相同就说明没加盐。"""
    from app.db.session import SessionLocal

    names = [_name(), _name()]
    cleanup_users.extend(names)
    for name in names:
        assert register(name).status_code == 200
        client.cookies.clear()

    with SessionLocal() as db:
        hashes = [
            db.query(User).filter(User.username == name).first().password_hash for name in names
        ]
    assert hashes[0] != hashes[1]
    assert all(h.startswith("$2") for h in hashes), "应当是 bcrypt 哈希"


# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #
def test_login_returns_user_but_never_token_or_hash(cleanup_users) -> None:
    username = _name()
    cleanup_users.append(username)
    register(username)
    client.cookies.clear()

    res = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert res.status_code == 200
    raw = res.text

    # 响应体里不能出现令牌与口令哈希 —— 令牌走 httpOnly Cookie，不该交给 JS
    assert "password_hash" not in raw
    assert "$2b$" not in raw
    assert '"token"' not in raw
    assert "eyJ" not in raw, "响应体里不该出现 JWT（形如 eyJ...）"


def test_login_wrong_password_and_missing_user_are_indistinguishable(cleanup_users) -> None:
    """两种失败必须返回同样的状态码与同样的措辞，否则可以用来枚举账号。"""
    username = _name()
    cleanup_users.append(username)
    register(username)
    client.cookies.clear()

    wrong = client.post("/api/auth/login", json={"username": username, "password": "definitely-wrong"})
    missing = client.post(
        "/api/auth/login", json={"username": _name(), "password": "definitely-wrong"}
    )

    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["detail"] == missing.json()["detail"]


def test_me_is_null_when_anonymous() -> None:
    client.cookies.clear()
    res = client.get("/api/auth/me")
    assert res.status_code == 200, "未登录不是错误，是正常初始状态"
    assert res.json() is None


def test_logout_clears_session(cleanup_users) -> None:
    username = _name()
    cleanup_users.append(username)
    register(username)
    assert client.get("/api/auth/me").json() is not None

    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").json() is None


# --------------------------------------------------------------------------- #
# 令牌安全
# --------------------------------------------------------------------------- #
def test_tampered_token_is_rejected(cleanup_users) -> None:
    """改载荷、改签名、改算法 —— 三种篡改都必须被拒。"""
    username = _name()
    cleanup_users.append(username)
    res = register(username)
    assert res.status_code == 200

    token = client.cookies.get("shizhi_session")
    assert token, "登录后应下发会话 Cookie"
    header, payload, signature = token.split(".")

    # 1) 改载荷（把 sub 改成别人）—— 签名对不上
    forged_payload = jwt.utils.base64url_encode(
        b'{"sub":"99999","exp":9999999999,"username":"attacker"}'
    ).decode()
    client.cookies.set("shizhi_session", f"{header}.{forged_payload}.{signature}")
    assert client.get("/api/auth/me").json() is None

    # 2) 改签名
    client.cookies.set("shizhi_session", f"{header}.{payload}.AAAAinvalidsignature")
    assert client.get("/api/auth/me").json() is None

    # 3) alg: none 攻击 —— 必须被显式算法白名单挡住
    none_header = jwt.utils.base64url_encode(b'{"alg":"none","typ":"JWT"}').decode()
    client.cookies.set("shizhi_session", f"{none_header}.{payload}.")
    assert client.get("/api/auth/me").json() is None

    client.cookies.clear()


def test_expired_token_is_rejected(cleanup_users, monkeypatch) -> None:
    from app.core import security

    username = _name()
    cleanup_users.append(username)
    register(username)

    # 用负的有效期签一个"已经过期"的令牌
    monkeypatch.setattr(security.settings, "jwt_expire_minutes", -1)
    expired = security.create_access_token(user_id=1, username=username, learner_id="x")
    monkeypatch.undo()

    client.cookies.set("shizhi_session", expired)
    assert client.get("/api/auth/me").json() is None
    client.cookies.clear()


# --------------------------------------------------------------------------- #
# 身份与数据隔离（本文件最重要的一组）
# --------------------------------------------------------------------------- #
def test_request_body_learner_id_is_ignored(cleanup_users) -> None:
    """请求体里的 learner_id 不再被采信 —— 否则就是一个越权读写的口子。"""
    username = _name()
    cleanup_users.append(username)
    res = register(username)
    real_learner = res.json()["user"]["learner_id"]

    # 试图冒充另一个身份
    res = client.get("/api/tutor/dashboard", params={"learner_id": "some-other-learner"})
    assert res.status_code == 200
    assert res.json()["learner_id"] == real_learner, "身份必须来自会话，不是查询串"

    res = client.post(
        "/api/tutor/start",
        json={"knowledge_point_id": 1, "learner_id": "some-other-learner"},
    )
    # 知识点不存在会 404，但那不是这里要验的；关键是**没有被当成别人的身份**
    if res.status_code == 200:
        assert res.json()["session_id"]


def test_two_accounts_have_isolated_learning_data(cleanup_users, tutor_kp) -> None:
    """A 的学习进度不能出现在 B 的看板上。账号体系的意义就在这一条。"""
    names = [_name(), _name()]
    cleanup_users.extend(names)

    learners: list[str] = []
    for name in names:
        res = register(name)
        assert res.status_code == 200
        learners.append(res.json()["user"]["learner_id"])

    assert learners[0] != learners[1], "两个账号必须有不同的学习档案标识"

    # 给 A 造一点学习数据
    from app.db.session import SessionLocal
    from app.services import learner_service

    with SessionLocal() as db:
        state = learner_service.get_or_create_learner_state(
            db, knowledge_point_id=tutor_kp["kp_id"], learner_id=learners[0]
        )
        learner_service.update_learning_state(db, state, correct=True, score=1.0)

    # A 能看到，B 看不到
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": names[0], "password": PASSWORD})
    dash_a = client.get("/api/tutor/dashboard").json()

    client.cookies.clear()
    client.post("/api/auth/login", json={"username": names[1], "password": PASSWORD})
    dash_b = client.get("/api/tutor/dashboard").json()

    assert dash_a["learner_id"] == learners[0]
    assert dash_b["learner_id"] == learners[1]
    assert dash_a["overview"]["tracked"] >= 1
    assert dash_b["overview"]["tracked"] == 0, "B 不该看到 A 的学习数据"

    client.cookies.clear()


def test_anonymous_and_registered_see_different_data(cleanup_users) -> None:
    """匿名落在本地档案上，注册用户落在自己的档案上 —— 两者互不可见。"""
    client.cookies.clear()
    anon = client.get("/api/tutor/dashboard").json()

    username = _name()
    cleanup_users.append(username)
    me = register(username).json()["user"]

    assert anon["learner_id"] != me["learner_id"]
    client.cookies.clear()
