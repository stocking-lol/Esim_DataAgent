"""Debug sqlglot AST structure for RLS"""
import sqlglot
from sqlglot import exp

sql = "SELECT * FROM users"
parsed = sqlglot.parse_one(sql, dialect="mysql")
print("Type:", type(parsed).__name__)
print("Args keys:", list(parsed.args.keys()))
print()

selects = list(parsed.find_all(exp.Select))
print(f"Found {len(selects)} Select nodes")
for s in selects:
    print(f"  Select args keys: {list(s.args.keys())}")
    from_clause = s.args.get("from")
    print(f"  from clause: {from_clause}")
    print(f"  from type: {type(from_clause).__name__ if from_clause else None}")
    if from_clause:
        tables = list(from_clause.find_all(exp.Table))
        print(f"  tables in from: {[(t.name, t.alias) for t in tables]}")

    all_tables = list(s.find_all(exp.Table))
    print(f"  all tables in select: {[(t.name, t.alias) for t in all_tables]}")

print()
sql2 = "SELECT u.id, o.amount FROM users u JOIN orders o ON u.id = o.user_id"
parsed2 = sqlglot.parse_one(sql2, dialect="mysql")
print("JOIN test:")
print(f"  Type: {type(parsed2).__name__}")
selects2 = list(parsed2.find_all(exp.Select))
for s in selects2:
    from_clause = s.args.get("from")
    print(f"  from: {from_clause}")
    all_tables = list(s.find_all(exp.Table))
    print(f"  all tables: {[(t.name, t.alias) for t in all_tables]}")
    joins = s.args.get("joins")
    print(f"  joins: {joins}")

print()
# Test setting where
print("Testing WHERE injection:")
sql3 = "SELECT * FROM users"
parsed3 = sqlglot.parse_one(sql3, dialect="mysql")
select3 = parsed3
cond = exp.column("mvno_id", table="users").eq(exp.Literal.number(1))
print(f"  condition: {cond}")
print(f"  condition type: {type(cond).__name__}")
select3.set("where", exp.Where(this=cond))
print(f"  result SQL: {select3.sql(dialect='mysql')}")

print()
# Test with existing WHERE
sql4 = "SELECT * FROM users WHERE status = 'active'"
parsed4 = sqlglot.parse_one(sql4, dialect="mysql")
existing_where = parsed4.args.get("where")
print(f"  existing where: {existing_where}")
print(f"  existing where type: {type(existing_where).__name__}")
if existing_where:
    print(f"  existing where.this: {existing_where.this}")
    print(f"  existing where.this type: {type(existing_where.this).__name__}")
    combined = exp.and_(existing_where.this, cond)
    parsed4.set("where", exp.Where(this=combined))
    print(f"  result SQL: {parsed4.sql(dialect='mysql')}")
