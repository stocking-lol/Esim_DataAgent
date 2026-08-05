"""SQL 安全网关快速测试"""
import sys
sys.path.insert(0, '.')
from app.core.sql_security import sql_gateway

tests = [
    ("Normal SELECT", lambda: sql_gateway.validate_sql("SELECT COUNT(*) FROM users")),
    ("DROP TABLE", lambda: sql_gateway.validate_sql("DROP TABLE users")),
    ("DELETE", lambda: sql_gateway.validate_sql("DELETE FROM users WHERE id=1")),
    ("Non-whitelist table", lambda: sql_gateway.validate_sql("SELECT * FROM mysql.user")),
    ("SQL injection input", lambda: sql_gateway.check_input("'; DROP TABLE users; --")),
    ("Normal input", lambda: sql_gateway.check_input("本月新增多少eSIM用户")),
    ("Auto LIMIT", lambda: sql_gateway.validate_sql("SELECT * FROM users")),
    ("Too many JOINs", lambda: sql_gateway.validate_sql(
        "SELECT * FROM users u "
        "JOIN orders o ON u.id=o.user_id "
        "JOIN plans p ON o.plan_id=p.id "
        "JOIN esim_profiles e ON u.id=e.user_id "
        "JOIN data_usage d ON u.id=d.user_id "
        "JOIN operators op ON e.mno_id=op.id"
    )),
    ("Dangerous function", lambda: sql_gateway.validate_sql("SELECT LOAD_FILE('/etc/passwd')")),
]

for name, fn in tests:
    r = fn()
    status = "PASS" if r.passed else "BLOCK"
    extra = f" layer={r.layer}" if not r.passed else ""
    if "LIMIT" in r.sql_after_check.upper() and r.passed:
        extra = " (auto-LIMIT added)"
    print(f"  {status:5s} | {name:25s} | reason={r.reason}{extra}")

print("\nAll security tests completed!")
