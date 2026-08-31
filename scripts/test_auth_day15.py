"""Day 15 认证系统验证测试"""
import sys
from pathlib import Path

project_root = str(Path(__file__).resolve().parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from app.services.auth_service import auth_service
from app.utils.crypto import hash_password, verify_password
from app.core.auth import login_with_fallback, DBAuthManager


def main():
    # Test 1: crypto
    h = hash_password("test123")
    assert verify_password("test123", h), "crypto verify failed"
    assert not verify_password("wrong", h), "crypto should reject wrong password"
    print("[PASS] Crypto utilities OK")

    # Test 2: authenticate DB user
    user = auth_service.authenticate_user("admin", "esim_admin_2026")
    assert user is not None, "admin auth failed"
    assert user.role == "admin", f"wrong role: {user.role}"
    print(f"[PASS] DB auth OK: admin (id={user.id}, role={user.role})")

    # Test 3: wrong password
    user2 = auth_service.authenticate_user("admin", "wrongpass")
    assert user2 is None, "should reject wrong password"
    print("[PASS] Wrong password rejected")

    # Test 4: list users
    result = auth_service.list_users()
    assert result["total"] >= 3, f"expected >=3 users, got {result['total']}"
    print(f"[PASS] List users OK: total={result['total']}")

    # Test 5: DBAuthManager fallback
    result = login_with_fallback("admin", "esim_admin_2026")
    assert result is not None, "login_with_fallback failed"
    print(f"[PASS] login_with_fallback OK: token={result['access_token'][:20]}...")

    # Test 6: analyst DB auth
    result2 = login_with_fallback("analyst", "esim_analyst_2026")
    assert result2 is not None
    print("[PASS] analyst DB auth OK")

    # Test 7: viewer DB auth
    result3 = login_with_fallback("viewer", "esim_viewer_2026")
    assert result3 is not None
    print("[PASS] viewer DB auth OK")

    # Test 8: register new user
    test_user = auth_service.register_user(
        username="test_user_day15",
        email="test_day15@esim-platform.local",
        password="testpass123",
        role="viewer",
    )
    assert test_user.id is not None
    print(f"[PASS] Register OK: id={test_user.id}")

    # Test 9: update role
    updated = auth_service.update_user_role(test_user.id, "analyst")
    assert updated.role == "analyst"
    print("[PASS] Update role OK")

    # Test 10: deactivate
    deactivated = auth_service.deactivate_user(test_user.id)
    assert not deactivated.is_active
    print("[PASS] Deactivate OK")

    # Cleanup
    from app.config.database import db_manager
    session = db_manager.get_session()
    session.query(type(test_user)).filter_by(id=test_user.id).delete()
    session.commit()
    session.close()
    print("[PASS] Cleanup done")

    print("\n=== All tests passed! ===")


if __name__ == "__main__":
    main()
