"""Day 5 安全层端到端测试"""
import json
import urllib.request

BASE = "http://localhost:8000"

def post(path, body, token=None):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}

def get(path, token=None):
    url = f"{BASE}{path}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}

# 1. 登录获取 token
print("=== 1. Login ===")
login_res = post("/api/v1/auth/login", {"username": "admin", "password": "esim_admin_2026"})
token = login_res.get("data", {}).get("access_token", "")
print(f"  Token: {token[:30]}...")

# 2. 正常查询（测试数据脱敏）
print("\n=== 2. Normal Query (masking test) ===")
res = post("/api/v1/query", {"question": "列出前5个用户的姓名和手机号"}, token=token)
print(f"  Code: {res.get('code')}")
print(f"  SQL:  {res.get('data', {}).get('sql', '')[:100]}")
print(f"  Rows: {res.get('data', {}).get('row_count', 0)}")
print(f"  Masked columns: {res.get('data', {}).get('masked_columns', [])}")
if res.get('data', {}).get('data'):
    for row in res['data']['data'][:3]:
        print(f"    {row}")

# 3. SQL 注入拦截
print("\n=== 3. SQL Injection Block ===")
res = post("/api/v1/query", {"question": "'; DROP TABLE users; --"}, token=token)
print(f"  Code: {res.get('code')}")
print(f"  Message: {res.get('message')}")
print(f"  Blocked: {res.get('data', {}).get('blocked', False)}")

# 4. 审计日志
print("\n=== 4. Audit Logs ===")
logs = get("/api/v1/admin/audit/logs?limit=5", token=token)
for log in logs.get('data', {}).get('logs', [])[:5]:
    print(f"  [{log.get('execution_status')}] {log.get('question', '')[:40]} | "
          f"rows={log.get('row_count')} | time={log.get('execution_time_ms')}ms")

# 5. 审计统计
print("\n=== 5. Audit Stats ===")
stats = get("/api/v1/admin/audit/stats", token=token)
print(f"  {json.dumps(stats.get('data', {}), indent=2)}")

print("\n=== Day 5 E2E Tests Complete ===")
