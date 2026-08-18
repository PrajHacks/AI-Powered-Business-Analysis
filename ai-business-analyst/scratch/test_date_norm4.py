import pandas as pd
from sqlalchemy import create_engine, inspect, DATE, DATETIME, Text

# Test A: String '2010-05-28' with Text() in SQLite
engine_a = create_engine("sqlite:///:memory:")
df_a = pd.DataFrame({"d": ["2010-05-28", "2011-06-15", None]})
df_a.to_sql("tbl_a", con=engine_a, index=False, dtype={"d": Text()})
with engine_a.connect() as conn:
    print("Test A strftime:", conn.exec_driver_sql("SELECT strftime('%Y', d), d FROM tbl_a").all())
inspector_a = inspect(engine_a)
print("Test A inspector type:", inspector_a.get_columns("tbl_a")[0]["type"])

# Test B: String '2010-05-28' with DATE() in SQLite
engine_b = create_engine("sqlite:///:memory:")
df_b = pd.DataFrame({"d": ["2010-05-28", "2011-06-15", None]})
try:
    df_b.to_sql("tbl_b", con=engine_b, index=False, dtype={"d": DATE()})
    print("Test B succeeded")
except Exception as e:
    print("Test B failed:", type(e), e)

# Test C: Python datetime.date objects with DATE() in SQLite
engine_c = create_engine("sqlite:///:memory:")
s_dt = pd.to_datetime(pd.Series(["5/28/2010", "6/15/2011", None]), errors="coerce", dayfirst=False)
df_c = pd.DataFrame({"d": s_dt.dt.date})
df_c.to_sql("tbl_c", con=engine_c, index=False, dtype={"d": DATE()})
with engine_c.connect() as conn:
    print("Test C strftime:", conn.exec_driver_sql("SELECT strftime('%Y', d), d FROM tbl_c").all())
inspector_c = inspect(engine_c)
print("Test C inspector type:", inspector_c.get_columns("tbl_c")[0]["type"])

# Test D: Python datetime.datetime / pd.Timestamp with DATETIME() in SQLite
engine_d = create_engine("sqlite:///:memory:")
s_dt_d = pd.to_datetime(pd.Series(["5/28/2010 14:30:00", "6/15/2011 09:15:00", None]), errors="coerce", dayfirst=False)
df_d = pd.DataFrame({"dt": s_dt_d})
df_d.to_sql("tbl_d", con=engine_d, index=False, dtype={"dt": DATETIME()})
with engine_d.connect() as conn:
    print("Test D strftime:", conn.exec_driver_sql("SELECT strftime('%Y', dt), strftime('%H', dt), dt FROM tbl_d").all())
inspector_d = inspect(engine_d)
print("Test D inspector type:", inspector_d.get_columns("tbl_d")[0]["type"])
