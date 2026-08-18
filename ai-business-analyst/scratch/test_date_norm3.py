import pandas as pd
from sqlalchemy import create_engine, inspect, DATE, DATETIME, Text

engine = create_engine("sqlite:///:memory:")

# Test 1: Date only with pd.to_datetime and .dt.date
parsed_dates = pd.to_datetime(pd.Series(["5/28/2010", "6/15/2011", "invalid_val", None]), errors="coerce", dayfirst=False)
has_time = (parsed_dates.dt.hour != 0).any() or (parsed_dates.dt.minute != 0).any() or (parsed_dates.dt.second != 0).any()
print("has_time for date-only:", has_time)

# Test 2: Datetime with pd.to_datetime
parsed_dt = pd.to_datetime(pd.Series(["5/28/2010 14:30:00", "6/15/2011 09:15:22", "invalid_val", None]), errors="coerce", dayfirst=False)
has_time_dt = (parsed_dt.dt.hour != 0).any() or (parsed_dt.dt.minute != 0).any() or (parsed_dt.dt.second != 0).any()
print("has_time for datetime:", has_time_dt)

df = pd.DataFrame({
    "d_col": parsed_dates.dt.date,
    "dt_col": parsed_dt,
})

df.to_sql("test_tbl", con=engine, index=False, dtype={"d_col": DATE(), "dt_col": DATETIME()})

with engine.connect() as conn:
    rows = conn.exec_driver_sql("SELECT d_col, dt_col, strftime('%Y', d_col), strftime('%Y', dt_col) FROM test_tbl").all()
    print("Rows in DB:", rows)

inspector = inspect(engine)
for c in inspector.get_columns("test_tbl"):
    print("Schema Column:", c["name"], c["type"])
