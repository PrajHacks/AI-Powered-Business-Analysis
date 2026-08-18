import pandas as pd
from sqlalchemy import create_engine, inspect, DATE, DATETIME, Text

engine = create_engine("sqlite:///:memory:")

# If we normalize string column to ISO-8601 format (e.g., '2010-05-28' or '2010-05-28 14:30:00')
# and use DATE() or DATETIME() or Text() in dtype:
df1 = pd.DataFrame({
    "date_str": ["2010-05-28", "2011-06-15", None],
    "bad_str": ["5/28/2010", "6/15/2011", None]
})

# Let's test with dtype={'date_str': DATE(), 'bad_str': Text()} vs dtype={'date_str': Text(), 'bad_str': Text()}
# Note: In SQLite, if column is DATE(), passing string '2010-05-28' directly without pandas datetime dtype might trigger TypeError in SQLAlchemy SQLite dialect
# Let's see:
df1_dt = pd.DataFrame({
    "date_col": pd.to_datetime(["5/28/2010", "6/15/2011", None], errors="coerce"),
    "text_col": ["5/28/2010", "6/15/2011", "not a date"]
})

df1_dt.to_sql("sales_dt", con=engine, index=False, dtype={"date_col": DATE(), "text_col": Text()})

with engine.connect() as conn:
    print("sales_dt rows:", conn.exec_driver_sql("SELECT strftime('%Y', date_col), date_col, text_col FROM sales_dt").all())

inspector = inspect(engine)
cols = inspector.get_columns("sales_dt")
for c in cols:
    print("Col:", c["name"], "Type:", type(c["type"]), c["type"])
